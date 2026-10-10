#!/bin/sh
# Screen, browser, optional live view, then the API. The browser is long-lived so ATS logins
# (Workday accounts and the like) survive between applications in /data/profile.
set -e
Xvfb :99 -screen 0 1366x900x24 -nolisten tcp &
sleep 1

CHROME=$(ls -d /ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1)
[ -n "$CHROME" ] || { echo "no chromium under /ms-playwright" >&2; exit 1; }
echo "$CHROME" > /tmp/chrome-path
# Restarted by the API's watchdog if it dies (see app/browser.py).
/opt/applier/app/start-chrome.sh &

if [ -n "$APPLIER_VNC_PASSWORD" ]; then
  mkdir -p "$HOME/.vnc"
  x11vnc -storepasswd "$APPLIER_VNC_PASSWORD" "$HOME/.vnc/passwd" >/dev/null
  x11vnc -display :99 -forever -shared -rfbauth "$HOME/.vnc/passwd" -rfbport 5900 -quiet -bg -o /tmp/x11vnc.log
  websockify --web /usr/share/novnc 6080 127.0.0.1:5900 >/tmp/websockify.log 2>&1 &
  echo "live view on :6080/vnc.html"
else
  echo "APPLIER_VNC_PASSWORD unset: live view off"
fi

exec /opt/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
