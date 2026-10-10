#!/bin/sh
# One Chromium with a persistent profile and a DevTools port that only this container can reach.
exec "$(cat /tmp/chrome-path)" \
  --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \
  --user-data-dir=/data/profile --no-first-run --no-default-browser-check \
  --no-sandbox --disable-dev-shm-usage --window-size=1366,900 --window-position=0,0 \
  --disable-features=Translate,MediaRouter --password-store=basic \
  about:blank >/tmp/chrome.log 2>&1
