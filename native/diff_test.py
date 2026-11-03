"""Differential test: Zabbix server engine (libzbxtester.so) vs the pure-Python implementation."""
import ctypes
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))

from conftest import make_item, make_series  # noqa: E402
from test_functions import run as py_run  # noqa: E402
from trigger_tester.expression.functions import FunctionError  # noqa: E402
from trigger_tester.expression.period import PeriodError  # noqa: E402
from trigger_tester.history import Point, Series, TrendPoint, TrendSeries  # noqa: E402

lib = ctypes.CDLL(os.path.join(HERE, "libzbxtester.so"))
lib.zt_item_register.argtypes = [ctypes.c_uint64, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p]
lib.zt_item_add_value.argtypes = [ctypes.c_uint64, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
lib.zt_item_finish.argtypes = [ctypes.c_uint64]
lib.zt_evaluate_function.argtypes = [ctypes.c_uint64, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_char_p)]
lib.zt_free.argtypes = [ctypes.c_void_p]


def c_run(itemid, name, params, sec, ns=0):
    out, err = ctypes.c_char_p(), ctypes.c_char_p()
    rc = lib.zt_evaluate_function(itemid, name.encode(), params.encode(), sec, ns, ctypes.byref(out), ctypes.byref(err))
    text = (out.value if rc == 0 else err.value).decode()
    lib.zt_free(out)
    lib.zt_free(err)
    return ("ok", text) if rc == 0 else ("err", text)


def load(series):
    item = series.item
    lib.zt_item_register(int(item.itemid), item.value_type, item.host.encode(), item.key_.encode())
    for p in series.points:
        meta = p.meta or {}
        lib.zt_item_add_value(int(item.itemid), p.clock, p.ns, str(p.value).encode(), meta.get("timestamp", 0),
                              (meta.get("source") or "").encode(), meta.get("severity", 0), meta.get("logeventid", 0))
    lib.zt_item_finish(int(item.itemid))


def py(series, name, params, sec):
    period, _, rest = params.partition(",")
    args = []
    if rest:
        import csv
        args = next(csv.reader([rest], skipinitialspace=True))
    try:
        value = py_run(name, series, period or None, args, clock=sec)
        return ("ok", str(value))
    except (FunctionError, PeriodError, ValueError, KeyError) as exc:
        return ("err", str(exc))


def same(a, b):
    if a[0] != b[0]:
        return False
    if a[0] == "err":
        return True
    try:
        fa, fb = float(a[1]), float(b[1])
        return abs(fa - fb) <= 1e-6 * max(1.0, abs(fa), abs(fb))
    except ValueError:
        return a[1].strip('"') == b[1].strip('"')


START = 1_700_000_000
cpu = make_series(make_item(itemid="1"), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], start=START)
counter = make_series(make_item(itemid="2", key="net"), [100, 160, 220, 20], start=START)
bits = make_series(make_item(itemid="3", key="bits", value_type=3), [2, 6, 10, 3], start=START)
status = make_series(make_item(itemid="4", key="status", value_type=1), ["ok", "error: disk", "ERROR", "ok"], start=START)
stat = make_series(make_item(itemid="5", key="stat"), [2, 4, 4, 4, 5, 5, 7, 9], start=START)
flat = make_series(make_item(itemid="6", key="flat"), [5, 5, 5, 5], start=START)
same_sec = Series(make_item(itemid="7", key="burst"), [Point(START, 100, 1.0), Point(START, 200, 2.0), Point(START + 60, 0, 3.0)])
log = Series(make_item(itemid="8", key="log", value_type=2), [
    Point(START, 0, "boot", {"timestamp": START, "source": "kernel", "severity": 2, "logeventid": 100}),
    Point(START + 60, 0, "fail", {"timestamp": START, "source": "sshd", "severity": 4, "logeventid": 4625}),
])
for s in (cpu, counter, bits, status, stat, flat, same_sec, log):
    load(s)

L = START + 540
cases = [
    (cpu, "last", "", L), (cpu, "last", "#3", L), (cpu, "last", "#1:now-5m", L), (cpu, "last", "#20", L),
    (cpu, "last", "", START - 1), (cpu, "first", "5m", L), (cpu, "first", "1s", L),
    (cpu, "min", "5m", L), (cpu, "max", "5m", L), (cpu, "avg", "5m", L), (cpu, "sum", "#2", L), (cpu, "avg", "3m", START + 180),
    (cpu, "min", "300", L), (cpu, "min", "5m:now-5m", L), (cpu, "min", "#3:now-1m", L), (cpu, "avg", "0", L), (cpu, "avg", "#0", L),
    (cpu, "count", "10m", L), (cpu, "count", '10m,"gt","7"', L), (cpu, "count", '10m,"le","2"', L), (cpu, "count", '#4,"eq","9"', L),
    (cpu, "count", '10m,"ne","1"', L), (cpu, "count", '10m,"like","1"', L), (cpu, "count", '10m,"regexp","^1"', L),
    (cpu, "count", '10m,,"5"', L), (cpu, "count", '10m,"gt"', L),
    (bits, "count", '10m,"bitand","3/2"', L), (bits, "count", '10m,"bitand","2"', L), (bits, "count", '10m,"bitand","1"', L),
    (cpu, "countunique", "10m", L), (cpu, "find", '#2,"like","9"', L), (cpu, "find", '10m,"gt","9"', L), (cpu, "find", '#1,"eq","10"', L),
    (cpu, "percentile", "10m,50", L), (cpu, "percentile", "10m,100", L), (cpu, "percentile", "10m,0", L), (cpu, "percentile", "10m,95", L),
    (stat, "percentile", "10m,50", L), (stat, "stddevpop", "10m", L), (stat, "stddevsamp", "10m", L), (stat, "varpop", "10m", L),
    (stat, "varsamp", "10m", L), (stat, "sumofsquares", "#2", L), (stat, "mad", "10m", L), (stat, "kurtosis", "10m", L), (stat, "skewness", "10m", L),
    (stat, "monoinc", "10m", L), (stat, "monoinc", '10m,"strict"', L), (stat, "monodec", "10m", L), (cpu, "monoinc", "#1", L),
    (cpu, "change", "", L), (cpu, "change", "", START), (counter, "rate", "10m", L), (counter, "rate", "#4", L), (cpu, "rate", "1m", L),
    (cpu, "timeleft", "10m,100", L), (cpu, "timeleft", "10m,0", L), (flat, "timeleft", "10m,100", L), (cpu, "timeleft", "10m,10", L),
    (cpu, "forecast", "10m,1h", L), (cpu, "forecast", "10m,1h,,\"max\"", L), (cpu, "forecast", '10m,1h,"linear","delta"', L),
    (cpu, "forecast", '10m,1h,"polynomial2"', L), (cpu, "forecast", '10m,1h,"exponential"', L),
    (cpu, "nodata", "5m", L + 200), (cpu, "nodata", "5m", L + 301), (cpu, "nodata", "5m", L + 300), (cpu, "nodata", "30s", L), (cpu, "nodata", "5m", START - 1000),
    (cpu, "fuzzytime", "60", L), (cpu, "stddevpop", "#1", L),
    (status, "count", '10m,"like","rror"', L), (status, "count", '10m,"iregexp","^err"', L), (status, "count", '10m,"eq","ok"', L),
    (status, "count", '10m,"gt","a"', L), (status, "find", '#2,"like","ERROR"', L), (status, "find", ',"like","ERROR"', L),
    (status, "countunique", "10m", L), (status, "last", "", L), (status, "change", "", L), (status, "min", "10m", L),
    (same_sec, "last", "", START), (same_sec, "count", "1m", START + 60), (same_sec, "count", "60", START + 60), (same_sec, "avg", "#2", START),
    (log, "logseverity", "", START + 60), (log, "logsource", ',"^ssh"', START + 60), (log, "logeventid", ',"^4625$"', START + 60),
    (log, "logeventid", '#2,"^4625$"', START + 60), (log, "count", '10m,"like","fail"', START + 60),
    (cpu, "sum", "1s", L), (cpu, "sum", "1s:now-30s", L), (cpu, "forecast", "1s:now-30s,1h", L), (cpu, "last", "5m", L), (cpu, "last", "5m:now-1m", L), (cpu, "count", '10m,"ge","1K"', L),
    (bits, "bitand", "#1,2", L), (bits, "bitand", "#2,8", L), (bits, "changecount", "10m", L), (bits, "changecount", '10m,"dec"', L),
    (cpu, "changecount", '10m,"inc"', L), (status, "changecount", "10m", L), (cpu, "changecount", "#1", L),
    (cpu, "forecast", '10m,1h,"logarithmic"', L), (cpu, "forecast", '10m,1h,"power","avg"', L), (cpu, "forecast", '10m,1h,"polynomial3","max"', L),
    (cpu, "forecast", '10m,1h,"polynomial2","avg"', L), (cpu, "forecast", '10m,1h,"exponential","delta"', L), (cpu, "forecast", "1s,1h", L),
    (cpu, "timeleft", '10m,100,"polynomial2"', L), (cpu, "timeleft", '10m,100,"exponential"', L), (cpu, "timeleft", '10m,100,"logarithmic"', L),
    (cpu, "timeleft", '10m,0,"exponential"', L), (cpu, "timeleft", "#1,100", L), (stat, "forecast", '10m,30m,"polynomial4","min"', L),
    (cpu, "percentile", "10m,50K", L), (cpu, "count", '10m,"eq","5"', L), (cpu, "nodata", "#5", L), (cpu, "fuzzytime", "#1", L),
    (cpu, "monoinc", '10m,"weak"', L), (cpu, "monoinc", '10m,"bad"', L), (same_sec, "rate", "#3", START + 60),
]
# trend based functions share one item with 5 weeks of hourly trends
DAY = 1704844800  # Wed 2024-01-10 00:00 UTC
trend_item = make_item(itemid="9", key="trend")
trends = TrendSeries(trend_item, [TrendPoint(DAY + h * 3600, 60, 5.0, 10 + (h % 24) + 100 * ((h // 24) % 7 == 2), 200.0) for h in range(-24 * 35, 3)])
lib.zt_item_register(9, 0, b"srv", b"trend")
for tp in trends.points:
    lib.zt_item_add_trend.argtypes = [ctypes.c_uint64, ctypes.c_int, ctypes.c_int, ctypes.c_double, ctypes.c_double, ctypes.c_double]
    lib.zt_item_add_trend(9, tp.clock, tp.num, tp.value_min, tp.value_avg, tp.value_max)
T2 = DAY + 2 * 3600 + 30
trend_cases = [
    ("trendavg", "1d:now/d"), ("trendmin", "1d:now/d"), ("trendmax", "1w:now/w"), ("trendsum", "2h:now/h"), ("trendcount", "1M:now/M"),
    ("trendavg", "1M:now/M-1M"), ("trendavg", "1d:now/d-1d"), ("trendavg", "1h:now-1d/h"), ("trendavg", "1d"), ("trendavg", "1d:now/h"),
    ("baselinewma", '1h:now/h,"d",3'), ("baselinedev", '1h:now/h,"d",3'), ("baselinewma", '1d:now/d,"w",2'), ("baselinedev", '1d:now/d,"w",4'),
    ("baselinewma", '1h:now/h,"d",0'), ("baselinewma", '1w:now/w,"M",1'), ("baselinedev", '1h:now/h,"h",5'), ("baselinewma", '1d:now/d,"M",1'),
]


def py_trend(name, params, sec):
    from trigger_tester.expression.ast import HistoryCall, ItemRef
    from trigger_tester.expression.functions import HISTORY_FUNCTIONS, Frame
    from trigger_tester.expression.period import Period
    import csv
    period, _, rest = params.partition(",")
    args = next(csv.reader([rest], skipinitialspace=True)) if rest else []
    call = HistoryCall(name, ItemRef("srv", "trend"), period, args, name, item=trend_item)
    try:
        call.period = Period.parse(period)
        return ("ok", str(HISTORY_FUNCTIONS[name].impl(Frame(call, None, trends, sec, 0, None))))
    except (FunctionError, PeriodError, ValueError) as exc:
        return ("err", str(exc))


mismatches = 0
for name, params in trend_cases:
    c = c_run(9, name, params, T2)
    p = py_trend(name, params, T2)
    flag = "  " if same(c, p) else "!!"
    mismatches += flag == "!!"
    print(f"{flag} {name}(/srv/trend,{params}) @trend".ljust(55) + f" C: {c[0]} {c[1][:70]:<72} PY: {p[0]} {p[1][:60]}")
cases_total = len(trend_cases)
for series, name, params, sec in cases:
    c = c_run(int(series.item.itemid), name, params, sec)
    p = py(series, name, params, sec)
    flag = "  " if same(c, p) else "!!"
    if flag == "!!":
        mismatches += 1
    label = f"{name}(/{series.item.host}/{series.item.key_},{params}) @{sec - START:+d}"
    print(f"{flag} {label:<52} C: {c[0]} {c[1][:70]:<72} PY: {p[0]} {p[1][:60]}")
print(f"\n{len(cases) + cases_total} cases, {mismatches} mismatches")
