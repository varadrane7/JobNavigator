#!/usr/bin/env bash
# Start a dedicated Chrome profile with DevTools on 127.0.0.1:9222 and expose it to the
# JobNavigator backend container on the docker bridge (default 172.17.0.1:9223).
#
#   backend/autoapply/tools/start-chrome-cdp.sh
#
# Log in to LinkedIn / ZipRecruiter once in the window that opens; the profile keeps the session.
# Chrome 136+ refuses remote debugging on your default profile, hence the separate one.
# Stop with Ctrl-C (closes the bridge; the Chrome window stays until you close it).
set -euo pipefail

PORT="${CDP_PORT:-9222}"
BRIDGE_PORT="${CDP_BRIDGE_PORT:-9223}"
BRIDGE_IP="${CDP_BRIDGE_IP:-$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)}"
BRIDGE_IP="${BRIDGE_IP:-172.17.0.1}"
FLAGS=(--remote-debugging-port="$PORT" --no-first-run --no-default-browser-check)

if command -v google-chrome-stable >/dev/null; then
  CHROME=(google-chrome-stable); PROFILE="$HOME/.chrome-autoapply"
elif command -v google-chrome >/dev/null; then
  CHROME=(google-chrome); PROFILE="$HOME/.chrome-autoapply"
elif flatpak info com.google.Chrome >/dev/null 2>&1; then
  CHROME=(flatpak run com.google.Chrome); PROFILE="$HOME/.var/app/com.google.Chrome/autoapply-profile"
elif command -v chromium >/dev/null; then
  CHROME=(chromium); PROFILE="$HOME/.chromium-autoapply"
elif flatpak info org.chromium.Chromium >/dev/null 2>&1; then
  CHROME=(flatpak run org.chromium.Chromium); PROFILE="$HOME/.var/app/org.chromium.Chromium/autoapply-profile"
else
  echo "No Chrome or Chromium found (native or flatpak)." >&2; exit 1
fi
mkdir -p "$PROFILE"

if ! curl -s -m 1 "http://127.0.0.1:$PORT/json/version" >/dev/null; then
  echo "Starting ${CHROME[*]} with profile $PROFILE"
  "${CHROME[@]}" "${FLAGS[@]}" --user-data-dir="$PROFILE" >/dev/null 2>&1 &
  for _ in $(seq 1 30); do curl -s -m 1 "http://127.0.0.1:$PORT/json/version" >/dev/null && break; sleep 0.5; done
fi
curl -s -m 2 "http://127.0.0.1:$PORT/json/version" | grep -q webSocketDebuggerUrl \
  || { echo "DevTools did not come up on 127.0.0.1:$PORT" >&2; exit 1; }

command -v socat >/dev/null || { echo "socat is required (dnf install socat)" >&2; exit 1; }
echo "Bridging $BRIDGE_IP:$BRIDGE_PORT -> 127.0.0.1:$PORT  (set autoapply_cdp_url = http://host.docker.internal:$BRIDGE_PORT)"
exec socat "TCP-LISTEN:$BRIDGE_PORT,bind=$BRIDGE_IP,fork,reuseaddr" "TCP:127.0.0.1:$PORT"
