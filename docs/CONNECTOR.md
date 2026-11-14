# Connector (MCP)

JobNavigator is a remote MCP server at `<your address>/mcp`. Any MCP client can ask it about
your search: Claude (web, desktop, mobile), Claude Code, or another agent.

The address and the buttons are in **Settings › Advanced › Connector (MCP)**.

## Connect Claude (web, desktop, mobile)

1. In Claude: **Settings › Connectors › Add custom connector**, paste the address
   (`https://jobs.example.com/mcp`).
2. Claude opens a JobNavigator page. Use the browser where you are signed in to the
   dashboard, pick whether Claude may also update leads and applications, and press **Allow**.

Claude's servers make the calls, not your device, so the address must be reachable from the
internet over HTTPS. JobNavigator builds its own URLs from `JN_PUBLIC_URL`, else
`OIDC_PUBLIC_URL`, else `GMAIL_PUBLIC_URL`, else the host the proxy forwards. Set
`JN_PUBLIC_URL=https://jobs.example.com` when HTTPS ends at a tunnel in front of Caddy and none
of the others is set.

## Connect Claude Code or another agent

Press **Read token** or **Read + update token**, then **Copy Claude Code command**:

```sh
claude mcp add --transport http jobnavigator https://jobs.example.com/mcp \
  --header "Authorization: Bearer <token>"
```

Any other client: the address, and the header `Authorization: Bearer <token>`. On the same
machine `http://localhost/mcp` works too. A token is shown once and lasts a year.

## What a connected app gets

| Tool | Gives |
| --- | --- |
| `overview` | Feed size, applications by status, reply and interview rates, upcoming interviews, leads due |
| `insights` | What works: outcomes by source, role lane, résumé, posting age, sponsor record, apply link and week; outreach by kind of person; the last curator and scout runs with their errors |
| `journal` | Dated notes on what was changed, decided or found |
| `list_applications`, `get_application` | Applications as short rows; one in full with notes, history, interviews |
| `search_jobs`, `get_job`, `today` | Jobs by text, fit, status or age; one posting in full; the curator's list for a day |
| `list_resumes`, `get_resume` | Résumé names; one résumé as text or JSON |
| `get_profile` | Contact, work authorization, preferences, networking profile; projects and saved answers on request |
| `list_leads`, `get_lead` | People to reach out to; one in full with talking points and drafts |
| `update_lead`, `add_lead`, `update_application`, `log_note` | Only with **update** access: a journal note, lead status, notes and drafts, new or already-contacted people, application status and notes |

Lists are short rows with ids; a `get_` tool returns one item in full, so an answer costs few
tokens. A connection sees one account's data. It cannot change settings, run scrapes, send
anything, delete anything, or read demographics answers.

## Logging people other agents contact

The curator and the lead scout are not the only source of leads. An agent that finds or
messages someone itself (an outreach agent, a script, you) records the person so the People
screen stays the one list, and so nobody is contacted twice.

`add_lead` is an upsert. The same person sent again (same LinkedIn URL, else email, else name
and company) is the lead they already are: empty fields are filled, the text sent replaces an
old draft, and the status moves forward only. So an agent can report after every step without
checking first, and two agents reporting the same message make one row.

| Field | Meaning |
| --- | --- |
| `name` | Required |
| `company`, `title`, `linkedin_url`, `email`, `location` | Who they are |
| `role` | `founder`, `hiring_manager`, `recruiter`, `engineer` |
| `kind` | `job`, `network`, `hiring_post`, `project_fit`, `alumni`, `founder` |
| `status` | `new` (default), `requested` (connection sent), `connected`, `messaged`, `replied`, `referral`, `snoozed`, `skip` |
| `contacted_on`, `next_action_at` | Dates (`YYYY-MM-DD`): when the step was taken, when to follow up |
| `linkedin_note`, `email_draft` | The text that was sent (a note is at most 300 characters) |
| `why`, `evidence`, `talking_points`, `show_project`, `score` | Why this person, and what to say |
| `job_id` | A job's id or dashboard number, when it is about one job |
| `agent` | Who is reporting; shown as the lead's source (`agent:muse`) |

Over MCP, with a **Read + update** token:

```sh
curl -s https://jobs.example.com/mcp -H "Authorization: Bearer <connector token>" \
  -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":
  {"name":"add_lead","arguments":{"name":"Asha Rao","company":"Acme","status":"requested",
   "linkedin_url":"https://www.linkedin.com/in/asha-rao","contacted_on":"2026-11-13",
   "linkedin_note":"Hi Asha, ...","kind":"alumni","agent":"muse"}}}'
```

Over the REST API, `POST /api/contacts` takes the same fields, one contact or a list of up to
100, and answers each with `created: true | false`. It uses the dashboard's own credentials,
not a connector token: `X-API-Key` (the install password) and `X-Account-Token` (the profile's
API token, from `POST /api/auth/verify-pin`). `GET /api/contacts` and
`PATCH /api/contacts/{id}` read and update them.

```sh
curl -s https://jobs.example.com/api/contacts -H "X-API-Key: <install key>" \
  -H "X-Account-Token: <account token>" -H "Content-Type: application/json" \
  -d '[{"name":"Asha Rao","company":"Acme","status":"messaged","email_draft":"...","agent":"muse"}]'
```

A token belongs to one profile. To log people on a second profile, make the token while that
profile is the one open in the dashboard.

## Disconnect

**Disconnect all** ends every connection and token of your account at once. Changing your PIN
does the same. In Claude you can also remove the connector.

## For developers

`backend/connector/`: `routes.py` (`POST /mcp`, Streamable HTTP, stateless JSON), `tools.py`
(add a tool: one function and one `Tool(...)` entry), `oauth.py` (OAuth 2.1 with PKCE and
dynamic client registration; metadata under `/.well-known/`), `tokens.py`. Nothing is stored:
clients, codes and tokens are values signed with the install secret. Tests:
`backend/tests/test_connector.py`.
