#!/usr/bin/env bash
# Fetch the Zabbix sources at the pinned tag (native/ZABBIX_TAG) and prepare the static
# libraries needed by build.sh.
#   ./fetch_sources.sh /path/where/to/clone        (ZABBIX_TAG=x.y.z overrides the pin)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
TAG=${ZABBIX_TAG:-$(tr -d '[:space:]' < "$HERE/ZABBIX_TAG")}
DEST=${1:?usage: fetch_sources.sh <destination directory>}
REPO=${ZABBIX_REPO:-https://github.com/zabbix/zabbix.git}
LIBS="zbxcommon zbxalgo zbxvariant zbxnum zbxstr zbxregexp zbxexpr zbxparam zbxtime zbxjson zbxeval \
      zbxexpression zbxtrends zbxhistory zbxserialize zbxprof"

if [ ! -d "$DEST/.git" ]; then
    echo "cloning zabbix $TAG into $DEST"
    git clone -q --depth 1 --filter=blob:none --sparse --branch "$TAG" "$REPO" "$DEST"
    git -C "$DEST" sparse-checkout set --no-cone '/*' '!/ui'
fi
cd "$DEST"
echo "configuring"
./bootstrap.sh > bootstrap.log 2>&1
./configure --enable-agent --with-libpcre2 > configure.log 2>&1
for lib in $LIBS; do
    echo "building $lib"
    make -s -C "src/libs/$lib" CFLAGS="-fPIC -O2 -g" -j"$(nproc)" > "src/libs/$lib/build.log" 2>&1
done
echo "sources ready: $DEST (tag $TAG)"
