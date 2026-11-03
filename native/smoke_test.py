"""Quick check of libzbxtester.so through ctypes."""
import ctypes
import os
import sys

lib = ctypes.CDLL(os.path.join(os.path.dirname(os.path.abspath(__file__)), "libzbxtester.so"))
lib.zt_zabbix_version.restype = ctypes.c_char_p
lib.zt_item_register.argtypes = [ctypes.c_uint64, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p]
lib.zt_item_add_value.argtypes = [ctypes.c_uint64, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
lib.zt_item_add_trend.argtypes = [ctypes.c_uint64, ctypes.c_int, ctypes.c_int, ctypes.c_double, ctypes.c_double, ctypes.c_double]
lib.zt_item_finish.argtypes = [ctypes.c_uint64]
lib.zt_evaluate_function.argtypes = [ctypes.c_uint64, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_char_p)]
lib.zt_evaluate_expression.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_char_p)]
lib.zt_free.argtypes = [ctypes.c_void_p]
lib.zt_set_debug.argtypes = [ctypes.c_int]

def func(itemid, name, params, sec, ns=0):
    out, err = ctypes.c_char_p(), ctypes.c_char_p()
    rc = lib.zt_evaluate_function(itemid, name.encode(), params.encode(), sec, ns, ctypes.byref(out), ctypes.byref(err))
    res = (out.value or err.value).decode()
    lib.zt_free(out)
    lib.zt_free(err)
    return ("OK " if rc == 0 else "ERR") + " " + res

def expr(text, sec, ns=0):
    res, err = ctypes.c_double(), ctypes.c_char_p()
    rc = lib.zt_evaluate_expression(text.encode(), sec, ns, ctypes.byref(res), ctypes.byref(err))
    msg = err.value.decode() if err.value else ""
    lib.zt_free(err)
    return f"{'OK ' if rc == 0 else 'ERR'} {res.value if rc == 0 else msg}"

if "-d" in sys.argv:
    lib.zt_set_debug(1)
print("zabbix", lib.zt_zabbix_version().decode())
T = 1_700_000_000
lib.zt_item_register(1, 0, b"srv", b"cpu")
for i, v in enumerate([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]):
    lib.zt_item_add_value(1, T + i * 60, 0, str(v).encode(), 0, b"", 0, 0)
print("values:", lib.zt_item_finish(1))
lib.zt_item_register(2, 1, b"srv", b"status")
for i, v in enumerate(["ok", "error: disk", "ERROR", "ok"]):
    lib.zt_item_add_value(2, T + i * 60, 0, v.encode(), 0, b"", 0, 0)
lib.zt_item_finish(2)
lib.zt_item_register(3, 0, b"srv", b"trend")
day = 1704844800
for h in range(24):
    lib.zt_item_add_trend(3, day - 86400 + h * 3600, 60, 1.0, 2.0 + h % 2, 5.0)

last = T + 9 * 60
tests = [
    ("last", "", last), ("last", "#3", last), ("last", "#1:now-5m", last), ("last", "#20", last),
    ("min", "5m", last), ("max", "5m", last), ("avg", "5m", last), ("sum", "#2", last), ("first", "5m", last),
    ("avg", "3m", T + 180),
    ("count", "10m", last), ("count", '10m,"gt","7"', last), ("count", '10m,"bitand","3/2"', last), ("count", '10m,"bitand","1"', last),
    ("percentile", "10m,50", last), ("percentile", "10m,100", last), ("stddevpop", "10m", last), ("mad", "10m", last),
    ("change", "", last), ("rate", "10m", last), ("monoinc", "10m", last), ("monoinc", '10m,"strict"', last),
    ("timeleft", "10m,100", last), ("forecast", "10m,1h", last), ("forecast", "10m,1h,\"polynomial2\"", last),
    ("nodata", "5m", last + 200), ("nodata", "5m", last + 301),
]
for name, params, sec in tests:
    print(f"  {name}(/srv/cpu,{params}) @+{sec - T:<5} -> {func(1, name, params, sec)}")
for name, params in [("count", '10m,"like","rror"'), ("count", '10m,"iregexp","^err"'), ("find", '#2,"like","ERROR"'), ("countunique", "10m"), ("change", "")]:
    print(f"  {name}(/srv/status,{params}) -> {func(2, name, params, T + 180)}")
print()
for e, sec in [
    ("avg(/srv/cpu,3m)>20 and last(/srv/cpu)=4", T + 180),
    ("abs(change(/srv/cpu))=1 and min(last(/srv/cpu),5)=4", T + 180),
    ("1K=1024 and 5m=300 and 0.1+0.2=0.3", T),
    ("1 or last(/srv/cpu,#50)>0", T),
    ("0 and last(/srv/cpu,#50)>0", T),
    ("1 and last(/srv/cpu,#50)>0", T),
    ("last(/srv/status)=\"error: disk\"", T + 60),
    ("last(/srv/status)>1", T + 60),
    ("1/0>1", T),
    ("trendavg(/srv/trend,1d:now/d)>2 and trendmax(/srv/trend,1d:now/d)=5", day + 7200),
    ("dayofweek()>0 and now()>0", T),
    ("last(/srv/nope)>0", T),
    ("{$MACRO}>0", T),
]:
    print(f"  {e} @+{sec - T} -> {expr(e, sec)}")
