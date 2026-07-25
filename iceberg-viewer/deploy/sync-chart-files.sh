#!/bin/sh
# Refresh the backend sources embedded in the Helm chart (files/ must track
# mock-backend-py/). Run after changing the backend; CI-style check: --check.
set -e
cd "$(dirname "$0")"
SRC=../mock-backend-py
DST=helm/iceberg-ui-mock/files
if [ "$1" = "--check" ]; then
    for f in server.py data.py webjson.py; do
        cmp -s "$SRC/$f" "$DST/$f" || { echo "STALE: $DST/$f differs from $SRC/$f — run deploy/sync-chart-files.sh"; exit 1; }
    done
    echo "OK: chart files match mock-backend-py"
else
    cp "$SRC"/server.py "$SRC"/data.py "$SRC"/webjson.py "$DST"/
    echo "Synced server.py data.py webjson.py -> $DST"
fi
