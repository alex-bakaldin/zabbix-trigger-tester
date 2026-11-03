# Native evaluation engine (Zabbix server code)

`libzbxtester.so` wraps the real Zabbix server expression engine so the tester
evaluates trigger expressions exactly like the server does:

- `libzbxeval` — parser, operators, three-valued UNKNOWN logic, math/string/date functions;
- `zbxexpression/evalfunc.c` — all history functions (`last`, `avg`, `count`, `percentile`,
  `forecast`, `timeleft`, `trend*`, ...).

Only the data layer is replaced (`zbxtester.c`): the value cache (`zbx_vc_get_values`),
trend SQL aggregates (`zbx_trends_eval_*`), the configuration cache lookups used by
`nodata()` and the clock (`zbx_timespec`) read from in-memory stores filled from Python.
Zabbix is AGPL-3.0; this directory is distributed under the same license.

## Build

The library is pinned to one Zabbix release: the tag in [`ZABBIX_TAG`](ZABBIX_TAG).
`fetch_sources.sh` clones exactly that tag (sparse, without `ui/`), runs `configure`
and builds the static libraries; `build.sh` refuses a source tree of another version.

```bash
./fetch_sources.sh /tmp/zabbix-src          # ~3 minutes, needs network
ZABBIX_SRC=/tmp/zabbix-src ./build.sh       # -> libzbxtester.so
```

Requirements: gcc, make, autoconf/automake, libtool, pkg-config, libpcre2 (dev headers).

To move to another release: change `ZABBIX_TAG`, re-run both scripts, run
`uv run pytest` and `uv run python native/diff_test.py` and read the Zabbix
changelog for `evalfunc.c` / `zbxeval` changes. `ZT_ALLOW_VERSION_MISMATCH=1`
builds from a different tree anyway (for experiments only). The tag is compiled
into the library and printed in the report header.

## Check

```bash
python3 smoke_test.py          # functions and expressions on synthetic history
uv run python diff_test.py     # server engine vs the pure-Python implementation
```

## Exported API (ctypes)

| function | purpose |
|---|---|
| `zt_item_register(itemid, value_type, host, key)` | declare an item |
| `zt_item_add_value(itemid, sec, ns, value, log_ts, log_source, log_severity, log_eventid)` | add a history value (strings for all types) |
| `zt_item_add_trend(itemid, clock, num, min, avg, max)` | add an hourly trend record |
| `zt_item_finish(itemid)` | sort values, returns their count |
| `zt_evaluate_function(itemid, name, params, sec, ns, &value, &error)` | one history function at a timestamp |
| `zt_evaluate_expression(text, sec, ns, &result, &error)` | whole trigger expression; 0 = value, 1 = UNKNOWN with error, 2 = parse error |
| `zt_set_now(sec, ns)` | virtual clock (set automatically by the evaluate calls) |
| `zt_reset()`, `zt_free(ptr)`, `zt_set_debug(flag)`, `zt_zabbix_version()` | housekeeping |

## Known gaps

- `nodata()` strict mode and proxy "nodata windows" need the configuration cache; items are
  treated as server-monitored with data expected from the beginning of time.
- Global regular expressions (`@name`) resolve to nothing.
- Wildcard / filtered item queries (`/*/key`, `?[tag=...]`) are rejected.

`trendstl()` and `baselinewma()`/`baselinedev()` work: the STL code and `baseline.c` are
linked as is, only their trend reads go to the in-memory store.
