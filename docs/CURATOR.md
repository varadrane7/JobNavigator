# Daily curator

Every morning (default **04:30 America/New_York**, after the Claude/Antigravity limits reset) the curator turns the day's few
hundred new jobs into a ranked list of 50 worth applying to today, saves them,
and finds 15–20 people to contact about them. Results are on the **Today**
screen, and a summary goes to Telegram.

## How a job gets picked

| Step | What happens | Cost |
|---|---|---|
| Pool | `status = new`, posted (or first seen) within `curate_window_hours` (36), US or unknown country, no application to that company in the last 30 days | SQL |
| Hard drops | title words in `curate_title_exclude` (senior, staff, lead, manager…), JD says no sponsorship, JD asks for 6+ years | regex |
| Embedding pre-filter | résumé-vs-JD cosine similarity on the local embedding model (`curate_embed_model`) | seconds |
| LLM fit | the top `curate_llm_pool` (150) get the usual light fit score, if they have none | local LLM |
| Company intel | for the companies that could make the cut: funding stage, last raise, layoffs, how often candidates hear back, pay band for ~3 YOE, email format. 5 SearXNG queries + 1 LLM extraction, cached 30 days in `company_intel` | search + local LLM |
| Rank | composite of the parts below, at most 2 jobs per company | none |
| Write | top 50 → `saved` with `curated_rank`; the rest of the window → `ignored` (`curation.out = true`, reversible) | SQL |
| Outreach | people for the top companies, email guess, drafted note + email | search + LinkedIn + LLM |

Composite score (`curate_weights`):

- **fit 0.40**: the LLM résumé score, blended with the embedding percentile.
- **fresh 0.20**: full marks for the first 4 h after posting, then the value halves every 18 h.
- **visa/pay 0.20**: the best of three signals:
  - H-1B sponsorship (LCA count and approval rate);
  - pay against the target band (`target_base_min` 115k to `target_base_max` 150k, taken from the posting, else the LCA median, else the web estimate);
  - "well funded" (Series C+, public, a recent Series B, or 50+ LCAs), which is never held to the band.
- **reply 0.12**: your own reply rate at that company once you have 3+ applications there, else its interview-review reputation. Greenhouse/Ashby/Lever postings get a small bump; Workday/Taleo/iCIMS a small dip.
- **stability 0.08**: funding stage, a raise in the last year, no layoffs.

Each pick gets an **apply window**:

- **now**: ranks 1–15, or posted very recently.
- **morning**: ranks 16–35.
- **today**: the rest.

It also gets a one-line reason, e.g. `fit 86 (SDE) · posted 5h ago · H-1B 320 LCAs · Series C '25 · ~$140k base (posted)`.

## Which model does what

| Work | Model | Why |
|---|---|---|
| Fit scoring of unscored shortlist jobs | the **Scoring** model (your local one) | per job, there are many |
| Embedding pre-filter | `curate_embed_model` on the local server | seconds for hundreds of JDs |
| Company research, outreach drafts | `curator_llm` → `curator_fallback_llm` → local | defaults: Antigravity `gemini-3.8-flash-medium`, then Claude Code `claude-sonnet-5`, then the local model. A model that fails or hits its limit is skipped for the rest of the run. `curate_concurrency` (4) calls run in parallel |

## People to contact

For each top company (best-ranked first) until `outreach_count` people are found:

1. SearXNG `site:linkedin.com/in <company> ("engineering manager" OR founder OR CTO …)` and a recruiter query. Results are classified as founder / hiring manager / recruiter. Founders rank higher at small companies (Series B or earlier, or under 300 people).
2. If nothing useful turns up, LinkedIn's own people search runs in a headless browser:
   - It uses the **mock account** session first (the one Settings → LinkedIn refreshes).
   - It falls back to the **personal** account only if the mock session is dead or rate-limited.
   - It makes at most `linkedin_people_daily_cap` searches a day, spaced 20–60 s apart.
3. An email address is guessed from the company's email format, which comes from RocketReach/LeadIQ snippets via SearXNG (e.g. `flast@ramp.com`). It is marked *(guess)*. Nothing is sent or verified.
4. The curator LLM drafts a LinkedIn note (≤ 280 chars) and a short cold email. Both can be copied from the Today screen.

## Search: SearXNG

- The `searxng` compose service is reachable at `http://searxng:8080` from the backend and at `http://127.0.0.1:8888` from this host only. Its config is `searxng/settings.yml`.
- From this host, Google, DuckDuckGo, Brave and Qwant answer with CAPTCHAs or bans. They are disabled; Bing, Yahoo, Presearch, Mojeek and Startpage do the work.
- Set `SEARXNG_SECRET` in `.env`.

### LM Studio

The `mcp-searxng` compose service exposes SearXNG as an MCP server on `127.0.0.1:3010`. It provides these tools:

- `searxng_web_search`
- `web_url_read`
- `searxng_search_suggestions`
- `searxng_instance_info`

To use it in LM Studio, put this in `~/.lmstudio/mcp.json` (or Program → Install → Edit mcp.json):

```json
{
  "mcpServers": {
    "searxng": { "url": "http://127.0.0.1:3010/mcp" }
  }
}
```

Then enable the `searxng` integration in a chat. Your local model can now search and read pages.

The backend has its own tool loop with `web_search` and `fetch_page` over the same SearXNG (`backend/analyzer/agent_loop.py`) for open-ended agent tasks. The curator mostly uses fixed queries plus small JSON extractions instead, because those stay reliable on a 4B local model.

## Settings

Find these in Settings → Pipeline → Daily curator:

- `curate_cron`, `curate_count`, `curate_window_hours`, `curate_llm_pool`, `curate_auto_ignore`
- `target_base_min`, `target_base_max`, `curate_title_exclude`, `curate_weights`, `curate_embed_model`
- `curator_llm_*`, `curator_fallback_llm_*`, `curate_concurrency`
- `outreach_count`, `linkedin_people_daily_cap`, `searxng_url`

**Run now** (on the Today screen or in Settings) does the same thing as the cron run. `POST /api/curator/run` takes the flags `outreach=false` and `send=false`.

## Time zone

The whole app runs on one zone, `TZ` in `.env` (default `America/New_York`):

- APScheduler crons;
- the Postgres session, so day buckets in SQL fall on the same days;
- "today" in the feed counts and the Telegram digest;
- the UI's cron descriptions and Stats days.

See `backend/tz.py` and `frontend/src/time.js` (`APP_TZ`).
