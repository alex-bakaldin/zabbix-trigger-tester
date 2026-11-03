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

# what-if: another expression on the same items
zbx-trigger-test -t 23456 --period 7d \
    --expression 'min(/srv1/system.cpu.util,10m)>{$CPU.MAX}' --macro '{$CPU.MAX}=85'

# machine readable output for further analysis / visualisation
zbx-trigger-test -t 23456 --json result.json
```

| option | meaning |
|---|---|
| `-t/--trigger` | trigger ID |
| `--from`, `--till`, `--period` | period: ISO date/time, unix time, `now-1d`, `now/d` |
| `--step` | interval of timer evaluations; `auto` = 30 s when the trigger has `nodata()`, `now()`, trend functions or time shifts |
| `--expression`, `--recovery-expression` | override the expressions (Zabbix syntax with `/host/key` references) |
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
   expression (and the recovery expression) is evaluated by the engine and fed
   to the trigger state machine with the trigger's recovery mode and event
   generation mode. Failed functions make the trigger UNKNOWN and keep its
   previous state; the three-valued logic (`1 or UNKNOWN = 1`,
   `0 and UNKNOWN = 0`) is the server's.

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
- The moment of timer evaluations is approximated (server timers are not
  aligned), so transitions caused by `nodata()`-style functions may be off by
  up to the step.
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
