"""Console and JSON reporting of a simulation."""

from __future__ import annotations

import json
from datetime import datetime, tzinfo
from typing import Any, TextIO

from trigger_tester.binder import Binding
from trigger_tester.expression.ast import to_text
from trigger_tester.history import Series, TrendSeries
from trigger_tester.model import Trigger
from trigger_tester.simulator import OK, PROBLEM, Evaluation, SimulationResult
from trigger_tester.values import format_value, is_unknown


def format_ts(clock: int, tz: tzinfo | None) -> str:
    return datetime.fromtimestamp(clock, tz).strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    if seconds:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _functions_text(evaluation: Evaluation) -> str:
    seen = set()
    parts = []
    for text, value in evaluation.functions:
        if text in seen:
            continue
        seen.add(text)
        parts.append(f"{text}={format_value(value)}")
    return "  ".join(parts)


class Reporter:
    def __init__(self, out: TextIO, tz: tzinfo | None, verbose: int = 0) -> None:
        self.out = out
        self.tz = tz
        self.verbose = verbose

    def line(self, text: str = "") -> None:
        print(text, file=self.out)

    # ------------------------------------------------------------------
    def header(
        self,
        trigger: Trigger,
        binding: Binding,
        time_from: int,
        time_till: int,
        step: int | None,
        series: dict[str, Series],
        trends: dict[str, TrendSeries],
        overrides: dict[str, str],
        engine: str = "pure Python implementation (reduced fidelity)",
    ) -> None:
        host_names = ", ".join(sorted(trigger.hosts.values())) or "-"
        if trigger.triggerid:
            status = "enabled" if trigger.status == 0 else "DISABLED"
            self.line(f"Trigger #{trigger.triggerid}: {trigger.description}")
            self.line(f"  hosts: {host_names}   severity: {trigger.priority_name}   status: {status}")
            self.line(
                f"  recovery mode: {trigger.recovery_mode_name}   "
                f"problem events: {'multiple' if trigger.type == 1 else 'single'}   "
                f"current value: {'PROBLEM' if trigger.value == 1 else 'OK'}"
            )
        else:
            self.line("Ad-hoc expression (no trigger)")
            self.line(f"  hosts: {host_names}   recovery mode: {trigger.recovery_mode_name}   problem events: single")
        expr_label = "Expression (override)" if "expression" in overrides else "Expression"
        self.line(f"{expr_label}:")
        self.line(f"  {to_text(binding.expression)}")
        if binding.recovery is not None:
            rec_label = "Recovery expression (override)" if "recovery" in overrides else "Recovery expression"
            self.line(f"{rec_label}:")
            self.line(f"  {to_text(binding.recovery)}")
        self.line("Items:")
        for itemid, item in sorted(binding.items.items(), key=lambda kv: kv[1].ref):
            s = series.get(itemid)
            t = trends.get(itemid)
            flags = []
            if item.status != 0:
                flags.append("DISABLED")
            if item.state != 0:
                flags.append("NOT SUPPORTED")
            parts = [f"  [{itemid}] {item.ref}  ({item.value_type_name}, delay {item.delay or '?'})"]
            if flags:
                parts.append(" ".join(flags))
            self.line("  ".join(parts))
            if s is not None:
                if len(s):
                    in_period = len(s.in_range(time_from, time_till))
                    first, last = s.points[0].clock, s.points[-1].clock
                    self.line(
                        f"      history: {len(s)} values ({in_period} in period, {len(s) - in_period} lookback), "
                        f"{format_ts(first, self.tz)} .. {format_ts(last, self.tz)}"
                    )
                else:
                    self.line("      history: NO VALUES (retention expired or item never collected?)")
            if t is not None:
                self.line(f"      trends: {len(t)} hourly records")
        step_text = f"timer every {format_duration(step)}" if step else "on new values only"
        self.line(
            f"Period: {format_ts(time_from, self.tz)} .. {format_ts(time_till, self.tz)} "
            f"({format_duration(time_till - time_from)}), evaluation: {step_text}"
        )
        self.line(f"Engine: {engine}")
        for warning in binding.warnings:
            self.line(f"warning: {warning}")

    # ------------------------------------------------------------------
    def timeline(self, result: SimulationResult, time_from: int) -> None:
        self.line()
        if self.verbose:
            self.line("Evaluations (* = state change, ? = UNKNOWN):")
            for ev in result.evaluations:
                self._evaluation_line(ev)
            return
        self.line("Timeline:")
        self.line(f"  {format_ts(time_from, self.tz)}  {result.initial_state:<8} (initial state)")
        for ev in result.transitions:
            self._transition_line(ev)
        if not result.transitions:
            self.line("  (no state changes)")

    def _evaluation_line(self, ev: Evaluation) -> None:
        mark = "*" if ev.changed else ("?" if ev.unknown else " ")
        value = format_value(ev.value)
        extra = f" recovery={format_value(ev.recovery_value)}" if ev.recovery_value is not None else ""
        if ev.suppressed:
            extra += " [true, but PROBLEM not allowed for this recalculation]"
        source = "timer" if ev.source == "timer" else "value"
        self.line(f"{mark} {format_ts(ev.clock, self.tz)}  {ev.state:<8} [{source}] expr={value}{extra}  {_functions_text(ev)}")
        if ev.errors and self.verbose > 1:
            for error in ev.errors:
                self.line(f"      ! {error}")

    def _transition_line(self, ev: Evaluation) -> None:
        self.line(f"  {format_ts(ev.clock, self.tz)}  {ev.state:<8} {_functions_text(ev)}")

    # ------------------------------------------------------------------
    def summary(self, result: SimulationResult, time_from: int, time_till: int) -> None:
        self.line()
        intervals = result.problem_intervals(time_from, time_till)
        durations = [(end if end is not None else time_till) - start for start, end in intervals]
        total = sum(durations)
        self.line("Summary:")
        self.line(f"  evaluations: {len(result.evaluations)}, unknown: {result.unknown_count}")
        self.line(f"  problem events: {result.problem_events}, recovery events: {result.ok_events}")
        if result.suppressed_count:
            self.line(
                f"  problem results ignored: {result.suppressed_count} "
                "(recalculation started by a recovery-expression item or timer; the server does not raise PROBLEM then)"
            )
        if intervals:
            longest = max(durations)
            share = 100.0 * total / max(1, time_till - time_from)
            self.line(
                f"  problem intervals: {len(intervals)}, total {format_duration(total)} ({share:.1f}% of period), "
                f"longest {format_duration(longest)}"
            )
            for start, end in intervals:
                end_text = format_ts(end, self.tz) if end is not None else "(still open)"
                self.line(f"    {format_ts(start, self.tz)} .. {end_text}")
        else:
            self.line("  problem intervals: none")
        self.line(f"  final state: {result.final_state}")
        if result.error_counter:
            self.line("  errors (UNKNOWN causes):")
            for error, count in result.error_counter.most_common(5):
                self.line(f"    {count:>6}x {error}")

    # ------------------------------------------------------------------
    def compare_events(self, result: SimulationResult, events: list[dict], tolerance: int) -> None:
        self.line()
        self.line(f"Actual events from Zabbix ({len(events)}) vs simulated state changes ({len(result.transitions)}):")
        if not events and not result.transitions:
            self.line("  nothing to compare")
            return
        remaining = list(result.transitions)
        missed = 0
        for event in events:
            state = PROBLEM if event["value"] == 1 else OK
            match = None
            for ev in remaining:
                if ev.state == state and abs(ev.clock - event["clock"]) <= tolerance:
                    match = ev
                    break
            if match is not None:
                remaining.remove(match)
                delta = match.clock - event["clock"]
                delta_text = "" if delta == 0 else f" (simulated {delta:+d}s)"
                self.line(f"  OK     {format_ts(event['clock'], self.tz)}  {state:<8} event #{event['eventid']}{delta_text}")
            else:
                missed += 1
                self.line(f"  MISSED {format_ts(event['clock'], self.tz)}  {state:<8} event #{event['eventid']} not reproduced")
        for ev in remaining:
            self.line(f"  EXTRA  {format_ts(ev.clock, self.tz)}  {ev.state:<8} simulated, no matching event")
        self.line(f"  matched: {len(events) - missed}, missed: {missed}, extra: {len(remaining)}")
        if missed or remaining:
            self.line(
                "  note: mismatches can come from maintenance, manual close, dependencies, expired history,"
                " trigger edits or a different initial state."
            )


def _json_value(value: Any) -> Any:
    if value is None or is_unknown(value):
        return None
    if isinstance(value, float) and value == 1.797693134862316e308:
        return "inf"
    return value


def build_json(
    trigger: Trigger,
    binding: Binding,
    time_from: int,
    time_till: int,
    step: int | None,
    result: SimulationResult,
    events: list[dict] | None,
    tz_name: str,
    engine: str = "python",
) -> dict:
    return {
        "engine": engine,
        "trigger": {
            "triggerid": trigger.triggerid or None,
            "description": trigger.description,
            "expression": to_text(binding.expression),
            "recovery_expression": to_text(binding.recovery) if binding.recovery is not None else None,
            "recovery_mode": trigger.recovery_mode,
            "type": trigger.type,
            "priority": trigger.priority,
            "hosts": trigger.hosts,
        },
        "items": {
            itemid: {
                "host": item.host,
                "key": item.key_,
                "name": item.name,
                "value_type": item.value_type_name,
                "units": item.units,
            }
            for itemid, item in binding.items.items()
        },
        "period": {"from": time_from, "till": time_till, "step": step, "tz": tz_name},
        "initial_state": result.initial_state,
        "final_state": result.final_state,
        "problem_intervals": [{"from": s, "till": e} for s, e in result.problem_intervals(time_from, time_till)],
        "evaluations": [
            {
                "clock": ev.clock,
                "ns": ev.ns,
                "source": ev.source,
                "itemids": list(ev.itemids),
                "value": _json_value(ev.value),
                "recovery_value": _json_value(ev.recovery_value),
                "suppressed": ev.suppressed,
                "state": ev.state,
                "unknown": ev.unknown,
                "event": ev.event,
                "errors": ev.errors,
                "functions": {text: _json_value(v) for text, v in ev.functions},
            }
            for ev in result.evaluations
        ],
        "events": events,
    }


def write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
