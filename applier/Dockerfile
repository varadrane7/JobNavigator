# Applier: one Chromium + a Claude Code agent that fills job application forms through Playwright MCP.
# A separate service on purpose: the agent drives a browser with no access to JobNavigator's database,
# .env or files. JobNavigator sends it everything one application needs over HTTP.
FROM mcr.microsoft.com/playwright:v1.64.0-noble

# Xvfb gives Chromium a screen (headed browsers trip fewer bot checks than headless ones);
# x11vnc + noVNC let you watch the agent, or solve a captcha for it, from a browser tab.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify python3-venv fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Pinned: a layer cache would otherwise keep whatever version the first build found.
RUN npm install -g @anthropic-ai/claude-code@2.1.281 @playwright/mcp@0.0.82

COPY applier/requirements.txt /opt/applier/requirements.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r /opt/applier/requirements.txt

COPY applier/app /opt/applier/app
# The same Navigator extension the dashboard ships: structured autofill runs inside every page the agent opens.
COPY extension /opt/applier/extension
COPY applier/entrypoint.sh /opt/applier/entrypoint.sh
RUN chmod +x /opt/applier/entrypoint.sh /opt/applier/app/start-chrome.sh && mkdir -p /data /work && chown -R pwuser:pwuser /data /work

# Claude Code refuses some modes as root, and nothing here needs root.
USER pwuser
ENV PYTHONPATH=/opt/applier PYTHONUNBUFFERED=1 DISPLAY=:99 HOME=/home/pwuser
EXPOSE 8080 6080
CMD ["/opt/applier/entrypoint.sh"]
