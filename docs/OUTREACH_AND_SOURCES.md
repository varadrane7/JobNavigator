# Outreach, sources and tailoring — review after day 1 (2026-10-13)

Goal: 70–80 applications a day, every day, with as little manual work as possible. A curated list is only as good as the jobs that feed it, and an application only goes as far as the people who see it. This doc covers four things:

1. whether tailoring is worth the time;
2. what the LinkedIn "443" error was;
3. which companies to add;
4. how to change the searches.

The numbers come from the live DB on 2026-10-13.

---

## 1. Should you keep tailoring your résumé?

**Short answer:** stop tailoring every application. Pick the right base résumé, which is automatic. Lightly tailor only your top ~10 a day. Put the time you save into referrals.

### Evidence that tailoring helps

| Source | What they measured | Result | Caveat |
|---|---|---|---|
| Huntr (1.9M applications on their tracker) | interview rate, tailored vs not | 5.7% vs 3.0% | vendor that sells a tailoring tool; self-reported "tailored" |
| Kickresume experiment (204 applications) | same AI tool, one generic résumé vs one adapted per posting | 18% vs 10% response | small; mostly customer-support roles, not engineering |
| Jobscan (2.5M applications) | job title from the posting appears on the résumé | ~10.6× interview rate | measures *title match*, not full tailoring |
| Wellfound survey (500k+ seekers, cited by Workday) | "tailored, well-structured" résumés | ~2× interviews | method not published |

### Evidence that it matters less than the marketing says

- None of the above is peer-reviewed or specific to engineering. Each one comes from a company that sells résumé tooling.
- **Greenhouse does not reject on résumé keywords.** Its auto-rejects come from yes/no knockout questions (sponsorship, location). A human reviews everything else. Recruiters *search* by keyword, so the parsed skills still matter, but a reordered skills line does not change whether a person reads your résumé. Ashby and Lever behave much the same, with knockout questions plus human review.
- Eye-tracking studies (TheLadders, 2012 and 2018) found recruiters spend about 6–7 s on a first pass, almost all of it on **current title, company, dates and education**. The summary and skills block get the least attention.
- Anecdotes conflict. Some engineers on Blind and Reddit say tailoring raised their hit rate; others got the same results firing off a base résumé.

### Your own data

| | Applications | Interviews | Rejections |
|---|---|---|---|
| Base résumé | 179 | 2 (1.1%) | 9 |
| Tailored for the job | 26 | 0 | 3 |

That sample proves nothing either way. Telling a 3% interview rate from a 6% one needs about 300 applications in each group.

### What to do

- **Keep picking a variant.** Choosing General, SDE, Cloud or .NET is the cheap version of tailoring, and it gets most of the gain (title and keyword match). The curator already picks one per job (`best_cv`), and the Today screen's cover-letter link uses it.
- **Tailor lightly, and only for the top ~10 a day:** rank "Apply now" with a person found for that job. Three edits:
  1. the title line, so it matches the posting;
  2. the first sentence of the summary;
  3. the skills line, reordered so the JD's must-haves come first.
  Never rewrite bullets. Five minutes at most.
- **Put the rest of the time into referrals.** Every referral study disagrees on the exact numbers. One says referrals are 2.8% of applicants but about 17% of hires; another gives a 30% vs 7% hire rate. They all agree that referred candidates convert several times better than cold applicants. Contacting 35 people a day will move your interview rate more than tailoring 70 résumés.
- Re-check in about 6 weeks. Tailored résumés are rows in `resumes` with a `job_id`, so the comparison above can be re-run.

Sources:
- [Kickresume experiment](https://www.kickresume.com/en/press/job-application-experiment/)
- [Workday on Wellfound data](https://www.workday.com/en-us/perspectives/hr/resume-tips-ai-hiring.html)
- [Jobscan title stat (via Interview Guys)](https://blog.theinterviewguys.com/how-to-stand-out-when-250-people-applied/)
- [Greenhouse: what happens after you apply](https://my.greenhouse.com/blogs/what-really-happens-after-you-apply-for-a-job)
- [The 6-second rule](https://simplify.jobs/blog/6-second-resume-rule)
- [Blind thread](https://www.teamblind.com/post/tailoring-resumes-vs-just-applying-everywhere-quality-or-quantity-qold04vv)
- [Referral statistics](https://voxbooster.com/blog/employee-referral-statistics-2026)

---

## 2. LinkedIn "443"

**It is not an error code. It is the HTTPS port.** The backend log shows:

```
JobSpy:LinkedIn - LinkedIn: HTTPSConnectionPool(host='www.linkedin.com', port=443): Max retries exceeded with url:
/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=Software+Engineer&...&start=60&f_TPR=r86400
(Caused by ResponseError('too many 429 error responses'))
```

- The real error is **HTTP 429 Too Many Requests**. LinkedIn throttled its public "guest" jobs endpoint, which python-jobspy calls with no login and no proxy. It cut the scraper off after a few pages (at `start=10` and at `start=60`).
- **The jobs from the earlier pages were still saved**, so those runs were partial, not empty.
- The error summary took the first number in the message that looked like a 4xx/5xx status, which was `port=443`. That is fixed: the run log now reads `429: rate-limited by the board; jobs from the earlier pages were kept`.

**Why it happens.** Both keyword searches ran every 2 hours with `hours_old = 24` and `results_wanted = 50`. That means re-paging the whole last day of postings 12 times a day per search, and the guest endpoint tolerates only a few pages per burst.

**Put LinkedIn back.** You removed it from both searches after the errors, but LinkedIn was where most of your applications came from: 35 applied and 22 curated in the last 14 days, compared with 16 and 17 from Indeed. Re-add it as **separate LinkedIn-only searches** set to fetch only what is new:

| Setting | Value | Why |
|---|---|---|
| sources | `linkedin` only | Indeed keeps its own searches with `hours_old = 24` |
| `hours_old` | **4** | the run every 2 h only needs the last few hours; 1–2 pages instead of 5 |
| `results_wanted` | **25** | stays under the per-burst limit |
| interval | the default 120 min | 4 h of look-back covers a missed run |

If 429s keep showing up after that, set `proxy_url` in Settings. Even then, the earlier pages are kept.

---

## 3. Companies to add

**What happened on day 1.** Your 18 tracked companies put **0 jobs into the Top 50**. Their jobs reached the curator but aged out of the 36 h window, because a career page rarely says when a job was posted. For example, Intuit "Software Engineer 2" scored 94 and was still `new`. This is fixed: tracked companies now get a 7-day window and a small "your list" ranking bonus. So the companies you add here will make it into the list.

Your profile, as the companies below were matched against it:
- 3 YOE backend: trading APIs serving 2M users, an API gateway, auth.
- Python, Go and C#/.NET.
- AWS (Step Functions, Bedrock, Aurora/pgvector) and Terraform.
- RAG and document extraction (97% of a healthtech startup's intake).
- MS from NJIT (May 2026), so you are new-grad eligible.
- Based in NYC/NJ.

How to read the tables:
- **Fit now** = open roles on the public board that are engineering titles, not senior/staff/lead/manager, and in a US or remote location. These were counted from the live APIs on 2026-10-13, so they are a snapshot.
- Add each one in **Companies** with the board URL. If a board is big, add a title filter (see the notes).

### Tier 1: best fit, and hiring now

| Company | Why you | ATS · board | Eng / fit now |
|---|---|---|---|
| **Plaid** (reactivate; already in your list, inactive) | fintech APIs, backend | Ashby · https://jobs.ashbyhq.com/plaid | 39 / 12 (Software Engineer, Backend ×2) |
| **Clear Street** (NYC) | trading infra, which is your Kotak work | Greenhouse · https://job-boards.greenhouse.io/clearstreet | 19 / 3 (SWE – Platform, SWE – Low Touch Desk) |
| **Alpaca** | brokerage APIs | Greenhouse · https://job-boards.greenhouse.io/alpaca | 31 / 6 (SWE – Core Trading) |
| **Virtu Financial** (NYC) | electronic trading, C#/Python | Greenhouse · https://job-boards.greenhouse.io/virtu | 17 / 8 (incl. 2027 SWE internship; filter interns) |
| **Robinhood** | trading | Greenhouse · https://job-boards.greenhouse.io/robinhood | 73 / 15 |
| **Coinbase** | trading, payments | Greenhouse · https://job-boards.greenhouse.io/coinbase | 96 / 24 (filter interns, "Enterprise Engineer") |
| **Brex** | fintech, AI agents | Greenhouse · https://job-boards.greenhouse.io/brex | 71 / 6 (AI Engineer, SWE Forward Deployed) |
| **Mercury** | fintech backend | Greenhouse · https://job-boards.greenhouse.io/mercury | 18 / 3 (SWE – Infrastructure, SWE – Product) |
| **Modern Treasury** | payments APIs | Ashby · https://jobs.ashbyhq.com/moderntreasury | 2 / 2 |
| **Column** | bank infra APIs | Ashby · https://jobs.ashbyhq.com/column | 3 / 3 (Forward Deployed Engineer) |
| **iCapital** (NYC) | fintech, AI | Greenhouse · https://job-boards.greenhouse.io/icapitalnetwork | 44 / 26 (AI Engineer – AVP; "AVP"/"VP" are bank titles, so check the level) |
| **Reducto** | document extraction, which is exactly your pipeline | Ashby · https://jobs.ashbyhq.com/reducto | 12 / 10 (Backend/AI Engineer, FDE) |
| **Rogo** (NYC) | AI for finance | Ashby · https://jobs.ashbyhq.com/rogo | 13 / 12 (SWE: Platform) |
| **Tennr** (NYC) | healthcare document intake AI, your healthtech RAG work | Ashby · https://jobs.ashbyhq.com/tennr | 8 / 4 (Backend Software Engineer) |
| **Candid Health** | healthtech backend | Ashby · https://jobs.ashbyhq.com/candidhealth | 12 / 7 (Forward Deployed SWE) |
| **Harvey** (NYC office) | LLM applications | Ashby · https://jobs.ashbyhq.com/harvey | 123 / 24 (filter "Legal Engineer", "Research") |
| **Glean** | RAG/search; already showing up in your Top 50 | Greenhouse · https://job-boards.greenhouse.io/gleanwork | 40 / 26 (filter support/security) |
| **Chalk** | data/ML infra; showing up in your Top 50 | Ashby · https://jobs.ashbyhq.com/chalk | 13 / 7 (Software Engineer, New Grad) |
| **Temporal** | Go, distributed systems | Ashby · https://jobs.ashbyhq.com/temporal | 34 / 3 (Software Engineer II ×2) |
| **Supabase** | Postgres, pgvector | Ashby · https://jobs.ashbyhq.com/supabase | 35 / 33 (filter "Support Engineer") |

### Tier 2: good fit, fewer openings right now

| Company | Why you | ATS · board | Eng / fit now |
|---|---|---|---|
| Writer | LLM apps, has East-coast roles | Ashby · https://jobs.ashbyhq.com/writer | 18 / 10 (AI deployment engineer) |
| Modal | serverless and ML infra | Ashby · https://jobs.ashbyhq.com/modal | 12 / 8 (FDE – Systems) |
| ClickHouse | databases, C++/Go | Ashby · https://jobs.ashbyhq.com/clickhouse | 119 / 14 |
| Kong | API gateways, which is your Kotak gateway work | Ashby · https://jobs.ashbyhq.com/kong | 25 / 3 (SRE 2, FDE) |
| Chainguard | Go, supply-chain security; showing up in your Top 50 | Greenhouse · https://job-boards.greenhouse.io/chainguard | 31 / 11 (filter "Sales Engineer") |
| Figma | showing up in your Top 50 | Greenhouse · https://job-boards.greenhouse.io/figma | 30 / 19 |
| MongoDB (NYC HQ) | databases, backend | Greenhouse · https://job-boards.greenhouse.io/mongodb | 116 / 6 |
| Neon | serverless Postgres | Ashby · https://jobs.ashbyhq.com/neon | 3 / 1 |
| Unstructured | document ETL for LLMs | Ashby · https://jobs.ashbyhq.com/unstructured | 3 / 2 |
| Kraken | trading | Ashby · https://jobs.ashbyhq.com/kraken.com | 25 / 1 |
| Addepar (NYC) | wealth-tech data platform | Greenhouse · https://job-boards.greenhouse.io/addepar1 | 39 / 3 (mostly field/sales eng) |
| Headway (NYC) | healthtech | Ashby · https://jobs.ashbyhq.com/headway | 20 / 2 |
| Abridge | healthcare LLM | Ashby · https://jobs.ashbyhq.com/abridge | 5 / 0 |
| Zocdoc (NYC) | healthtech | Greenhouse · https://job-boards.greenhouse.io/zocdoc | 15 / 0 |
| Komodo Health | healthtech data | Greenhouse · https://job-boards.greenhouse.io/komodohealth | 8 / 0 |
| Ro (NYC) | healthtech | Lever · https://jobs.lever.co/ro | 17 / 1 |
| Gemini (reactivate) | crypto exchange | Greenhouse · https://job-boards.greenhouse.io/gemini | 14 / 1 |
| Cedar (reactivate) | healthtech payments | Ashby · https://jobs.ashbyhq.com/cedar | 1 / 1 |
| Grafana Labs | observability, Go | Greenhouse · https://job-boards.greenhouse.io/grafanalabs | 57 / 1 (mostly senior) |
| LlamaIndex | RAG framework | Ashby · https://jobs.ashbyhq.com/llamaindex | 3 / 2 (devrel) |
| Extend | document AI | Greenhouse · https://job-boards.greenhouse.io/extend | 5 / 0 |

### Worth adding, but not on a public Greenhouse/Ashby/Lever board

These need the career-page (generic/Playwright) scraper. Indeed and LinkedIn searches often surface them anyway.
- **MarketAxess** (NYC trading, Java/.NET)
- **Tradeweb** (NYC, Workday)
- **Marqeta**
- **Stack Overflow** (.NET)
- **Spring Health**
- **Instabase**
- **Postman** (API tooling, matching LazyCurl)
- **Bilt** (NYC)
- **Whatnot**
- **Increase**
- **Two Sigma** and **Hudson River Trading** (stretch; HRT's Ashby board showed 0 roles)
- **Bloomberg** (stretch; huge NYC C++/Python/.NET employer)
- **Hyperscience** and **Ocrolus**: document AI, a great fit on paper, but their boards had 1 and 0 roles. Add them inactive and check monthly.

### Current companies to rethink

| Company | 30-day jobs | Note |
|---|---|---|
| Kalshi | 0 | Board returns nothing for your filter. Check the department filter or drop it. |
| Gen Digital | 15 | Mostly non-eng (e.g. "Customer Service Identity Alerts Representative"). Tighten `title_include_expr` to engineer titles. |
| Oscar | 41 | High volume, few fits. Add an engineer title filter. |
| Intuit | 46 | Good jobs (SE2 scored 94–95), but lots of non-eng noise. Add an engineer title filter. |
| Ramp | 11 | The public board has **36 fit roles**, but your department filter only catches part of them. Widen it (e.g. engineering *and* AI/infra departments). |
| Datadog | 19 | The public Greenhouse board (`datadog`) has 124 eng roles. Many are Sales Engineer, so filter those. |
| Chainanalysis | 8 | Fine. (The name is misspelled; the company is "Chainalysis". Matching works anyway because jobs take the name from your Companies row.) |

### Global title noise

The public boards are full of **Sales Engineer / Solutions Engineer / Support Engineer / Field Engineer** titles, plus interns. Consider adding `sales engineer`, `solutions engineer`, `support engineer`, `field engineer` and `intern` to the global title excludes (Settings → Scraping), which today are vp, vice, principal, lead and legal. The curator already drops `intern` through its own list, so adding it globally just saves scoring calls.

---

## 4. Search terms

Today you run two keyword searches: US-wide "Software Engineer" (Indeed, 5,187 rows in 14 days) and ".NET" (New York). The US-wide one is mostly noise; it produced 17 curated jobs out of 5k. Swap it for terms that match your résumé, and split by board.

**Indeed** (`hours_old = 24`, `results_wanted = 50`; location "New York, NY", one copy with "Jersey City, NJ", and one with Remote checked):
- Backend Engineer
- Software Engineer II
- Python Engineer *or* Python Developer
- Golang Engineer
- AI Engineer, Applied AI Engineer, LLM Engineer
- Platform Engineer
- Forward Deployed Engineer: it showed up repeatedly in your Top 50, and it fits backend + LLM + customer-facing work
- New Grad Software Engineer 2026: you finished your MS in May 2026, so new-grad roles are open to you
- .NET Developer and **C# Developer** (keep .NET; add C#)

**LinkedIn** (see section 2: `hours_old = 4`, `results_wanted = 25`, location United States):
- Backend Engineer
- Software Engineer II
- AI Engineer
- Python Engineer
- Forward Deployed Engineer

**Keep:** Jobright (122 applications, 1 interview; your biggest single source), Freehire, and the Extension searches.

### Senior titles

At your request, `senior`, `sr.` and `sr` were removed from the curator's title excludes. Yesterday that filter dropped 109 + 11 jobs. Senior postings now reach the ranking, and the **6+ years** JD check still drops the ones out of reach. A "Senior" role asking for 3–5 years is a realistic stretch at 3 YOE. `staff`, `principal`, `lead` and `manager` stay excluded.

---

## 5. Outreach at 35 people a day

- Settings → Daily curator:
  - **People per day** (`outreach_count`): **25** for job-linked people. It is currently set to 30.
  - **People to know per day** (`outreach_network_count`): **10** for decision makers whether or not they are hiring.
  - Job slots nobody filled roll over to people to know.
- Every draft now names **one of your projects with its link**: Cloud Document Extraction Pipeline, PrivGuard Sentinel, or LazyCurl, whichever fits the person's team best.
- **LinkedIn caps invitations at roughly 100–200 a week, even with Premium.** 35 a day is about 245 a week, so plan on:
  - LinkedIn notes for about 15–20 a day: the hiring managers on jobs ranked "Apply now", plus the people to know;
  - **email** for the rest. Every contact has a guessed address from the company's email format. Verify it first (e.g. a free checker) before sending.
- Raise **LinkedIn searches · cap** (`linkedin_people_daily_cap`, now 30) to about **40**. It is only used when SearXNG finds nobody useful, but the network step adds up to 10 more companies a day.
