# AutoApply

Self-hosted auto-apply pipeline. Everything lives in `backend/autoapply/`, `frontend/src/autoapply/` and
`backend/tests/autoapply/`; upstream files carry only one-line hooks (`main.py`, `scheduler.py`,
`App.jsx`, `Shell.jsx`).

```
scrape -> drop job boards/aggregators -> score vs base résumés -> rank, top N/day
       -> tailor summary + skills -> (cover letter) -> review gate
       -> applier service fills + submits (paced) -> Applications tracker
```

Only postings on a company's own ATS or careers site are queued (Greenhouse, Lever, Ashby, Workday, any
other company site). Jobs whose URL is on LinkedIn, ZipRecruiter, Indeed, Glassdoor, Dice, freehire, ...
never enter the daily queue, so they never use up a slot; the list is `AGGREGATORS` in
`backend/autoapply/submitters/registry.py`, and `autoapply_extra_aggregators` adds more domains.

## Turn it on

1. Add the compose overlay (starts the `applier` service), in `.env`:
   `COMPOSE_FILE=docker-compose.yml:docker-compose.autoapply.yml`, plus `CLAUDE_CODE_OAUTH_TOKEN`
   (`claude setup-token`), optionally `APPLIER_TOKEN` (any random string, shared by backend and applier)
   and `APPLIER_VNC_PASSWORD` (turns on the live view). Then `docker compose up -d --build`.
2. Open **AutoApply** in the sidebar -> **Settings**. Defaults are conservative: `autoapply_enabled`
   off, `autoapply_mode` = review, `autoapply_dry_run` on.
3. Enable it, press **Build today's queue**, approve items. With dry run on, forms are filled and
   screenshotted but never submitted; check the receipts in the item drawer, then turn dry run off.

Mind the tailoring guard: only `summary` and `skills` may change, experience / projects / education /
header must stay byte-identical, skills must already appear in the base résumé, and no new numbers
may appear in the summary. A model reply that breaks this is retried once, then the base résumé is used.

## The applier (form filler)

`applier/` is a separate service: one Chromium on a virtual screen plus Claude Code, which fills each
form through the Playwright MCP tools (the approach of ApplyPilot, rebuilt as an HTTP service). The
backend sends it one application at a time: URL, persona, standard answers, Q&A bank, résumé text,
the tailored résumé PDF and the cover letter. It has no access to JobNavigator's database or `.env`.

- The agent reads the page's accessibility tree and adapts to any form layout, so it is not tied to
  Greenhouse/Lever/Ashby selectors. Questions it cannot answer truthfully come back as **Needs you**
  questions; your answer goes to the Persona Q&A bank and the item is retried.
- Captchas and login walls come back as Needs you. With `APPLIER_VNC_PASSWORD` set, **Live view**
  on the queue screen (`/applier/vnc.html`, proxied by Caddy) shows the browser; you can solve the
  captcha or create the account there, then **Retry submit**. The browser profile is kept in the
  `applier_profile` volume, so logins survive.
- It runs on your Claude subscription (`CLAUDE_CODE_OAUTH_TOKEN`). One application is roughly 20-60
  browser actions. Hitting the usage limit puts the item back to ready and tries again 30 minutes later.
- Settings: `autoapply_engine` (agent | builtin, the old scripted fillers), `autoapply_agent_model`,
  `autoapply_agent_timeout_s`, `autoapply_agent_max_turns`, `autoapply_agent_instructions`
  (appended to every run: salary expectations, notice period, anything forms keep asking).
- **Check applier** on the queue screen reports browser, Claude Code version and login.
- API (internal network only): `POST /runs`, `GET /runs/{id}`, `POST /runs/{id}/cancel`, `GET /health`.
  To run it on another machine, publish port 8080 there, set `autoapply_applier_url`, and set
  `APPLIER_TOKEN` on both sides.

Start with dry run on: the agent fills everything and stops before submit; check the final screenshot
in the item drawer (Needs you tab, status dry run ok), then turn dry run off.

## LLM routing

`autoapply_llm_cheap` (form answers) and `autoapply_llm_quality` (tailoring, free-text answers) are
ordered `[{provider, model}]` chains. A provider that hits its usage limit is skipped for
`autoapply_llm_quota_cooldown_min` (default 300); any other error for `..._error_cooldown_min` (5).
Usage per provider/day: `GET /api/autoapply/llm/usage` and the Stats screen.

Cover letters go through upstream's cover-letter LLM setting; point it at `claude_code` to use your
subscription. Scoring keeps upstream's `scoring_llm_*` setting.

## Your own Chrome (legacy: LinkedIn Easy Apply, ZipRecruiter)

Not used any more: LinkedIn and ZipRecruiter are aggregators and are no longer queued. Kept for reference.

Those sites need your login, so AutoApply drives your Chrome over the DevTools protocol, in one new tab
per application, and never touches your other tabs. Chrome 136+ only allows this on a non-default
profile.

On the machine with Chrome (the Mac):

```
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-autoapply"
```

Log in to LinkedIn / ZipRecruiter once in that window. Then give the backend container a path to
port 9222 (pick one):

- **Reverse tunnel from the Mac** (no Remote Login needed on the Mac):
  `ssh -N -R 172.17.0.1:9223:127.0.0.1:9222 you@<server>`. The server's `sshd_config` needs
  `GatewayPorts clientspecified` for the bridge-IP bind.
- **Forward tunnel from the server** (needs Remote Login on the Mac):
  `ssh -N -L 172.17.0.1:9223:127.0.0.1:9222 you@<mac>`.
- Chrome on the server itself: `backend/autoapply/tools/start-chrome-cdp.sh`.

`autoapply_cdp_url` defaults to `http://host.docker.internal:9223`. **Check Chrome** on the queue screen
reports reachability and whether LinkedIn is logged in. A checkpoint / "unusual activity" page pauses
that site for `autoapply_site_pause_hours`; clear it in your Chrome, then **Resume now**.

LinkedIn and ZipRecruiter forbid automation in their terms; keep their caps low (defaults 15 and 10/day).

## Workday

Each employer is a separate tenant that needs its own candidate account. AutoApply creates one with
your persona email and a generated password, stored Fernet-encrypted in `autoapply_ats_credentials`
(key: `AUTOAPPLY_SECRET_KEY`, else `<autoapply_data_dir>/.secret_key`, mode 0600; back it up with the DB or
the passwords are unreadable). Dry runs never create accounts. If a tenant wants the email verified, the item
parks as **Needs you**; verify, then retry. To use an account you already have:
`PUT /api/autoapply/credentials {host, email, password}`.

This handler is exercised against a look-alike fixture, not a live tenant; expect tenant-specific
quirks and use dry run first.

## Pacing and safety

Quiet hours (`23-7` in `autoapply_timezone`), jittered gap between submits (45-180 s, 90-300 s on your
Chrome), global / per-site / per-company daily caps counted on real submissions, never the same
company back to back. CAPTCHAs and login walls park the item; there is no solver and no
fingerprint spoofing.

Nothing is retried on its own. A failure stays in **Failed** with its screenshots until you press
**Retry submit**. If the submit button was clicked but no confirmation was recognised (an emailed
verification code, an unfamiliar thank-you page), the item parks in **Needs you** rather than being
sent again. Postings that no longer take applications are skipped, and the job is marked `skip`.
For anything you finish by hand, **Mark applied** records it in the application tracker like an
automatic submit.

## Tests

```
docker run --rm -v $PWD/backend:/app/backend -v $PWD/pytest.ini:/app/pytest.ini -w /app \
  jobnavigator-backend:latest python -m pytest backend/tests/autoapply -q
```
