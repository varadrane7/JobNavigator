<<<<<<< HEAD
# JobNavigator

**Self-hosted job-hunt automation.**

Scrape career pages and aggregators, score jobs against your résumés with an LLM, tailor résumés and cover letters, auto-fill applications from your persona, get Telegram alerts, track every application.

<p align="center">
  <img src="docs/default.png?v=2" alt="JobNavigator 2.0 — Jobs feed, Paper theme" width="100%">
</p>

<p align="center">
  <a href="docs/board.png"><img src="docs/board.png" alt="Green Paper theme" width="24%"></a>
  <a href="docs/slate.png"><img src="docs/slate.png" alt="Stone theme" width="24%"></a>
  <a href="docs/v1like.png"><img src="docs/v1like.png" alt="V1 Style theme" width="24%"></a>
  <a href="docs/win98.png"><img src="docs/win98.png" alt="Windows 98 theme" width="24%"></a>
  <br>
  <sub>Five themes, each in light and dark: Paper (above), Green Paper, Stone, V1 Style, Windows 98.</sub>
</p>

## How It Works

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                               JOB DISCOVERY                                 │
│                                                                             │
│   Career Pages          │  Aggregators            │  Chrome Extension       │
│                         │                         │                         │
│   Any site via          │  JobSpy: LinkedIn,      │  Passive LinkedIn       │
│   Playwright            │  Indeed, ZipRecruiter,  │  capture while          │
│                         │  Google Jobs            │  browsing               │
│   11 ATS endpoints:     │                         │                         │
│   Workday, Greenhouse   │  LinkedIn Personal      │  Auto-fill based on     │
│   Lever, Ashby,         │  collections            │  your persona input     │
│   Oracle, Phenom,       │                         │  and question bank      │
│   TalentBrew, Rippling  │  Jobright.ai            │                         │
│   SmartRecruiters,      │  Levels.fyi             │  Save any job from      │
│   + custom              │  freehire.me            │  any page               │
│                         │                         │                         │
└─────────────────────────┴───────────┬─────────────┴─────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                PROCESSING                                   │
│                                                                             │
│   Dedup ────── URL-hash dedup, tracking params stripped                     │
│   Filters ──── Title / company include & exclude, body exclusion phrases    │
│   H-1B ─────── Company LCA data from MyVisaJobs (cached)                    │
│   Salary ───── Extracted from posting, H-1B data, description               │
│                                                                             │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                 JOB FEED                                    │
│                                                                             │
│   Review ───── Dynamic filters, sorting, detail panel                       │
│   Decide ───── Save promising jobs, skip the rest, score with AI            │
│                                                                             │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              AI RESUME SCORING                              │
│                                                                             │
│   Providers ── Claude API, Claude CLI, OpenAI, OpenRouter, Ollama           │
│   Depths ───── Light (scores only) or Full (report + keyword analysis)      │
│   Multi ────── Score against multiple resumes, compare fit per role         │
│                                                                             │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           RESUME + COVER LETTER                             │
│                                                                             │
│   Templates ── 8 resume + 8 cover-letter, auto-discovered (add your own!)   │
│   AI Tailor ── Rewrites resume bullets/keywords from the scoring report     │
│   AI Letter ── Job-specific cover letters from resume + JD, voice presets   │
│   Export ───── Live-preview PDF via Playwright with export                  │
│                                                                             │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                   TRACK                                     │
│                                                                             │
│   Autofill ─── ATS forms + AI answers to free-text questions (Persona)      │
│   Apps ─────── Stages, interviews, notes, prep handover, funnel and Sankey  │
│   Tracer ───── Unique links per resume/letter, tracks who opened them       │
│   Gmail ────── Auto-detects responses, updates application status           │
│   Telegram ─── Job alerts, daily digest, scrape health notifications        │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Features

| Feature | Description |
|---------|-------------|
| **Discovery** | Career pages (Playwright + 11 ATS handlers), JobSpy (LinkedIn, Indeed, ZipRecruiter, Google), LinkedIn collections, Levels.fyi, Jobright.ai, freehire.me, the Chrome extension |
| **Scoring** | Claude, OpenAI, OpenRouter, Ollama or Claude Code; live model search, a model per feature; Light (score) or Full (report, keyword coverage, requirement mapping) against every base résumé; prompt caching on Anthropic |
| **Résumés** | Structured base résumés, tailoring per job with a review step, 8 PDF templates (drop in your own), tracked links that record opens |
| **Cover letters** | Generated from the paired résumé and persona; voice and length presets, 8 templates, PDF |
| **Dedup** | URL identity hash (tracking params stripped) plus company + title hash across sources |
| **Jobs** | Filters, sorting, keyboard shortcuts (`?`), full report, collapsible analysis pane, bulk actions with undo, posting preview |
| **Applications** | Stages, interviews, notes, status history (funnel and Sankey in Stats), a prep handover for any AI chat |
| **Persona** | Contact, work authorization, compensation, preferences, résumé content, Q&A bank; import from a résumé or PDF |
| **Extension** | Passive LinkedIn capture, save any job from any page, ATS form autofill and AI answers to free-text questions from your Persona |
| **Gmail** | Polls replies, classifies them, updates the application |
| **Telegram** | New-job alerts, daily digest, scrape health, inline actions |
| **H-1B** | Company filing lookups, description exclusion phrases |
| **Scheduling** | Intervals and crons for scraping, email, backups, cleanup, auto-reject; every cron field explains itself and offers presets |
| **Themes** | Paper, Green Paper, Stone, V1 Style, Windows 98 × light / dark / system |

> The posting preview is an `iframe`; sites that refuse framing show blank unless the extension (which strips the frame-blocking headers) is installed. "Open" always works, and applied jobs keep a cached snapshot.

## Quick Start

```bash
git clone https://github.com/vesaias/JobNavigator.git
cd JobNavigator
cp .env.example .env

docker compose up --build -d
```

Open `http://localhost`. On first run sign in with a blank key, then set one in Settings › Advanced. The previous interface is at `/classic`.

**First steps:**
1. Settings › AI — provider and key
2. Résumés — create a base résumé or import a PDF
3. Persona — import from that résumé, fill the Q&A bank
4. Companies and Searches — add a few, run them

## Chrome Extension ("The Navigator")

- Unblocks the posting preview by stripping frame-blocking headers.
- Captures job ids while you browse `linkedin.com/jobs/collections/*` and imports them with full details.
- Fills ATS forms and drafts answers to free-text questions from your Persona and Q&A bank (toggle in the popup; model and prompt in Settings › AI).

Install: `chrome://extensions` › Developer mode › Load unpacked › `extension/`. The LinkedIn import needs a separate LinkedIn account in Settings › Accounts.

## Optional Integrations

**Telegram** — bot token in `.env`, chat id in Settings.

**Gmail** — `python backend/gmail_oauth_setup.py`, OAuth credentials in `.env`.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI, SQLAlchemy, APScheduler, Playwright |
| Frontend | React 18, Vite, Recharts, a token-based design system ([DESIGN-SYSTEM.md](frontend/src/DESIGN-SYSTEM.md)) |
| Database | PostgreSQL 16 |
| Infrastructure | Docker Compose, Caddy, nginx |
| AI | Anthropic SDK, OpenAI SDK, Ollama, Claude Code CLI |
| Extension | Chrome Manifest V3 |

## Contributing

PRs welcome, see [CONTRIBUTING.md](CONTRIBUTING.md). Good first ones: an ATS handler, a résumé template, a theme.

## Backups

Scheduled `pg_dump`s land in `backups/` (five kept). They contain the settings table, API keys included: treat them like `.env`. The folder is git-ignored.

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md). Please don't open public issues for security bugs.

## Privacy

Self-hosted. Your résumés, jobs and credentials stay on your machine; the only outside party is the AI provider you configure.

## Disclaimer

Personal use. Not affiliated with any job platform; some scrapers are off by default and you are responsible for the terms of the sites you use. See [LEGAL_DISCLAIMER.md](LEGAL_DISCLAIMER.md).

## License

[MIT](LICENSE)
=======
# JobNavigator

Self-hosted job search automation — scrape any career portal or use job aggregator, AI scoring against your profile, resume tailoring with custom themes, persona-based application auto-fill, Telegram notifications and tracking in one system. 

<p align="center">
  <img src="docs/jobnavigator.gif" alt="JobNavigator Demo" width="100%">
</p>

## How It Works

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                               JOB DISCOVERY                                 │
│                                                                             │
│   Career Pages          │  Aggregators            │  Chrome Extension       │
│                         │                         │                         │
│   Any site via          │  JobSpy: LinkedIn,      │  Passive LinkedIn       │
│   Playwright            │  Indeed, ZipRecruiter,  │  capture while          │
│                         │  Google Jobs            │  browsing               │
│   11 ATS endpoints:     │                         │                         │
│   Workday, Greenhouse   │  LinkedIn Personal      │  Auto-fill based on     │
│   Lever, Ashby,         │  collections            │  your persona input     │
│   Oracle, Phenom,       │                         │  and question bank      │
│   TalentBrew, Rippling  │  Jobright.ai            │                         │
│   SmartRecruiters,      │  Levels.fyi             │  Save any job from      │
│   + custom              │  freehire.me            │  any page               │
│                         │                         │                         │
└─────────────────────────┴───────────┬─────────────┴─────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                PROCESSING                                   │
│                                                                             │
│   Dedup ────── URL-hash dedup, tracking params stripped                     │
│   Filters ──── Title / company include & exclude, body exclusion phrases    │
│   H-1B ─────── Company LCA data from MyVisaJobs (cached)                    │
│   Salary ───── Extracted from posting, H-1B data, description               │
│                                                                             │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                 JOB FEED                                    │
│                                                                             │
│   Review ───── Dynamic filters, sorting, detail panel                       │
│   Decide ───── Save promising jobs, skip the rest, score with AI            │
│                                                                             │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              AI RESUME SCORING                              │
│                                                                             │
│   Providers ── Claude API, Claude CLI, OpenAI, Ollama                       │
│   Depths ───── Light (scores only) or Full (report + keyword analysis)      │
│   Multi ────── Score against multiple resumes, compare fit per role         │
│                                                                             │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           RESUME + COVER LETTER                             │
│                                                                             │
│   Templates ── 8 resume + 8 cover-letter, auto-discovered (add your own!)   │
│   AI Tailor ── Rewrites resume bullets/keywords from the scoring report     │
│   AI Letter ── Job-specific cover letters from resume + JD, voice presets   │
│   Export ───── Live-preview PDF via Playwright with export                  │
│                                                                             │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                   TRACK                                     │
│                                                                             │
│   Tracer ───── Unique links per resume/letter, tracks who opened them       │
│   Gmail ────── Auto-detects responses, updates application status           │
│   Telegram ─── Job alerts, daily digest, scrape health notifications        │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Features

| Feature | Description |
|---------|-------------|
| **Multi-Source Discovery** | 7 scraping tiers: career pages (Playwright + 11 ATS), JobSpy (4 boards), LinkedIn Personal, Levels.fyi, Jobright.ai, freehire.me, Chrome Extension |
| **AI Resume Scoring** | Multi-provider (Claude, OpenAI, Ollama), light/full depth, per-resume comparison, keyword analysis, requirement mapping, ATS tips. **Prompt caching** on Anthropic cuts repeat-scoring cost ~50%. |
| **Resume Builder** | 8 templates (auto-discovered - add yours), AI tailoring per job, PDF export, tracer links to track opens (cover letters too) |
| **Cover Letters** | AI-generated per job, grounded in the paired resume + persona; editable voice presets, 8 templates, PDF export, prompt-cached generation |
| **Smart Dedup** | URL-hash dedup with configurable tracking-param stripping; content hash stored per job for cross-source matching |
| **Job Feed** | Filters, sorting, keyboard shortcuts (j/k/s/x/e), scoring reports, bulk operations, in-app job preview |
| **Application Board** | Kanban pipeline with drag-and-drop, status transition history |
| **Chrome Extension** | Passive LinkedIn capture + save any job from any page |
| **Application Autofill** | Generate persona-grounded answers to free-text application questions on any job site, from the extension — review with a length picker, then insert, copy, or save to a reusable Q&A bank |
| **Gmail Monitor** | OAuth2 polling, auto-classifies responses, updates application status |
| **Telegram Alerts** | New job alerts, daily digest, scrape health, inline action buttons |
| **H-1B Data** | Company LCA lookups from MyVisaJobs, JD exclusion scanning |
| **Scheduling** | Cron-based: scraping, email checks, backups, cleanup, auto-reject |
| **Dark Mode** | Full Tailwind dark mode across all pages |

> **Note on the Job Feed preview pane:** the detail panel renders the live job posting in an `iframe`. Many career sites block being framed (via `X-Frame-Options` / CSP `frame-ancestors`, or cross-origin scripts that fail when embedded), so the in-app preview works best with a browser extension that strips frame-blocking headers (e.g. an "ignore X-Frame-Options" extension). Without one, some postings show blank — use the "Open" button to view them in a new tab. Applied jobs fall back to a cached snapshot that always renders.

## Quick Start

```bash
git clone https://github.com/vesaias/JobNavigator.git
cd JobNavigator
cp .env.example .env
# Edit .env if needed (optional — API keys can be set from dashboard)

docker compose up --build -d
```

Open `http://localhost`. On first run, click "Sign In" with a blank API key to proceed. Set a real key from Settings > General once you've accessed the dashboard.

**First steps:**
1. Settings > AI tab — configure your LLM provider and API key
2. Companies — activate a few seed companies or add your own
3. Searches — configure a keyword search or activate LinkedIn Personal
4. Resumes — create your resume (or import an existing PDF); it powers AI scoring

## Chrome Extension ("The Navigator")

**What it does:**
- **Unblocks the Job Feed preview.** Strips frame-blocking headers (`X-Frame-Options` / CSP `frame-ancestors`) so job postings render in the in-app `iframe` preview pane instead of showing blank.
- **Captures your LinkedIn collections.** As you browse your personalized **Recommended** and **Top Applicant** collections (`linkedin.com/jobs/collections/*`), it passively collects the job IDs, then imports them into the Job Feed with full details (title, company, description, real apply URL).
- **Fills application answers.** On any job site, focus a free-text question and click the Navigator button to generate a first-person answer based on your **Persona** + saved **Q&A bank**. Enable it with the **Application Autofill** toggle in the popup, and fill out your Persona (`/persona`) first so answers have something to ground on. Configure the model/prompt in **Settings → AI**.

**Install:**
1. `chrome://extensions/` → Developer mode → Load unpacked → select `extension/`
2. Toggle LinkedIn capture on, browse your personal job collections
3. Click "Send to JobNavigator" to import

**Make sure it works:** the import fetches each job's full data through LinkedIn's Voyager API using a **mock LinkedIn account** — set its email + password in **Settings → Accounts → LinkedIn Extension** before importing. Without it, capture still collects IDs but the import returns nothing.

## Optional Integrations

**Telegram** — Create bot via @BotFather, set token in `.env`, enter chat ID in Settings.

**Gmail** — Run `python backend/gmail_oauth_setup.py`, set OAuth credentials in `.env`.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI, SQLAlchemy, APScheduler, Playwright |
| Frontend | React 18, Tailwind CSS, Vite, Recharts |
| Database | PostgreSQL 16 |
| Infrastructure | Docker Compose, Caddy, nginx |
| AI | Anthropic SDK, OpenAI SDK, Ollama, Claude Code CLI |
| Extension | Chrome Manifest V3 |

## Contributing

PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Easy wins: new ATS scrapers, new resume templates, UI polish.

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md). Please don't open public issues for security bugs.

## Privacy

**JobNavigator is self-hosted — NOT a hosted service.** Your resume, job data, and credentials stay on your machine. Data is sent only to the AI provider you configure. We do not collect, store, or have access to any of your data.

## Disclaimer

Personal and educational use only. Not affiliated with LinkedIn, Indeed, Jobright.ai, or any job platform. Some scraping features are disabled by default and require explicit opt-in. You are responsible for complying with the Terms of Service of any platform you interact with. See [LEGAL_DISCLAIMER.md](LEGAL_DISCLAIMER.md).

## License

[MIT](LICENSE)
>>>>>>> 0967077 (base)
