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

- Job listings are spread across 9+ platforms, many in German
- Every JD needs a tailored cover letter — generic ones go straight to bin
- Visa compatibility is unclear on most listings (Chancenkarte ≠ "no right to work")
- Interviews take weeks; by the time you hear back, you've forgotten what the role was

This tool automates the tedious parts (scraping, deduplication, scoring, cover letter drafting) so you can focus on the parts that actually matter: deciding which jobs to pursue and preparing for interviews.

---

## Features

**Pipeline**
- Scrapes 9 job sources daily on a schedule (APIs + HTML, auto-deduped by JD content hash)
- Detects and auto-translates German JDs to English before scoring
- RAG-augmented LLM scoring against your personal resume knowledge base
- A/B/C grading with source bonus (Relocate.me, Greenhouse, Lever, Bundesagentur)
- Cover letter generated per job, editable with 3 tone presets (Formal / Startup / Concise)

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

```
Phase 1 (Ingestor)   →   Phase 2 (Scorer)   →   Phase 3 (Dashboard)
9 sources scraped        RAG + LLM grades         Review, edit CL, apply,
into SQLite              each job, generates       track interviews,
auto-deduped             cover letter + scores     on-demand AI analysis
```

**Phase 1** pulls from 9 sources and deduplicates by JD content hash (chars 50–550, skipping platform boilerplate). **Phase 2** detects German JDs, translates them, scores against your candidate knowledge base via RAG, and grades A/B/C. **Phase 3** is a Streamlit dashboard for reviewing, editing, applying, and tracking your full interview pipeline.

---

## Directory Structure

```
job-hunter/
├── .env                          # API keys (not committed)
├── .env.example                  # Copy → .env, fill in keys
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── scheduler.py                  # Stdlib scheduler (runs inside Docker)
├── phase1_ingestor.py            # Scrape jobs (9 sources)
├── phase2_scorer.py              # LLM score + cover letter + interview brief
├── phase3_dashboard.py           # Streamlit review dashboard
├── check_api.py                  # Quick LLM + embedding connectivity check
├── config/
│   ├── grading_rules.md          # Scoring rules injected as LLM system prompt
│   └── search_targets.yaml       # Keywords, locations, ATS company slugs
├── candidate_kb/                 # Your resume knowledge base
│   ├── resume_bullets.md
│   ├── projects.md
│   └── visa_status.md
├── data/
│   └── jobs.db                   # SQLite database (not committed)
├── qdrant_data/                  # Local vector store (not committed)
├── logs/
│   └── pipeline.log              # Scheduler run history
└── utils/
    ├── db.py                     # SQLite helpers + status transitions
    ├── kb_loader.py              # Build Qdrant knowledge base from candidate_kb/
    ├── llm.py                    # OpenAI / Mistral / Azure / custom endpoint factory
    ├── company_researcher.py     # On-demand company profile (scrape + LLM)
    ├── salary_estimator.py       # On-demand salary estimate + negotiation tips
    └── visa_checker.py           # On-demand Chancenkarte visa compatibility analysis
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

The `pipeline` service runs `scheduler.py` which fires Phase 1 + Phase 2 daily at 07:30 (Europe/Berlin timezone). The `dashboard` service stays up continuously.

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
# CHAT_MODEL=mistral-large-latest
# EMB_MODEL=mistral-embed

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
| [EnglishJobs.de](https://englishjobs.de) | HTML scrape | English-only roles in Germany |
| [Bundesagentur für Arbeit](https://api.arbeitsagentur.de) | REST API | Official German job register |
| [Remotive](https://remotive.com) | JSON API | Remote-only, English |
| [Relocate.me](https://relocate.me) | HTML scrape | Roles with relocation support |
| [Jobicy](https://jobicy.com) | JSON API | Remote-only; geo exclusion filter |
| [Greenhouse ATS](https://boards-api.greenhouse.io) | JSON API | Per-company board; no auth needed |
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
| `expires_at` is in the past | → `expired` (no LLM call) |
| JD text shorter than 100 characters | → `error` (no LLM call) |

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
    │              └──────────────────────────┴─→ rejected         │
    ├─→ skipped                                                     │
    ├─→ error        (LLM failed 3× — retry from dashboard)        │
    └─→ expired      (expires_at passed — auto-marked at Phase 2)  ◄─┘
```

---

## Dashboard Features

### KPI Row
Pending review · Applied this week · In interview · Offers · Follow-up due · Scoring errors

### Statistics Panel
Grade distribution · Language requirement breakdown · Application funnel · Source yield table (A-grade rate, interview-to-apply rate) · Weekly apply trend (last 8 weeks)

### Job Detail Panel

**Scoring overview**
- Match score, fit grade, language requirement, visa classification, contract type
- Top 3 grading rationale bullets

**Duplicate application warning**
If you've previously applied to another role at the same company, a warning is shown with the prior job title and current status.

**Visa compatibility (🛂)**
Shows the coarse visa classification from scoring. For `eu_only` roles, a "Deep Analysis" button runs a Chancenkarte-specific LLM analysis: scans the JD for relevant phrases, reasons about whether a Chancenkarte holder can apply, and suggests how to address visa status in the cover letter and first contact.

**Salary estimate (💰)**
Generates an LLM salary estimate for the role (market range, confidence level, negotiation opening price and floor) using JD context, location, and company size signals. Links to Glassdoor, Kununu, and Levels.fyi for manual verification.

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
| Mistral (`mistral-large-latest`) | No (JSON mode) | 1 req/sec | 1024 |
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
| Visa analysis | 1 chat | 1,500–3,000 |

All on-demand analyses (visa, salary, company research) are opt-in per job — triggered by buttons in the dashboard.

---

## Other Notes

- Bundesagentur API runs with SSL verification disabled (`verify=False`). Remove when the API's cert is stable.
- All personal files (`.env`, `candidate_kb/*.md`, `config/grading_rules.md`, `config/search_targets.yaml`, `data/`, `qdrant_data/`) are excluded from git via `.gitignore`.
- The scheduler runs at 07:30 **Europe/Berlin** time (set via `TZ=Europe/Berlin` in `docker-compose.yml`). To change the time: edit the `command:` line in `docker-compose.yml` (args are `hour minute`).
- `check_api.py` verifies both chat and embedding API connectivity and prints the detected embedding dimension — useful after switching providers.
