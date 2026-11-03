# trigger-tester

English | [Русский](README.ru.md)

Replay a Zabbix 7.0+ trigger against the history of its items and see when it
would have fired. The tool fetches the trigger expression through the API,
loads the history (and trends) of the referenced items for a time period and
evaluates the expression at every point in time the server would have
evaluated it, running the trigger state machine (problem / recovery
expressions, UNKNOWN handling). The result is a timeline of OK/PROBLEM
transitions, a summary, and optionally a comparison with the real events.

The expression engine is **the Zabbix server's own C code**, compiled into a
small shared library (`native/`), so operators, functions and edge cases behave
exactly as on the server. A pure-Python reimplementation, verified against the
native one function by function, serves as a fallback.

## Why

The only way to test a trigger in Zabbix is to wait for it to fire. The
frontend "Test" button only evaluates operators on numbers you type in. This
tool answers questions such as "would `min(/host/cpu,10m)>90` have been quieter
than `last()>90` last week?" in seconds, with the server's semantics.

## Install

```bash
uv sync                    # or: pip install -e .
uv run zbx-trigger-test --help
```

### Native engine (recommended)

```bash
native/fetch_sources.sh /tmp/zabbix-src      # clones the pinned Zabbix release and builds its libs
ZABBIX_SRC=/tmp/zabbix-src native/build.sh   # -> native/libzbxtester.so
```

Needs gcc, make, autoconf/automake, libtool, pkg-config and libpcre2
development headers. See [native/README.md](native/README.md) for details,
the pinned release (`native/ZABBIX_TAG`) and how to move to another one.
Without the library the tool falls back to the Python engine and says so in the
report header. The library is searched in `$ZBX_TRIGGER_TESTER_LIB`, next to
the package, and in `native/`.

## Usage

```bash
export ZABBIX_URL=https://zabbix.example.com
export ZABBIX_TOKEN=xxxxxxxx                 # API token, or --user/--password

# last 24 hours
zbx-trigger-test -t 23456

# explicit period, compare with real events, show every evaluation
zbx-trigger-test -t 23456 --from "2026-11-01 00:00" --till "2026-11-02 00:00" --compare-events -v

# what-if: an expression that is not a trigger yet (items are looked up by /host/key)
zbx-trigger-test --period 7d \
    --expression 'min(/srv1/system.cpu.util,10m)>{$CPU.MAX}' --macro '{$CPU.MAX}=85'

# ... with hysteresis: the ad-hoc trigger recovers by a recovery expression
zbx-trigger-test --period 7d --expression 'last(/srv1/system.cpu.util)>90' \
    --recovery-expression 'last(/srv1/system.cpu.util)<70'

# machine readable output for further analysis / visualisation
zbx-trigger-test -t 23456 --json result.json
```

| option | meaning |
|---|---|
| `-t/--trigger` | trigger ID |
| `--from`, `--till`, `--period` | period: ISO date/time, unix time, `now-1d`, `now/d` |
| `--chunk`, `--page-size` | history is fetched in time windows (default `6h`, `0` = whole period) and pages of at most N rows (default 10000) per `history.get`, so no single request is large |
| `--step` | interval of timer evaluations; `auto` = 30 s when either expression has `nodata()`, `date()/time()/now()/dayofweek()/dayofmonth()` or trend/baseline functions (time shifts alone do not make the server re-evaluate) |
| `--expression`, `--recovery-expression` | replay an ad-hoc expression instead of a trigger (Zabbix syntax with `/host/key` references); exactly one of `-t` and `--expression` must be given. A recovery expression is optional and switches the ad-hoc trigger to the "recovery expression" mode |
| `--macro {$NAME}=VALUE` | value for a user macro the API left unresolved |
| `--initial-state` | trigger state at the period start; `auto` uses the last event before the period |
| `--compare-events` | fetch the real events of the period and match them with the simulated transitions |
| `--engine` | `auto` / `native` / `python` |
| `--tz` | time zone for display and `now/d` rounding; use the Zabbix server's zone |
| `--json FILE` | full result: every evaluation with the value of each function |

Example output:

```
Trigger #555: High CPU on srv1
  hosts: srv1   severity: High   status: enabled
  recovery mode: expression   problem events: single   current value: OK
Expression:
  max(/srv1/system.cpu.util,2m) > 90
Items:
  [100] /srv1/system.cpu.util  (float, delay 1m)
      history: 489 values (480 in period, 9 lookback), 2026-11-01 23:58:00 .. 2026-11-02 08:00:00
Period: 2026-11-02 00:00:00 .. 2026-11-02 08:00:00 (8h), evaluation: on new values only
Engine: Zabbix 7.0.32 server code, tag 7.0.32 (.../native/libzbxtester.so)

Timeline:
  2026-11-02 00:00:00  OK       (initial state)
  2026-11-02 03:15:00  PROBLEM  max(/srv1/system.cpu.util,2m)=95.2
  2026-11-02 03:18:00  OK       max(/srv1/system.cpu.util,2m)=30.1

Summary:
  evaluations: 480, unknown: 0
  problem events: 1, recovery events: 1
  problem intervals: 1, total 3m (0.6% of period), longest 3m
  final state: OK
```

## How it works

1. `trigger.get` (functions, items, hosts) plus `trigger.get` with
   `expandExpression`, so Zabbix itself resolves `{functionid}` references and
   user macros.
2. The expression is parsed (operator precedence, suffixes, strings, macros) to
   find the items and how much history each function needs *before* the period
   (`5m` windows, `#N` counts, `:now-1h` shifts). History is fetched with
   `history.get` in pages, trends with `trend.get`.
3. Evaluation points are every new value of any involved item inside the
   period, plus a timer grid for time-based functions. At each point the
   trigger is recalculated the way `zbx_evaluate_expressions()` does it:
   - the problem expression is evaluated first; if it is true the trigger
     becomes PROBLEM, but only when the recalculation was started by a value
     of an item used in the problem expression or by a timer of that
     expression. A value of an item that appears only in the recovery
     expression, or a timer that exists only because of the recovery
     expression, can never raise PROBLEM (the report counts such moments as
     "problem results ignored");
   - the recovery expression is consulted only when the trigger is in
     PROBLEM, recovers by a recovery expression and the problem expression is
     false. Its errors matter only then;
   - timer points are evaluated when the problem expression has time-based
     functions, or while the trigger is in PROBLEM and its recovery expression
     has them. The grid is aligned like the server's (`30 * k + seed % 30`).
   Failed functions make the trigger UNKNOWN and keep its previous state; the
   three-valued logic (`1 or UNKNOWN = 1`, `0 and UNKNOWN = 0`) is the
   server's.

### Engines

- **native**: `libzbxeval` (parser, operators, math/string/date functions) and
  `evalfunc.c` (history and trend functions) from the pinned Zabbix release,
  with the value cache, trend SQL and configuration cache replaced by in-memory
  stores filled from Python. Time is virtual, so `nodata()` and `now()` see the
  evaluation moment.
- **python**: `trigger_tester.expression`, a transcription of the same server
  code (including `prediction.c` for `forecast()`/`timeleft()`).
  `native/diff_test.py` runs both engines on the same cases and must report
  0 mismatches; CI enforces that.

## Limitations

- Aggregate / foreach functions over wildcard item queries (`/*/key`,
  `?[tag=...]`), global regular expressions (`@name`), the strict mode of
  `nodata()` with proxy suppression windows.
- Python engine only: `trendstl()`, `jsonpath()`, `xmlpath()` are missing.
- How far back you can test is bounded by the history retention of the items.
- Trend and baseline functions are re-evaluated by the server on their own
  hourly schedule; the tester uses the 30 s grid for them too, so such
  transitions may show up earlier than on the server (never later).
- Values of several items that arrive with the same timestamp are evaluated
  once, together. The server may split them across history-syncer batches
  and evaluate the trigger twice, each time with a partial set of fresh
  values; such transient flaps cannot be reconstructed from the API.
- Real events can differ from the simulation because of maintenance periods,
  trigger dependencies, manual closing, correlation or edits to the trigger.
  `--compare-events` lists such mismatches explicitly.

## Development

```bash
uv sync
uv run pytest -q                                # native tests are skipped without the library
TZ=UTC uv run python native/diff_test.py        # native vs Python, expects "0 mismatches"
```

## License

GNU Affero General Public License v3 (see [LICENSE](LICENSE)). The native
engine links unmodified Zabbix source code, Copyright Zabbix SIA, which is AGPL-3.0
as well; see [NOTICE](NOTICE). The Zabbix sources are downloaded at build time
and are not part of this repository.
