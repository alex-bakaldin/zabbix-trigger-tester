#!/usr/bin/env bash
# Build libzbxtester.so from a configured Zabbix source tree.
#   ZABBIX_SRC=/path/to/zabbix ./build.sh
# The tree must be configured (./bootstrap.sh && ./configure --enable-agent --with-libpcre2)
# and the needed libs built with: make -C src/libs/<lib> CFLAGS="-fPIC -O2 -g"
set -euo pipefail
SRC=${ZABBIX_SRC:?set ZABBIX_SRC to the Zabbix source tree}
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$HERE/libzbxtester.so}
BUILD=$HERE/build
mkdir -p "$BUILD"
L=$SRC/src/libs
X=$L/zbxexpression

# The semantics we rely on are those of one specific release: refuse other trees unless asked.
TAG=${ZABBIX_TAG:-$(tr -d '[:space:]' < "$HERE/ZABBIX_TAG")}
ver() { sed -n "s/^#define ZABBIX_VERSION_$1[[:space:]]*\([0-9]*\).*/\1/p" "$SRC/include/version.h"; }
RC=$(sed -n 's/^#define ZABBIX_VERSION_RC[[:space:]]*"\(.*\)".*/\1/p' "$SRC/include/version.h")
SRC_VERSION="$(ver MAJOR).$(ver MINOR).$(ver PATCH)"
if [ "$SRC_VERSION$RC" != "$TAG" ] && [ -z "${ZT_ALLOW_VERSION_MISMATCH:-}" ]; then
    echo "error: source tree is Zabbix $SRC_VERSION$RC but native/ZABBIX_TAG pins $TAG" >&2
    echo "       (fetch_sources.sh checks out the pinned tag; set ZT_ALLOW_VERSION_MISMATCH=1 to override)" >&2
    exit 1
fi

INC="-I$SRC/include -I$SRC/include/common -I$X -I$L/zbxtrends $(pkg-config --cflags libpcre2-8)"
CFLAGS="-fPIC -O2 -g -DHAVE_CONFIG_H -DZT_SOURCE_TAG=\"$TAG\" $INC"

# trends.c: keep the pure helpers (parse_range, parse_timeshift) but rename the SQL based
# aggregate functions so that the in-memory versions from zbxtester.c are used instead.
RENAMES="-Dzbx_trends_eval_avg=zt_sql_trends_eval_avg -Dzbx_trends_eval_count=zt_sql_trends_eval_count \
         -Dzbx_trends_eval_max=zt_sql_trends_eval_max -Dzbx_trends_eval_min=zt_sql_trends_eval_min \
         -Dzbx_trends_eval_sum=zt_sql_trends_eval_sum -Dzbx_trends_get_avg=zt_sql_trends_get_avg"
gcc $CFLAGS $RENAMES -c "$L/zbxtrends/trends.c" -o "$BUILD/trends.o"

# zbx_timespec() is replaced by the virtual clock in zbxtester.c
objcopy --weaken-symbol=zbx_timespec "$L/zbxtime/libzbxtime.a" "$BUILD/libzbxtime_weak.a"

OBJS="$X/libzbxexpression_a-evalfunc.o $X/libzbxexpression_a-funcparam.o $X/libzbxexpression_a-anomalystl.o \
      $X/libzbxexpression_a-evalsimple.o $X/libzbxexpression_a-expression_names.o $BUILD/trends.o \
      $L/zbxtrends/libzbxtrends_a-baseline.o"
LIBS="$L/zbxeval/libzbxeval.a $L/zbxhistory/libzbxhistory.a $L/zbxexpr/libzbxexpr.a $L/zbxparam/libzbxparam.a \
      $L/zbxregexp/libzbxregexp.a $L/zbxvariant/libzbxvariant.a $L/zbxalgo/libzbxalgo.a $L/zbxjson/libzbxjson.a \
      $BUILD/libzbxtime_weak.a $L/zbxnum/libzbxnum.a $L/zbxstr/libzbxstr.a $L/zbxserialize/libzbxserialize.a \
      $L/zbxprof/libzbxprof.a $L/zbxcommon/libzbxcommon.a"

gcc $CFLAGS -fvisibility=hidden -shared -o "$OUT" -Wl,-z,defs "$HERE/zbxtester.c" $OBJS \
    -Wl,--start-group $LIBS ${EXTRA_LIBS:-} -Wl,--end-group $(pkg-config --libs libpcre2-8) -lm
echo "built $OUT from Zabbix $SRC_VERSION$RC (tag $TAG)"
