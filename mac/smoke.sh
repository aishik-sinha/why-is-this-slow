#!/bin/bash
# After ./build_app.sh: the app starts the recorder, and a crashed app leaves no recorder behind.
# Uses a throwaway database, never your recorded history.
set -euo pipefail
cd "$(dirname "$0")/.."
APP="build/Why is this slow?.app"
URL=http://127.0.0.1:8765/api/live
REC="Resources/slow.py record"

fail() { echo "smoke FAILED: $1" >&2; exit 1; }
within() { local n=$1; shift; for _ in $(seq "$n"); do if "$@"; then return 0; fi; sleep 1; done; return 1; }
up() { [[ "$(curl -sf -m 2 "$URL")" == *'"samples"'* ]]; }
gone() { ! pgrep -f "$REC" >/dev/null && ! curl -s -m 2 "$URL" >/dev/null && [ -z "$(lsof -nP -iTCP:8765 -sTCP:LISTEN)" ]; }

if curl -s -m 2 "$URL" >/dev/null; then fail "port 8765 already answers; stop that recorder first"; fi

export SLOW_DB="$(mktemp -d)/slow.db"
# a folder scan that is already fresh, so the recorder starts no `du` of ~/Library that would outlive this test
/usr/bin/python3 -B -c 'import slow, time; db = slow.connect(); db.execute("INSERT INTO folder VALUES(?, ?, 0)", (time.time(), "seed")); db.commit()'

"$APP/Contents/MacOS/WhyIsThisSlow" &
app=$!
rec=
trap 'kill -9 $app $rec 2>/dev/null || true' EXIT

within 30 up || fail "the dashboard never answered"
rec=$(pgrep -f "$REC") || fail "no recorder process"
[ "$(ps -o ppid= -p "$rec" | tr -d ' ')" = "$app" ] || fail "the recorder's parent is not the app"

kill -9 "$app"  # simulated crash
wait "$app" 2>/dev/null || true
within 10 gone || fail "the recorder outlived the app"
echo "smoke OK"
