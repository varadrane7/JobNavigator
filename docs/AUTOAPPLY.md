# AutoApply

Self-hosted auto-apply pipeline. Everything lives in `backend/autoapply/`, `frontend/src/autoapply/` and
`backend/tests/autoapply/`; upstream files carry only one-line hooks (`main.py`, `scheduler.py`,
`App.jsx`, `Shell.jsx`).

```
scrape -> score vs base résumés -> rank, top N/day -> tailor summary + skills -> (cover letter)
       -> review gate -> submit (paced) -> Applications tracker
```

## Turn it on

1. Add the compose overlay (gives the backend `host.docker.internal`), in `.env`:
   `COMPOSE_FILE=docker-compose.yml:docker-compose.autoapply.yml`, then `docker compose up -d --build`.
2. Open **AutoApply** in the sidebar -> **Settings**. Defaults are conservative: `autoapply_enabled`
   off, `autoapply_mode` = review, `autoapply_dry_run` on.
3. Enable it, press **Build today's queue**, approve items. With dry run on, forms are filled and
   screenshotted but never submitted; check the receipts in the item drawer, then turn dry run off.

Mind the tailoring guard: only `summary` and `skills` may change, experience / projects / education /
header must stay byte-identical, skills must already appear in the base résumé, and no new numbers
may appear in the summary. A model reply that breaks this is retried once, then the base résumé is used.

## LLM routing

`autoapply_llm_cheap` (form answers) and `autoapply_llm_quality` (tailoring, free-text answers) are
ordered `[{provider, model}]` chains. A provider that hits its usage limit is skipped for
`autoapply_llm_quota_cooldown_min` (default 300); any other error for `..._error_cooldown_min` (5).
Usage per provider/day: `GET /api/autoapply/llm/usage` and the Stats screen.

Cover letters go through upstream's cover-letter LLM setting; point it at `claude_code` to use your
subscription. Scoring keeps upstream's `scoring_llm_*` setting.

## Your own Chrome (LinkedIn Easy Apply, ZipRecruiter)

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
