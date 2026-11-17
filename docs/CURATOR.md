# Daily curator

Every morning (default **04:30 America/New_York**, after the Claude/Antigravity limits reset) the curator turns the day's few
hundred new jobs into a ranked list (`curate_count`) worth applying to today, saves them,
finds people to contact about them, and finds a few engineering leaders worth knowing at good
companies whether or not they are hiring. Results are on the **Today** screen. Telegram gets one
line of counts ("Curated list generated · X jobs to apply · Y unique companies · Z people to connect").

## One list per account

The run happens once per account, in turn, each on that account's own jobs, résumés and
Persona. List size, window, excludes, weights, pay targets and outreach counts are personal
settings (Settings › Daily curator; a new member starts from the defaults). The curator model
and its fallback follow the account's choice: **Inherit** the owner's provider and key, or
**Own** (the member's own provider and key). The schedule, embedding model and
parallelism stay the owner's. An account with no résumé is skipped; everyone can press
**Run curator** for their own list. Telegram summaries go to each account's own chat.

## How a job gets picked

| Step | What happens | Cost |
|---|---|---|
| Pool | `status = new`, posted (or first seen) within `curate_window_hours` (36; **7 days** for companies on the Companies list), in one of the account's `curate_countries` (default US) or of unknown country, no application to that company in the last 30 days | SQL |
| Hard drops | companies in `curate_company_exclude` (staffing agencies, body shops, AI-training gigs) and gig titles ("AI code evaluation", "annotation"), title words in `curate_title_exclude` (staff, lead, manager…; senior titles stay in), JD says no sponsorship, JD asks for 6+ years | regex |
| Embedding pre-filter | résumé-vs-JD cosine similarity on the local embedding model (`curate_embed_model`) | seconds |
| LLM fit | the top `curate_llm_pool` (150) get the usual light fit score, if they have none | local LLM |
| Company data | no web research. Your own reply rate at the company comes from your applications; the email format (domain + name pattern) is read from `company_intel` where an earlier version stored one | SQL |
| Rank | composite of the parts below, at most 2 jobs per company, one copy of a posting seen on two boards (same first company word + same title) | none |
| Write | top 50 → `saved` with `curated_rank`; the rest of the window → `ignored` (`curation.out = true`, reversible) | SQL |
| Outreach | people for the top companies, then people to know (below), email guess, drafted note + email | search + LinkedIn + LLM |

Composite score (`curate_weights`):

- **fit 0.40**: the LLM résumé score, blended with the embedding percentile.
- **fresh 0.20**: full marks for the first 4 h after posting, then the value halves every 18 h.
- **visa/pay 0.20**: the better of two signals:
  - H-1B sponsorship (LCA count and approval rate);
  - pay against the target band (`target_base_min` 115k to `target_base_max` 150k, taken from the posting, else the LCA median).
- **reply 0.12**: your own reply rate at that company once you have 3+ applications there, else a neutral 0.5. Greenhouse/Ashby/Lever postings get a small bump; Workday/Taleo/iCIMS a small dip.
- **tracked 0.05**: 1 for a company on your Companies list. The score is the weighted mean of the parts that are known, so a part left out (unknown pay with H-1B filings ignored) or a retired key in a saved `curate_weights` (`stability`) never skews it.

Each pick gets an **apply window**:

- **now**: ranks 1–15, or posted very recently.
- **morning**: ranks 16–35.
- **today**: the rest.

It also gets a one-line reason, e.g. `fit 86 (SDE) · posted 5h ago · H-1B 320 LCAs · ~$140k base (posted)`.

## A lane abroad (relocation)

An account whose `curate_countries` has no `US` (Settings › Daily curator › Countries, e.g.
`DE NL IE AE`) is a relocation lane. US H-1B data says nothing there, so:

- **Hard drops** add postings that need German (a stated level such as "fließend Deutsch",
  "German C1", or a posting written in German) and postings that need local work rights
  ("EU citizens only", "valid EU work permit required", "we cannot offer visa sponsorship",
  "UAE nationals only"). The "unlikely H-1B sponsor" drop is off.
- **visa/pay** reads the posting: visa sponsorship, relocation or Blue Card wording scores 1.0,
  "English is our working language" 0.65, silence 0.35 (0.7 for a company on your list). Pay is
  the posted EUR or AED figure (AED per month × 12) against the market band: DE €60–80k,
  NL/IE €55–75k, UAE AED 216–300k. A German posting under the reduced Blue Card threshold
  (€45,934) scores 0 on pay and says so. LCA medians (a US figure) are
  not used.
- **People to know** are looked for in the lane's first country.

The reason line reads e.g. `fit 82 (SDE) · posted 6h ago · visa/relocation offered · ~€72k base (posted)`.

`python -m backend.scripts.seed_international --commit` sets one up: a linked profile of the
owner (your single sign-on login switches to it from the account menu, no PIN), copying your
persona, résumés and settings. It adds LinkedIn, Indeed and Bayt searches per country,
Arbeitnow and Berlin Startup Jobs, and about 40 English-first employers that hire from abroad.
The employers are added **paused**; resume the ones you want. Its LinkedIn section inherits your
sessions, so both profiles share one daily LinkedIn budget. That is why it finds fewer people a
day.

## Which model does what

| Work | Model | Why |
|---|---|---|
| Fit scoring of unscored shortlist jobs | the **Scoring** model (your local one) | per job, there are many |
| Embedding pre-filter | `curate_embed_model` on the local server | seconds for hundreds of JDs |
| Outreach drafts | `curator_llm` → `curator_fallback_llm` → local | defaults: Antigravity `gemini-3.8-flash-medium`, then Claude Code `claude-sonnet-5`, then the local model. A model that fails or hits its limit is skipped for the rest of the run. `curate_concurrency` (4) calls run in parallel |

## People to contact

For each top company (best-ranked first) until `outreach_count` people are found:

1. A US job's company goes through the **company-first** search (`curator/company_first.py`): companies searched in the last 30 days come from the `company_people` cache, otherwise LinkedIn's faceted search runs on your own session: your schools' alumni, then (top 3 companies only) the authors of the company's hiring posts of the past week (`authorCompany`), then recruiters and engineers, then ex-colleagues. Post authors are saved with audience `post` (`hiring_manager` when the headline says so) and the post text as evidence, so the draft can mention the post. Every step spends one load of the daily LinkedIn budget (15 on the main session, split across the day's runs) and stops once enough people are found. A company whose LinkedIn id is not known yet, and any job outside the US, uses LinkedIn's keyword people search:
   - It uses the **mock account** session first (the one Settings → LinkedIn refreshes).
   - It falls back to the **personal** account only if the mock session is dead or rate-limited.
   - It makes at most `linkedin_people_daily_cap` searches a day, spaced 20–60 s apart.
   Every hit is checked before it is kept:
   - **Still there.** The card's current employer (LinkedIn's `Current:`/`Past:` line) must be the company. Headlines like "ex-Ramp" or "formerly at Ramp" are dropped.
   - **Same country as the job.** Read from the card's location (a bare ZIP counts as US). LinkedIn's own search adds its location filter.
   - **Close to the role.** The job title's area (ML, data, mobile, frontend, infra, security, backend) boosts managers whose headline matches. Technical recruiters beat general ones, and university / early-career recruiters rank up for new-grad roles. Someone in the job's state ranks above someone elsewhere in the country.
3. An email address is guessed from the company's stored email format (e.g. `flast@ramp.com`), when there is one. It is marked *(guess)*. Nothing is sent or verified.
4. The curator LLM drafts a LinkedIn note (≤ 290 chars, a Premium note) and a short cold email. Both can be copied from the Today screen.
   The drafts get a structured profile from the default résumé: summary, recent roles, skills, and **every project
   with its stack, one line and link**. The LLM names the one project closest to the role.

## People to know

After the job-linked people, the curator finds `outreach_network_count` (10) engineering leaders at good companies,
whether or not those companies have a posting for you today. Job slots nobody filled (`outreach_count` minus people found) are added to this.

- **Which companies, best first:**
  1. your Companies list (tier 1, then 2);
  2. companies that ranked just below today's cut.
  Left out: any company with a contact in the last 45 days, and anything in `curate_company_exclude`.
- **Who:** engineering managers, directors / heads / VPs of engineering, plus founders and CTOs. Recruiters are skipped because they work open requisitions. The team areas come from your résumé (backend, platform, AI…), and people in your Persona city rank first.
- **Drafts:** no "I applied". The closest project with its link, then the ask: a 15-minute chat, and a referral when a backend or AI role opens.
- They are saved as `contacts.purpose = 'network'` with no job, and appear under **People to know** on the Today screen.

LinkedIn allows roughly 100–200 invitations a week even with Premium. At 35 people a day, send the rest by email.

## Settings

Find these in Settings → Pipeline → Daily curator:

- `curate_cron`, `curate_count`, `curate_window_hours`, `curate_llm_pool`, `curate_auto_ignore`
- `target_base_min`, `target_base_max`, `curate_title_exclude`, `curate_weights`, `curate_embed_model`
- `curator_llm_*`, `curator_fallback_llm_*`, `curate_concurrency`
- `outreach_count`, `outreach_network_count`, `curate_company_exclude`, `linkedin_people_daily_cap`

**Run now** (on the Today screen or in Settings) does the same thing as the cron run. `POST /api/curator/run` takes the flags `outreach=false` and `send=false`.

## Time zone

The whole app runs on one zone, `TZ` in `.env` (default `America/New_York`):

- APScheduler crons;
- the Postgres session, so day buckets in SQL fall on the same days;
- "today" in the feed counts and the Telegram digest;
- the UI's cron descriptions and Stats days.

See `backend/tz.py` and `frontend/src/time.js` (`APP_TZ`).
