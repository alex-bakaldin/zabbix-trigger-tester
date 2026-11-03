"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trigger_tester import __version__
from trigger_tester.api import ZabbixAPI, ZabbixAPIError
from trigger_tester.binder import BindError, bind
from trigger_tester.expression.evaluator import EvalContext, Evaluator
from trigger_tester.expression.parser import ParseError, parse_expression, substitute_macros
from trigger_tester.expression.period import PeriodError, parse_duration
from trigger_tester.expression.ast import to_text
from trigger_tester.history import Series, TrendSeries
from trigger_tester.model import Trigger
from trigger_tester.native import NativeEngine, NativeError, NativeEvaluator, NativeParseError
from trigger_tester.report import Reporter, build_json, format_duration, write_json
from trigger_tester.simulator import OK, PROBLEM, Simulator, build_points
from trigger_tester.timeparse import parse_step, parse_time

DEFAULT_TIMER_STEP = 30
MIN_VERSION = (7, 0)


class CliError(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zbx-trigger-test",
        description="Replay a Zabbix 7.0+ trigger expression against item history for a time period.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    conn = parser.add_argument_group("connection")
    conn.add_argument("--url", default=os.environ.get("ZABBIX_URL"), help="Zabbix frontend URL (env ZABBIX_URL)")
    conn.add_argument("--token", default=os.environ.get("ZABBIX_TOKEN"), help="API token (env ZABBIX_TOKEN)")
    conn.add_argument("--user", default=os.environ.get("ZABBIX_USER"), help="user name, alternative to --token (env ZABBIX_USER)")
    conn.add_argument("--password", default=os.environ.get("ZABBIX_PASSWORD"), help="password (env ZABBIX_PASSWORD)")
    conn.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    conn.add_argument("--timeout", type=float, default=60, help="HTTP timeout, seconds")
    conn.add_argument("--chunk", default="6h", metavar="DURATION",
            help="fetch history in time windows of this length to keep requests small (0 = whole period at once)")
    conn.add_argument("--page-size", type=int, default=10000, metavar="N",
            help="maximum rows per history.get request inside a window")

    what = parser.add_argument_group("what to test")
    what.add_argument("-t", "--trigger", help="trigger ID to replay (either this or --expression)")
    what.add_argument("--from", dest="time_from", help="period start: ISO date/time, unix ts, now-1d, now/d ...")
    what.add_argument("--till", dest="time_till", default="now", help="period end")
    what.add_argument("--period", default="1d", help="period length, used when --from is omitted")
    what.add_argument("--step", default="auto", help="timer evaluation interval (e.g. 30s); auto = 30s when the trigger has time-based functions; 0 = off")
    what.add_argument("--expression", help="replay an ad-hoc expression instead of a trigger (Zabbix syntax, /host/key references)")
    what.add_argument("--recovery-expression", help="recovery expression for --expression; the ad-hoc trigger then recovers by it")
    what.add_argument("--macro", action="append", default=[], metavar="{$NAME}=VALUE", help="value for a user macro left unresolved in the expression (repeatable)")
    what.add_argument("--initial-state", choices=["auto", "ok", "problem"], default="auto", help="trigger state at period start; auto = taken from the last event before the period")
    what.add_argument("--compare-events", action="store_true", help="fetch real trigger events for the period and compare")
    what.add_argument("--engine", choices=["auto", "native", "python"], default="auto", help="native = Zabbix server code (libzbxtester.so), python = built-in reimplementation; auto prefers native")

    out = parser.add_argument_group("output")
    out.add_argument("-v", "--verbose", action="count", default=0, help="print every evaluation (-vv adds error details)")
    out.add_argument("--json", metavar="FILE", help="write detailed results as JSON")
    out.add_argument("--tz", help="time zone for display and now/d-style rounding (IANA name); default: system local")
    out.add_argument("--debug", action="store_true", help="log API calls")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _parse_macros(values: list[str]) -> dict[str, str]:
    macros = {}
    for item in values:
        name, sep, value = item.partition("=")
        if not sep:
            raise CliError(f"--macro expects {{$NAME}}=VALUE, got {item!r}")
        name = name.strip()
        if not name.startswith("{"):
            name = "{$" + name.lstrip("$") + "}"
        macros[name] = value
    return macros


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for chunk in text.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if not args.url:
        raise CliError("--url (or ZABBIX_URL) is required")
    if not args.token and not (args.user and args.password):
        raise CliError("either --token or --user/--password is required")

    tz = None
    tz_name = "local"
    if args.tz:
        try:
            tz = ZoneInfo(args.tz)
            tz_name = args.tz
        except ZoneInfoNotFoundError:
            raise CliError(f"unknown time zone {args.tz!r}") from None
        # the native engine uses libc local time (like the server does), so switch the process zone too
        os.environ["TZ"] = args.tz
        time.tzset()

    engine: NativeEngine | None = None
    if args.engine != "python":
        try:
            engine = NativeEngine()
            engine.set_debug(bool(args.debug))
        except NativeError as exc:
            if args.engine == "native":
                raise CliError(str(exc)) from None
            print(f"warning: {exc}; falling back to the Python implementation (reduced fidelity)", file=sys.stderr)

    now = int(time.time())
    try:
        time_till = parse_time(args.time_till, tz, now)
        if args.time_from:
            time_from = parse_time(args.time_from, tz, now)
        else:

            time_from = time_till - parse_duration(args.period)
        step = parse_step(args.step)
    except (ValueError, PeriodError) as exc:
        raise CliError(str(exc)) from None
    if time_from >= time_till:
        raise CliError("--from must be earlier than --till")
    macros = _parse_macros(args.macro)

    try:
        chunk_seconds = 0 if args.chunk.strip() == "0" else parse_duration(args.chunk)
    except (PeriodError, ValueError) as exc:
        raise CliError(f"--chunk: {exc}") from None
    if args.page_size < 1:
        raise CliError("--page-size must be positive")
    api = ZabbixAPI(args.url, token=args.token, verify=not args.insecure, timeout=args.timeout,
            page_size=args.page_size, chunk_seconds=chunk_seconds)
    reporter = Reporter(sys.stdout, tz, args.verbose)
    try:
        version = api.version()
        if _version_tuple(version) < MIN_VERSION:
            print(f"warning: Zabbix {version} detected, the tool targets 7.0+", file=sys.stderr)
        if not args.token:
            api.login(args.user, args.password)

        if args.trigger:
            trigger = api.get_trigger(args.trigger)
            if trigger.status != 0:
                print("warning: trigger is disabled in Zabbix", file=sys.stderr)
        else:
            trigger = _adhoc_trigger(args.expression, args.recovery_expression)

        overrides: dict[str, str] = {}
        expression_text = trigger.expanded_expression or trigger.expression
        recovery_text = trigger.expanded_recovery_expression or trigger.recovery_expression
        recovery_mode = trigger.recovery_mode
        if recovery_mode == 1 and not recovery_text:
            raise CliError("trigger uses recovery expression mode but has no recovery expression")

        expression_text = substitute_macros(expression_text, macros)
        recovery_text = substitute_macros(recovery_text, macros)
        try:
            expression = parse_expression(expression_text)
            recovery = parse_expression(recovery_text) if recovery_mode == 1 and recovery_text else None
        except ParseError as exc:
            raise CliError(f"cannot parse expression: {exc}") from None

        try:
            binding = bind(expression, recovery, trigger, api.find_item)
        except BindError as exc:
            raise CliError(str(exc)) from None
        if not trigger.triggerid:
            trigger.items = dict(binding.items)
            trigger.hosts = {item.hostid: item.host for item in binding.items.values()}

        if step == -1:
            step = DEFAULT_TIMER_STEP if binding.has_timer_functions else None

        series: dict[str, Series] = {}
        trends: dict[str, TrendSeries] = {}
        for itemid, req in binding.requirements.items():
            item = binding.items[itemid]
            if req.needs_history:
                start = time_from - req.lookback_seconds
                points = api.get_history(item, start, time_till)
                points += api.get_history_before(item, start - 1, req.lookback_count)
                series[itemid] = Series(item, points)
            if req.needs_trends:
                trends[itemid] = TrendSeries(item, api.get_trends(item, time_from - req.trend_lookback_seconds, time_till))

        initial_state = OK
        if args.initial_state == "problem":
            initial_state = PROBLEM
        elif args.initial_state == "auto" and trigger.triggerid:
            last_event = api.get_last_event_before(trigger.triggerid, time_from)
            if last_event is not None and last_event["value"] == 1:
                initial_state = PROBLEM

        events = api.get_trigger_events(trigger.triggerid, time_from, time_till) if args.compare_events else None
    except ZabbixAPIError as exc:
        hint = ""
        if "Not authorized" in exc.data and api.token:
            hint = (
                "\n  The API accepted the credentials but then rejected the Authorization header. "
                "Usually the web server strips it before PHP sees it: with Apache and php-fpm add "
                "`CGIPassAuth On` (or `SetEnvIf Authorization \"(.*)\" HTTP_AUTHORIZATION=$1`) "
                "to the Zabbix frontend configuration and reload Apache."
            )
        raise CliError(f"Zabbix API error: {exc}{hint}") from None
    finally:
        api.logout()

    reporter.header(trigger, binding, time_from, time_till, step, series, trends, overrides,
                    engine.describe() if engine is not None else "pure Python implementation (reduced fidelity)")
    empty = [binding.items[i].ref for i, s in series.items() if len(s) == 0]
    if empty:
        print(f"warning: no history for {', '.join(empty)}; check item history retention ({format_duration(now - time_from)} back)", file=sys.stderr)

    if engine is not None:
        engine.reset()
        for s_ in series.values():
            engine.load_series(s_)
        for t_ in trends.values():
            engine.load_trends(t_)
        native = NativeEvaluator(engine)
        native.register(binding.expression, _native_text(binding.expression, expression_text))
        if binding.recovery is not None:
            native.register(binding.recovery, _native_text(binding.recovery, recovery_text))
        try:
            native.check(binding.expression, time_from)
            if binding.recovery is not None:
                native.check(binding.recovery, time_from)
        except NativeParseError as exc:
            raise CliError(f"Zabbix engine rejected the expression: {exc}") from None
        evaluator = native
        engine_desc = engine.describe()
    else:
        evaluator = Evaluator(EvalContext(series=series, trends=trends, tz=tz))
        engine_desc = "pure Python implementation (reduced fidelity)"
    points = build_points(list(series.values()), time_from, time_till, step, binding.timer_seed)
    if not points:
        reporter.line()
        reporter.line("No evaluation points: items have no values in the period and no timer step is set.")
        return 1
    simulator = Simulator(
        binding.expression,
        binding.recovery,
        evaluator,
        recovery_mode=recovery_mode,
        multiple_problem_events=trigger.type == 1,
        initial_state=initial_state,
        problem_itemids=binding.problem_itemids,
        timer_in_expression=binding.timer_in_expression,
        timer_in_recovery=binding.timer_in_recovery,
    )
    result = simulator.run(points)

    reporter.timeline(result, time_from)
    reporter.summary(result, time_from, time_till)
    if events is not None:
        reporter.compare_events(result, events, tolerance=max(step or 0, 60))
    if args.json:
        write_json(args.json, build_json(trigger, binding, time_from, time_till, step, result, events, tz_name, engine_desc))
        reporter.line(f"\nJSON written to {args.json}")
    return 0


_FUNCID_RE = re.compile(r"\{\d+\}")


def _native_text(node, source: str) -> str:
    """Expression text for the server engine: the original text, unless it is the reduced {functionid} form."""
    if source and not _FUNCID_RE.search(source):
        return source
    return to_text(node)


def _adhoc_trigger(expression: str, recovery_expression: str | None) -> Trigger:
    """A trigger object for --expression runs: recovers by the recovery expression when one is given."""
    return Trigger(
        triggerid="",
        description="ad-hoc expression",
        expression=expression,
        recovery_expression=recovery_expression or "",
        expanded_expression=expression,
        expanded_recovery_expression=recovery_expression or "",
        recovery_mode=1 if recovery_expression else 0,
        type=0,
        priority=0,
        status=0,
    )


def _validate_args(args: argparse.Namespace) -> None:
    if bool(args.trigger) == bool(args.expression):
        raise CliError("specify either -t/--trigger or --expression, not both")
    if args.recovery_expression and not args.expression:
        raise CliError("--recovery-expression needs --expression")
    if args.expression and args.compare_events:
        raise CliError("--compare-events needs a real trigger (-t); an ad-hoc expression has no events")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _validate_args(args)
        return run(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
