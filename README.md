# Job Automation

A self-hosted job search pipeline: scrape → AI score → review & apply. Three phases connect job discovery, LLM-based grading with cover letter generation, and a Streamlit dashboard for managing your pipeline.

---

## How It Works

```
Phase 1 (Ingestor)   →   Phase 2 (Scorer)   →   Phase 3 (Dashboard)
9 sources scraped        LLM grades each job      Review, edit CL, apply
into SQLite              + generates cover letter  track interview pipeline
```

**Phase 1** pulls from 9 sources (aggregator APIs, ATS boards, job portals) and deduplicates by URL hash. **Phase 2** scores each unscored job against your candidate knowledge base using RAG + LLM, generates a cover letter, and grades A/B/C. **Phase 3** is a Streamlit dashboard where you review jobs, edit cover letters, download `.docx`, and track your pipeline from application through offer.

---

## Directory Structure

```
job-automation/
├── .env                          # API keys (not committed)
├── .env.example                  # Copy → .env, fill in keys
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── scheduler.py                  # Stdlib scheduler (runs inside Docker)
├── run_pipeline.sh               # Manual trigger wrapper with 5 MB log rotation
├── phase1_ingestor.py            # Scrape jobs (9 sources)
├── phase2_scorer.py              # LLM score + cover letter + interview brief
├── phase3_dashboard.py           # Streamlit review dashboard
├── config/
│   ├── grading_rules.md          # Scoring rules injected as LLM system prompt
│   ├── grading_rules.md.example  # Template — copy and fill in your profile
│   ├── search_targets.yaml       # Keywords, locations, ATS company slugs
│   └── search_targets.yaml.example
├── candidate_kb/                 # Your resume knowledge base
│   ├── resume_bullets.md
│   ├── projects.md
│   ├── visa_status.md
│   ├── resume_bullets.md.example
│   ├── projects.md.example
│   └── visa_status.md.example
├── data/
│   └── jobs.db                   # SQLite database (not committed)
├── qdrant_data/                  # Local vector store (not committed)
├── logs/
│   └── pipeline.log              # Scheduler run history
└── utils/
    ├── db.py                     # SQLite helpers + status transitions
    ├── kb_loader.py              # Build Qdrant knowledge base from candidate_kb/
    └── llm.py                    # OpenAI / Azure / custom endpoint factory
```

---

## Quick Start

### Option A — Docker (recommended)

Docker handles scheduling and keeps your host environment clean.

```bash
# 1. Copy and fill in config files
cp .env.example .env
cp config/grading_rules.md.example config/grading_rules.md
cp config/search_targets.yaml.example config/search_targets.yaml
cp candidate_kb/resume_bullets.md.example candidate_kb/resume_bullets.md
cp candidate_kb/projects.md.example candidate_kb/projects.md
cp candidate_kb/visa_status.md.example candidate_kb/visa_status.md

# Edit each file with your own details before continuing.

# 2. Build and start
docker compose up -d

# 3. Build the knowledge base (run once after filling candidate_kb/)
docker compose exec pipeline python -m utils.kb_loader

# Dashboard opens at http://localhost:8501
```

The `pipeline` service runs `scheduler.py` which fires Phase 1 + Phase 2 daily at the configured time (default 07:30). The `dashboard` service stays up continuously.

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
# ... copy and fill the other example files as above

python -m utils.kb_loader       # Build knowledge base once

python phase1_ingestor.py       # Scrape
python phase2_scorer.py         # Score
streamlit run phase3_dashboard.py
```

---

## Configuration

### `.env`

```
OPENAI_API_KEY=sk-...
QDRANT_PATH=./qdrant_data
DB_PATH=./data/jobs.db

# Azure OpenAI (optional — leave blank to use OpenAI directly)
# LLM_PROVIDER=azure
# AZURE_ENDPOINT=https://...
# AZURE_API_VERSION=2024-08-01-preview
# AZURE_CHAT_DEPLOYMENT=gpt-4o
# AZURE_EMB_DEPLOYMENT=text-embedding-3-small
```

### `config/grading_rules.md`

Defines how the LLM grades jobs. Fill in your tech stack, experience level, language skills, and target locations. This file is injected as the LLM system prompt on every Phase 2 run.

### `config/search_targets.yaml`

Controls which keywords and locations each scraper uses. For Greenhouse and Lever, add company slugs directly (verified at `https://job-boards.greenhouse.io/{slug}` and `https://jobs.lever.co/{slug}`). Invalid slugs are silently skipped at runtime.

### `candidate_kb/`

Three Markdown files that form your RAG knowledge base:

- `resume_bullets.md` — work history in STAR format with measurable outcomes
- `projects.md` — key projects with tech stack and concrete results
- `visa_status.md` — work authorization, availability, location

After editing any of these, rebuild the knowledge base:
```bash
# Docker
docker compose exec pipeline python -m utils.kb_loader

# Local
python -m utils.kb_loader
```

Phase 2 warns you if the knowledge base is stale relative to your `candidate_kb/` files.

---

## Job Sources

| Source | Method | Notes |
|--------|--------|-------|
| [Arbeitnow](https://www.arbeitnow.com) | JSON API | Stable; English-language roles |
| [EnglishJobs.de](https://englishjobs.de) | HTML scrape | English-only roles in Germany |
| [Bundesagentur für Arbeit](https://api.arbeitsagentur.de) | REST API | Official German job register |
| [Remotive](https://remotive.com) | JSON API | Remote-only, English |
| [Relocate.me](https://relocate.me) | HTML scrape | Roles with relocation support |
| [Jobicy](https://jobicy.com) | JSON API | Remote-only; geo exclusion filter |
| [Greenhouse ATS](https://boards-api.greenhouse.io) | JSON API | Per-company board; no auth needed |
| [Lever ATS](https://api.lever.co) | JSON API | Per-company board; no auth needed |
| LinkedIn / StepStone / other | Manual via dashboard | One-click search buttons in dashboard |

GermanTechJobs is currently disabled (JS SPA — requires Playwright).

---

## Scoring

### Deduplication

Jobs are deduplicated by an MD5 hash of characters 50–550 of the JD text. The first 50 characters are skipped to avoid platform boilerplate (e.g. "We are an equal opportunity employer…") causing false cross-source duplicates. The same position posted on multiple job boards is stored only once.

### Pre-flight filters (before any LLM call)

Phase 2 checks every unscored job before spending tokens:

| Condition | Action |
|-----------|--------|
| `expires_at` is in the past | → `expired` (no LLM call) |
| JD text shorter than 100 characters | → `error` (no LLM call) |

This prevents wasting API budget on stale listings or title-only entries that scrapers occasionally return.

### Grading

Grading logic lives in `config/grading_rules.md` and is applied by the LLM. A source bonus is applied in Python after scoring:

| Grade | Condition | Action |
|-------|-----------|--------|
| A | score ≥ 80, language not `de_required` | Priority apply |
| B | 60 ≤ score < 80, language not `de_required` | Consider |
| C | score < 60 or `de_required` | Skip this week |

**Language classification** (derived from JD text):
`en_required` → `de_plus` → `de_required` → `unknown`

**Source bonus** (applied in Python post-LLM, before grade assignment):

| Source | Adjustment | Reason |
|--------|-----------|--------|
| relocateme | +10 | Company actively offers relocation support |
| greenhouse | +5 | Direct ATS post — active hiring |
| lever | +5 | Direct ATS post — active hiring |
| bundesagentur | +5 | Official register; higher proportion of visa-friendly employers |
| others | 0 | Neutral |

**Visa classification** (derived from JD text):
`open` → `eu_only` → `sponsored` → `unclear`

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

- **KPI row**: pending review / applied this week / interview-stage / offers / follow-up due / scoring errors
- **Statistics panel** (expandable): grade distribution / language requirement breakdown / application funnel / source yield table (A-grade rate, interview rate) / weekly apply trend
- **Filters**: grade / language requirement / source / status (including `error`, `expired`)
- **Job detail panel**:
  - Score metrics (match score, grade, language req, visa, contract type) + grading rationale
  - Visa restriction banner: red warning for `eu_only` roles, green highlight for `sponsored` roles
  - Editable cover letter with live word count
  - Download `.docx` (for upload-required applications)
  - Action buttons (status-conditional): open job page / copy CL / mark applied / skip / rescore / advance pipeline stage / mark rejected
  - **Interview prep brief** (shown for `interview_1` / `interview_2` / `offer`): LLM-generated structured prep sheet auto-created when a job is advanced to interview stage. Covers role summary, key requirements, inferred challenges, your relevant experience matched from the KB, and 5 likely interview questions. Regenerate on demand; download as Markdown for offline reading.
  - Follow-up reminder (auto-set to 7 days post-apply, customisable date)
  - Notes field
- **Search targets manager** (expandable): edit keywords, Greenhouse slugs, and Lever slugs directly from the dashboard, saved back to `config/search_targets.yaml`
- **Pipeline log viewer** (expandable): last 100 lines of `logs/pipeline.log` + "Run now" button to trigger Phase 1+2 immediately

---

## Notes

- Phase 2 uses `gpt-4o` by default. Embeddings use `text-embedding-3-small`, batched 50 at a time. For custom or local LLM endpoints (`LLM_PROVIDER=custom`), Structured Outputs are not available — Phase 2 automatically falls back to JSON mode.
  - **Scoring**: ~2 API calls, ~3,500–8,000 tokens per job (RAG context + JD + cover letter output)
  - **Interview brief**: ~2 additional API calls, ~4,000–7,500 tokens — triggered once per job when advanced to interview stage
  - Full lifecycle for a job that reaches interview: ~4 API calls, ~7,500–15,500 tokens total
- Bundesagentur API currently runs with SSL verification disabled (`verify=False`). Remove when the API's cert is stable.
- All personal files (`.env`, `candidate_kb/*.md`, `config/grading_rules.md`, `config/search_targets.yaml`, `data/`, `qdrant_data/`) are excluded from git via `.gitignore`.
