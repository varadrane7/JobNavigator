# Applier

Fills one job application at a time: a Chromium on a virtual screen, driven by Claude Code through the
Playwright MCP tools. Started by `docker-compose.autoapply.yml`; JobNavigator calls it from
`backend/autoapply/applier_client.py`. See docs/AUTOAPPLY.md, "The applier".

Environment: `CLAUDE_CODE_OAUTH_TOKEN` (required), `APPLIER_MODEL` (default `sonnet`), `APPLIER_TOKEN`
(bearer token for the API), `APPLIER_VNC_PASSWORD` (live view on port 6080, `/applier/vnc.html` via Caddy).

Try one by hand from the backend container:

```
curl -s -XPOST http://applier:8080/runs -H 'content-type: application/json' \
  -d '{"url":"https://job-boards.greenhouse.io/acme/jobs/1","dry_run":true,"applicant":"Name: ..."}'
curl -s http://applier:8080/runs/<id> | jq '.state, .steps, .result'
```
