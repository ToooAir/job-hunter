# Job Hunter

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)
![Streamlit](https://img.shields.io/badge/Streamlit-dashboard-FF4B4B?logo=streamlit&logoColor=white)
![LLM](https://img.shields.io/badge/LLM-OpenAI%20%7C%20Mistral%20%7C%20Azure-412991?logo=openai&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

A self-hosted job search pipeline for the German tech market: scrape → AI score → review & apply.

Built for international candidates job-hunting in Germany — with first-class support for **Chancenkarte holders**, German JD auto-translation, and visa compatibility analysis built in.

![Dashboard screenshot](docs/screenshot.png)

---

## Why I Built This

Searching for a tech job in Germany as a non-EU candidate is a different problem from a regular job search:

- Job listings are spread across 16+ platforms, many in German
- Every JD needs a tailored cover letter — generic ones go straight to bin
- Visa compatibility is unclear on most listings (Chancenkarte ≠ "no right to work")
- Interviews take weeks; by the time you hear back, you've forgotten what the role was

This tool automates the tedious parts (scraping, deduplication, scoring, cover letter drafting) so you can focus on the parts that actually matter: deciding which jobs to pursue and preparing for interviews.

---

## Features

**Pipeline**
- Scrapes 16 job sources daily on a schedule (APIs + HTML, auto-deduped by JD content hash)
- Detects and auto-translates German JDs to English before scoring
- JDs sanitised before LLM injection — resists prompt injection attacks embedded in job postings
- RAG-augmented LLM scoring against your personal resume knowledge base
- A/B/C grading with source bonus (Relocate.me, Greenhouse, Lever, Bundesagentur)
- Cover letter generated per job, editable with 3 tone presets (Formal / Startup / Concise)

**Semi-auto Apply**
- Geo triage: bare "Remote" listings classified by Germany-hiring eligibility (free regex rules daily; optional LLM pass on demand), mislabelled German locations normalised so they reach the queue
- ATS scan: per-job ATS platform classification, liveness check, direct apply-URL backfill
- Ranked apply queue with company-level dedup gate and daily budget; Stage 1 generates grounded draft answers for each queued job
- Browser extension: one-click profile autofill on ATS forms (CV upload included), 💰 salary-expectation and 📄 cover-letter answer panel, submission accounting back into the tracker — human reviews and submits, always

**Chancenkarte & Visa**
- Coarse visa classification on every job (`open` / `eu_only` / `sponsored` / `unclear`)
- On-demand deep Chancenkarte compatibility analysis: scans JD for restrictive phrases, reasons about §20a AufenthG eligibility, suggests how to address visa status in cover letter and first contact

**On-demand Analysis (per job, one click)**
- Salary estimate with negotiation guidance (market range, opening price, floor)
- Company research (scrapes about page + LLM summary: tech stack, culture, relocation stance)
- Interview prep brief: role summary, key requirements, inferred pain points, your matched experience, 5 likely questions

**Pipeline Tracking**
- Full status flow: `scored → applied → interview_1 → interview_2 → offer / rejected`
- Structured interview records per round (date, format, questions, self-rating, impressions)
- Follow-up reminders (auto-set 7 days after applying, customisable)
- Duplicate application warning (flags if you've applied to the same company before)
- Stats dashboard: grade distribution, source yield table, application funnel, weekly trend

**Flexibility**
- Works with OpenAI, Mistral AI (free tier), Azure OpenAI, or any local/custom LLM endpoint
- Fully Dockerised — one `docker compose up -d` to run
- Dashboard UI supports **English / 中文** — toggle in the sidebar, no restart needed
- All personal data (resume, keys, DB) stays local, never leaves your machine

---

## How It Works

Daily pipeline (runs inside Docker, interval-based catch-up):

```
phase1_ingestor → remote_geo_triage → phase2_scorer → ats_scan → apply_stage1
16 sources        Germany-eligibility  RAG + LLM       ATS platform  apply-queue
→ SQLite,         relabeling of        scoring, CL,    + liveness    drafts for
auto-deduped      remote locations     translation     check         human review
```

**Phase 1** pulls from 16 sources and deduplicates by JD content hash (chars 50–550, skipping platform boilerplate). **Geo triage** relabels bare `Remote` listings by Germany-hiring eligibility and normalises German locations the keyword filters would miss (`"Dresden (DE)"`, `"54595 Prüm"`, second-tier cities) — outright-foreign listings are excluded from LLM scoring entirely, cutting scoring spend roughly in half. **Phase 2** detects German JDs, translates them, scores against your candidate knowledge base via RAG, and grades A/B/C. **ats_scan** classifies which ATS each queue candidate runs on (Greenhouse / Lever / Ashby / Workable / Personio / …) and whether the posting is still live. **Stage 1** builds a ranked apply queue (company-level dedup, daily budget) and generates grounded application drafts.

**Phase 3** is a Streamlit dashboard for reviewing, editing, applying, and tracking your full interview pipeline. A companion browser extension autofills ATS forms from your profile and answers open questions via copy-paste — **you always review and click Submit yourself; nothing is ever auto-submitted.**

---

## Directory Structure

```
job-hunter/
├── .env                              # API keys (not committed)
├── .env.example                      # Copy → .env, fill in keys
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── run_pipeline.sh                   # Shell wrapper for launchd / manual runs
├── scheduler.py                      # Catch-up scheduler: interval-based, resumes interrupted runs
├── phase1_ingestor.py                # Scrape jobs (16 sources)
├── remote_geo_triage.py              # Relabel remote/mislabelled locations by Germany eligibility
├── phase2_scorer.py                  # LLM score + cover letter + interview brief
├── ats_scan.py                       # ATS platform classification + liveness for queue candidates
├── apply_stage1.py                   # Apply-queue draft generation
├── apply_api.py                      # Local sidecar API for the browser extension (127.0.0.1:8531)
├── phase3_dashboard.py               # Streamlit review dashboard (EN / 中文)
├── extension/                        # Browser extension: ATS autofill + answer panel
├── tests/                            # Unit tests (run inside the container)
├── check_api.py                      # Quick LLM + embedding connectivity check
├── LICENSE
├── README.md
├── README_TC.md                      # Traditional Chinese README
├── config/
│   ├── grading_rules.md              # Scoring rules injected as LLM system prompt
│   ├── grading_rules.md.example      # Template — copy and customise
│   ├── search_targets.yaml           # Keywords, locations, ATS company slugs
│   └── search_targets.yaml.example  # Template — copy and customise
├── candidate_kb/                     # Your resume knowledge base (RAG source)
│   ├── resume_bullets.md
│   ├── resume_bullets.md.example
│   ├── projects.md
│   ├── projects.md.example
│   ├── visa_status.md
│   └── visa_status.md.example
├── docs/
│   └── screenshot.png
├── data/
│   └── jobs.db                       # SQLite database (not committed)
├── qdrant_data/                      # Local vector store (not committed)
├── logs/
│   └── pipeline.log                  # Scheduler run history
└── utils/
    ├── db.py                         # SQLite helpers + status transitions
    ├── kb_loader.py                  # Build Qdrant knowledge base from candidate_kb/
    ├── llm.py                        # OpenAI / Mistral / Azure / custom endpoint factory
    ├── geo_de.py                     # Germany location matching (single source of truth)
    ├── apply_queue.py                # Ranked apply queue (dedup gate, budget, ATS bias)
    ├── apply_llm.py                  # Shared LLM plumbing for apply flows
    ├── apply_verifier.py             # Fact-check pass over generated drafts
    ├── company_researcher.py         # On-demand company profile (scrape + LLM)
    ├── levels_scraper.py             # Levels.fyi aggregate stats scraper (cache + FX conversion)
    ├── salary_estimator.py           # On-demand salary estimate + negotiation tips
    └── visa_checker.py               # On-demand Chancenkarte visa compatibility analysis
```

---

## Quick Start

### Option A — Docker (recommended)

Docker handles scheduling and keeps your host environment clean.

```bash
# 1. Copy and fill in config files
cp .env.example .env
# Edit .env with your API key and provider

# Fill in your profile
nano config/grading_rules.md
nano config/search_targets.yaml
nano candidate_kb/resume_bullets.md
nano candidate_kb/projects.md
nano candidate_kb/visa_status.md

# 2. Build and start
docker compose up -d

# 3. Build the knowledge base (run once, and after editing candidate_kb/)
docker compose exec pipeline python utils/kb_loader.py

# 4. Verify API connectivity
docker compose exec pipeline python check_api.py

# Dashboard opens at http://localhost:8501
```

The `pipeline` service runs `scheduler.py`, an interval-based catch-up scheduler: the full chain (scrape → geo triage → score → ATS scan → draft generation) fires as soon as the last finished run ended ≥20h ago **and** the machine is online — so a laptop that was asleep at any fixed hour still catches up when it wakes, and a run interrupted mid-way resumes from the first unfinished stage. Tune with `PIPELINE_MIN_INTERVAL_HOURS`. The `dashboard` and `apply_api` services stay up continuously.

**Rebuild after code changes:**
```bash
docker compose build pipeline
docker compose up -d
```

**Rescore all jobs** (after editing `grading_rules.md`):
```bash
docker compose exec pipeline python phase2_scorer.py --rescore
```

### Option B — Local (venv)

```bash
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Fill in .env and config files as above

python utils/kb_loader.py       # Build knowledge base once

python phase1_ingestor.py       # Scrape
python phase2_scorer.py         # Score
streamlit run phase3_dashboard.py
```

---

## Configuration

### `.env`

```env
# LLM provider: openai | mistral | azure | custom
LLM_PROVIDER=openai

# OpenAI
OPENAI_API_KEY=sk-...

# Mistral AI (free tier available)
# LLM_PROVIDER=mistral
# MISTRAL_API_KEY=your_key_here
# CHAT_MODEL=mistral-small-2603
# EMB_MODEL=mistral-embed
# MISTRAL_MAX_CONCURRENT=3   # concurrent scoring workers (default 3, tune by TPM tier)

# Azure OpenAI
# LLM_PROVIDER=azure
# AZURE_ENDPOINT=https://...
# AZURE_API_VERSION=2024-08-01-preview
# AZURE_CHAT_DEPLOYMENT=gpt-4o
# AZURE_EMB_DEPLOYMENT=text-embedding-3-small

# Custom / local (LiteLLM, Ollama, vLLM, etc.)
# LLM_PROVIDER=custom
# CUSTOM_BASE_URL=http://localhost:11434/v1

DB_PATH=./data/jobs.db
QDRANT_PATH=./qdrant_data

# Levels.fyi salary data (optional — used by salary estimator)
# HOME_COUNTRY=germany           # Location slug for remote roles (default: germany)
# LEVELS_CACHE_TTL_DAYS=7        # How long to cache scraped data (default: 7 days)
# FALLBACK_USD_EUR_RATE=0.92     # Fallback FX rate if live APIs are unavailable
```

> **Switching providers**: If you change the embedding model (e.g. from OpenAI `text-embedding-3-small` at 1536-dim to Mistral `mistral-embed` at 1024-dim), you must rebuild the knowledge base: `python utils/kb_loader.py`

### `config/grading_rules.md`

Defines how the LLM grades jobs and formats cover letters. Fill in your tech stack, experience level, language skills, and target locations. This file is injected as the LLM system prompt on every Phase 2 run — keep it concise to minimise token usage.

### `config/search_targets.yaml`

Controls which keywords and locations each scraper uses. For Greenhouse and Lever, add company slugs (verify at `https://boards.greenhouse.io/{slug}` and `https://jobs.lever.co/{slug}`). Invalid slugs are silently skipped at runtime. Editable directly from the dashboard.

### `candidate_kb/`

Three Markdown files that form your RAG knowledge base:

- `resume_bullets.md` — work history in STAR format with measurable outcomes
- `projects.md` — key projects with tech stack and concrete results
- `visa_status.md` — work authorisation, availability, location

After editing any of these, rebuild the knowledge base:
```bash
docker compose exec pipeline python utils/kb_loader.py
```

Phase 2 warns you if the knowledge base is stale relative to your `candidate_kb/` files.

### Using AI to generate your config files

The quality of scoring and cover letters depends almost entirely on how well these files are written. Use the prompts below with any LLM (Claude, ChatGPT, etc.) to generate a solid first draft.

**`candidate_kb/resume_bullets.md`**

Paste your CV or LinkedIn experience, then send:

```
Convert my work experience below into STAR-format bullet points for a RAG knowledge base.
Requirements:
- Each bullet must include a specific measurable outcome (%, time saved, scale, etc.)
- Include all relevant technology names exactly as they appear in job postings
- Group by role with company name and date range
- Be specific — avoid vague phrases like "improved performance" or "worked on backend"

[paste your CV / LinkedIn experience here]
```

**`candidate_kb/projects.md`**

```
Summarise my key projects below for a RAG knowledge base used to write cover letters.
For each project include: what it does, the tech stack (exact library/framework names),
scale or impact metrics, and your specific contribution.

[paste your project descriptions here]
```

**`candidate_kb/visa_status.md`**

```
Write a short RAG knowledge base entry describing my work authorisation status.
Include: visa type and what it permits, location preference and flexibility,
language skills (level for each), availability date, and willingness to relocate.

My situation: [describe your visa, location, languages, availability]
```

**`config/grading_rules.md`**

```
I am customising a job-scoring system. Help me fill in the candidate profile section
of the grading rules below.

My profile:
- Current role / years of experience: [e.g. Backend Engineer, 5 years]
- Core tech stack: [e.g. Python, FastAPI, Node.js, GCP, Docker]
- Target roles in Germany: [e.g. Backend Engineer, AI Engineer, Platform Engineer]
- Language skills: [e.g. English fluent, German A2]
- Location preference: [e.g. Hamburg or Remote]
- Visa situation: [e.g. Chancenkarte holder, need sponsorship for long-term]

Replace the candidate profile section in this file with specific, concrete values:

[paste the current contents of config/grading_rules.md]
```

> After generating the files, read through them and correct anything the LLM invented or got wrong. The RAG system is only as accurate as the facts you put in.

---

## Job Sources

| Source | Method | Notes |
|--------|--------|-------|
| [Arbeitnow](https://www.arbeitnow.com) | JSON API | Stable; English and German roles |
| [WeAreDevelopers](https://www.wearedevelopers.com) | Private REST API | Germany's largest dev job board; Germany + remote passes |
| [EnglishJobs.de](https://englishjobs.de) | HTML scrape | English-only roles in Germany |
| [Bundesagentur für Arbeit](https://api.arbeitsagentur.de) | REST API | Official German job register |
| [Remotive](https://remotive.com) | JSON API | Remote-only, English |
| [Relocate.me](https://relocate.me) | HTML scrape | Roles with relocation support |
| [Jobicy](https://jobicy.com) | JSON API | Remote-only; geo exclusion filter |
| [Ashby ATS](https://jobs.ashbyhq.com) | GraphQL API | Per-company board; no auth needed |
| [Workable ATS](https://apply.workable.com) | REST API | Per-company board; built-in 429 backoff |
| [We Work Remotely](https://weworkremotely.com) | RSS feed | Programming + DevOps/sysadmin feeds |
| [Greenhouse ATS](https://boards-api.greenhouse.io) | JSON API | Per-company board; no auth needed |
| [Heise Jobs](https://jobs.heise.de) | HTML scrape | German IT job board; SSR with cumulative pagination |
| [Personio ATS](https://personio.de) | XML feed | Per-company feed at `{slug}.jobs.personio.de/xml` |
| [Welcome to the Jungle](https://www.welcometothejungle.com) | Algolia API | EU startup jobs; English-only, remote-EU + Germany filter |
| [Lever ATS](https://api.lever.co) | JSON API | Per-company board; no auth needed |
| LinkedIn / StepStone / other | Manual via dashboard | Search buttons + manual job entry form |

GermanTechJobs is currently disabled (JS SPA — requires Playwright).

---

## Scoring

### Deduplication

Jobs are deduplicated by an MD5 hash of characters 50–550 of the JD text. The first 50 characters are skipped to avoid platform boilerplate (e.g. "We are an equal opportunity employer…") causing false cross-source duplicates. The same position posted on multiple job boards is stored only once.

### German JD Translation

Phase 2 detects German-language JDs using a token frequency heuristic (>8% German function words). Detected JDs are translated to English via a single LLM call before scoring and embedding. The translation is cached in the database — rescoring reuses it without an extra API call.

### Pre-flight Filters (before any LLM call)

| Condition | Action |
|-----------|--------|
| Age from `fetched_at` exceeds source TTL (see table below) | → `expired` (TTL-based, runs first) |
| `expires_at` is in the past | → `expired` (explicit deadline) |
| Location names a non-German country/city outright, or geo triage labelled it `Remote — non-EU` | → skipped, stays `un-scored` (no LLM call; TTL cleans it up) |
| JD text shorter than 100 characters | → `error` (no LLM call) |

**Source TTL defaults** (applied when `expires_at` is not set by the scraper):

| Source | TTL |
|--------|-----|
| Greenhouse, Lever | 30 days |
| Remotive, Jobicy | 60 days |
| All others | 45 days |

TTL expiry also runs on every dashboard page load — so stale jobs are cleaned up even without running Phase 2.

### Grading

Grading logic lives in `config/grading_rules.md` and is applied by the LLM. A source bonus is applied in Python after scoring:

| Grade | Condition |
|-------|-----------|
| A | score ≥ 80, language not `de_required` |
| B | 60 ≤ score < 80, language not `de_required` |
| C | score < 60 or `de_required` |

**Source bonus** (applied in Python post-LLM):

| Source | Bonus | Reason |
|--------|-------|--------|
| relocateme | +10 | Company actively offers relocation support |
| greenhouse | +5 | Direct ATS post — active hiring signal |
| lever | +5 | Direct ATS post — active hiring signal |
| bundesagentur | +5 | Official register; higher proportion of visa-friendly employers |

**Visa classification** (from JD text): `open` · `eu_only` · `sponsored` · `unclear`

---

## Job Status Flow

```
un-scored
    │
    ▼
  scored ──────────────────────────────────────────────────────────┐
    │                                                              │
    ├─→ applied → interview_1 → interview_2 → offer               │
    │      │       └──────────────────────────┴─→ rejected         │
    │      └─→ ghosted   (auto after 35 days with no response)      │
    ├─→ skipped                                                     │
    ├─→ error        (LLM error — retry from dashboard)             │
    └─→ expired      (expires_at passed OR TTL exceeded)           ◄─┘
```

Expired jobs are removed from the main job list automatically. TTL is checked at Phase 2 start and on every dashboard page load.

---

## Dashboard Features

### KPI Row
Pending review · Applied this week · In interview · Offers · Follow-up due · Scoring errors

### Statistics Panel
Grade distribution · Language requirement breakdown · Application funnel · Source yield table (A-grade rate, interview-to-apply rate) · Weekly apply trend (last 8 weeks)

### Job List

The job table includes an **age** column showing how long ago the listing was fetched:

| Indicator | Meaning |
|-----------|---------|
| 🟢 Xd | Fetched < 14 days ago — likely still active |
| 🟡 Xd | 14–30 days — worth checking before applying |
| 🔴 Xd | 30+ days — high chance the listing is closed |

Jobs that exceed their source TTL are automatically marked `expired` and removed from the list.

### Job Detail Panel

**Scoring overview**
- Match score, fit grade, language requirement, visa classification, contract type
- Top 3 grading rationale bullets

**Duplicate application warning**
If you've previously applied to another role at the same company, a warning is shown with the prior job title and current status.

**Visa compatibility (🛂)**
Shows the coarse visa classification from scoring. For `eu_only` roles, a "Deep Analysis" button runs a Chancenkarte-specific LLM analysis: scans the JD for relevant phrases, reasons about whether a Chancenkarte holder can apply, and suggests how to address visa status in the cover letter and first contact.

**Salary estimate (💰)**
Generates an LLM salary estimate for the role (market range, confidence level, negotiation opening price and floor) using JD context, location, and company size signals. Levels.fyi aggregate statistics (median, P25/P75/P90) are scraped automatically via Playwright and injected as a market reference table into the LLM prompt when available — giving the model a calibrated anchor rather than estimating from training data alone. Remote roles use `HOME_COUNTRY` as the reference location. Data is cached for 7 days (configurable). Links to Glassdoor, Kununu, and Levels.fyi are also shown for manual verification.

**Company research (🔍)**
Scrapes the company's about/homepage and combines it with the JD to produce a structured company profile: overview, tech stack, culture, international-friendliness, and interview talking points.

**Cover letter**
- Editable text area with live word count (200–400 word target)
- Tone selector: **Formal** (corporate), **Startup** (direct, personality), **Concise** (≤200 words)
- Regenerate with selected tone via one click
- Download as `.docx` for upload-required applications

**Action buttons** (change with pipeline stage):
- `scored`: Open job · Copy CL · Mark Applied · Skip · Rescore
- `applied`: Open job · Interview Invite (auto-generates prep brief) · Rejected
- `interview_1/2`: Open job · Advance stage · Rejected
- `offer/rejected/skipped`: Open job only

**Interview prep brief**
Auto-generated when a job is advanced to interview stage. Covers: role summary, key technical requirements, inferred challenges, your relevant experience matched from the KB, and 5 likely interview questions. Regenerate on demand; download as Markdown.

**Interview records**
Log each interview round: date, interviewer, format (phone/video/onsite/technical), questions asked, self-rating (1–5), and impressions. Stored per job, viewable and deletable from the dashboard.

**Notes & follow-up**
Free-text notes field. Follow-up reminder date (auto-set to 7 days post-apply, customisable).

### Other Left-column Tools
- **Pipeline log viewer**: last 100 lines of `logs/pipeline.log` + "Run now" button
- **Search targets manager**: edit keywords, Greenhouse slugs, Lever slugs directly from the dashboard
- **Manual job entry**: paste any JD manually (LinkedIn, StepStone, company career pages)
- **LinkedIn search buttons**: pre-configured search links for your target keywords

---

## LLM Provider Notes

| Provider | Structured Outputs | Rate limit | Embedding dim |
|----------|--------------------|------------|---------------|
| OpenAI (`gpt-4o`) | Yes | None (tier-dependent) | 1536 |
| Mistral (`mistral-small-2603`) | No (JSON mode) | 1 RPS / 375,000 TPM | 1024 |
| Azure OpenAI | Yes | None (deployment-dependent) | 1536 |
| Custom / local | No (JSON mode) | None | varies |

**Token usage per job** (approximate):

| Operation | API calls | Tokens |
|-----------|-----------|--------|
| Scoring + cover letter | 2 (1 embed batch + 1 chat) | 3,500–8,000 |
| German translation (if needed) | 1 chat | 1,000–3,000 |
| Interview prep brief | 2 (1 embed + 1 chat) | 4,000–7,500 |
| Cover letter regeneration | 2 (1 embed + 1 chat) | 2,500–5,000 |
| Company research | 1 chat | 2,000–4,000 |
| Salary estimate | 1 chat | 1,500–3,000 |
| Visa analysis | 1 chat | 2,000–4,000 |

All on-demand analyses (visa, salary, company research) are opt-in per job — triggered by buttons in the dashboard.

---

## Other Notes

- Bundesagentur API runs with SSL verification disabled (`verify=False`). Remove when the API's cert is stable.
- All personal files (`.env`, `candidate_kb/*.md`, `config/grading_rules.md`, `config/search_targets.yaml`, `data/`, `qdrant_data/`) are excluded from git via `.gitignore`.
- Scheduling is interval-based (`PIPELINE_MIN_INTERVAL_HOURS`, default 20h) with an online probe — clock arguments to `scheduler.py` are deprecated. The container timezone is **Europe/Berlin** (`TZ` in `docker-compose.yml`); dashboard and apply-API timestamps rely on it.
- The last three pipeline stages (geo triage, ATS scan, draft generation) are best-effort: if one fails, the run still counts as a successful scrape+score catch-up.
- `check_api.py` verifies both chat and embedding API connectivity and prints the detected embedding dimension — useful after switching providers.
