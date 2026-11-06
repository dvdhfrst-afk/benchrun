#!/bin/sh
# One-command check: unit tests, example plan validation, and an end-to-end run.
set -e
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1 && python3 -c "" >/dev/null 2>&1; then
    PY=python3
else
    PY=python
fi
"$PY" -m unittest discover
for plan in examples/*.json; do
    case "$plan" in
        *station*) ;;
        *) "$PY" -m benchrun validate "$plan" ;;
    esac
done
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
"$PY" -m benchrun run examples/regulator_line_load.json --serial CHECK-001 \
    --sim --db "$TMP/check.sqlite" --report-dir "$TMP/reports"
"$PY" -m benchrun history --serial CHECK-001 --db "$TMP/check.sqlite"
echo "check.sh: all checks passed"
