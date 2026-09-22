# JobRadar (V2.1) — Real-Time, Multi-Tier AI Job Aggregator & Outreach Copilot

JobRadar is a lightweight, lightning-fast, and highly resilient local job-hunting platform custom-calibrated for high-signal developer profiles. It automatically aggregates, deduplicates, enriches, and scores software listings from 12 diverse sources

---

## Key Architectural Features (V2)

### 1. Two-Phase Scrape-and-Score Pipeline
Scraping and scoring are split into separate non-blocking transactions. Raw scraped listings are verified, filtered, and saved as `unscored` immediately. If a crash or timeout occurs during the subsequent LLM scoring pass, all unscored records are retained safely and picked up automatically on the next scheduler interval.

### 2. 12 Distributed Source Scrapers
1. **HackerNews**: Public Algolia JSON API looking for direct month-based "Who is Hiring" threads.
2. **YC Jobs**: Playwright scraper capturing emerging YC cohort roles.
3. **Wellfound**: Playwright-based stealth parser collecting salary, team size, and funding details.
4. **LinkedIn**: Direct search aggregates via JobSpy (filters Easy Apply noise).
5. **Remotive**: Direct category REST API.
6. **ATS Boards**: Leveraging Lever, Greenhouse, and Ashby APIs for 25 high-niche companies (LiveKit, Supabase, Vercel, Stripe).
7. **RemoteOK**: Public REST API.
8. **Indeed**: Aggregate parser via python-jobspy.
9. **dev.to**: Developer REST API.
10. **Glassdoor**: Playwright extraction of JSON-LD schemas.
11. **RemoteHunter**: Targeted Playwright scraper.
12. **SourcingXpress**: Playwright-based job scanner.

### 3. 4-Tier / 10-Level Intelligence Fallback
JobRadar scores listings on a precise **12-point calibration scale** (Role Match, Seniority, Reply Odds, Recency, Niche Match). To prevent API quota stalls, it rolls down an automated fallback chain:
- **Tier 1 (Gemini Direct API)**: Swaps between 4 fast models (`gemini-3.1-flash-lite` → `2.5-flash-lite` → `3-flash-preview` → `2.0-flash`).
- **Tier 2 (OpenRouter Free Tier)**: Fallback to `deepseek-v4-flash:free` → `gemma-4-26b:free` → `qwen3-next-80b:free`.
- **Tier 3 (Local MLX Offline Scorer)**: Runs offline scoring locally on Apple Silicon via MLX (`Phi-4-mini` or `Qwen3.5-4B`).
- **Tier 4 (Heuristic)**: Rule-based deterministic backup score (guarantees uptime).

### 4. Circuit Breakers & Centralized Rate Limiter
- Scrapers automatically trigger a **60-minute cooling breaker** after 3 consecutive failures to safeguard network health.

### 5. Advanced Filtration & Calibration (V2.1)
* **Dynamic Tech title Filtering:** Compiles positive keywords directly from the developer profile (React, Node, WebRTC) combined with standard software terms, preventing generic non-technical listings from entering Phase 1.
* **Regex-Based Experience Cap:** Dynamically parses text matching experience patterns (e.g., `2-6 years`, `3yrs`) and strictly rejects any job requiring **2 or more years of experience**.
* **US Visa & Citizenship Screening:** Automatically filters out jobs that explicitly require US citizenship or deny visa sponsorship, while keeping global remote or India-friendly listings.
* **Seniority Exclusion:** Excludes seniority keywords (`senior`, `lead`, `sr.`, `principal`) strictly from titles using boundary-safe matching to avoid false positives in descriptions.
* **Scorer Clean Room (Self-Healing DB):** Automatically sweeps legacy unscored jobs in the DB during Phase 2, filtering non-compliant roles at zero LLM cost to conserve API quotas.

---

## Project Directory Structure
```
Job_Scraper/
├── .gitignore              # Safely excludes local venv, node_modules, active DB, logs
├── docs/                   # UI specifications & detailed plan documentation
└── jobradar/
    ├── config.yaml         # Sanitized global template config
    ├── .env                # Local secret environment credentials (git-ignored)
    ├── requirements.txt    # Python dependencies
    ├── setup.sh            # One-click environment bootstrap script
    ├── backend/
    │   ├── main.py         # FastAPI App Lifespan and Entry
    │   ├── config.py       # Dotenv parser & config data mapping
    │   ├── scheduler.py    # APScheduler orchestration & ThreadPool scrapers
    │   ├── api/routes.py   # REST APIs
    │   ├── storage/db.py   # SQLite WAL ORM models
    │   ├── scrapers/       # 12 parallel python scraper modules
    │   └── intelligence/   # Filters, rate-limiter, scorers, email outreach builders
    └── frontend/
        ├── index.html      # Google Fonts links & main entry
        └── src/            # Staggered React layout components & useJobs hooks
```

---

## Quick Start Guide

### 1. Configure Secret API Keys
For security, JobRadar isolates sensitive credentials outside of `config.yaml`.
Create a local `.env` file inside `jobradar/`:
```env
GEMINI_API_KEY="your-gemini-api-key"
OPENROUTER_API_KEY="optional-openrouter-key"
```
*Note: `.env` is automatically blocked by Git, keeping your secrets safe.*

### 2. Setup the Environment
Initialize Python venv and install dependencies:
```bash
# Run one-click setup script
cd jobradar && bash setup.sh
```

### 3. Launch Services

**Terminal 1 — Python Backend:**
```bash
cd jobradar/backend
source venv/bin/activate
python main.py
```
*Runs FastAPI at `http://localhost:8000`.*

**Terminal 2 — Vite Frontend:**
```bash
cd jobradar/frontend
npm run dev
```
*Launches the Cyberpunk Glassmorphic dashboard at `http://localhost:5173`.*

---

## Key Diagnostic Commands

- **Trigger manual refresh**: `curl -X POST http://localhost:8000/api/refresh`
- **Rescore pending unscored jobs**: `curl -X POST http://localhost:8000/api/rescore`
- **Check scraper circuit status**: `curl http://localhost:8000/api/health`
- **View pipeline run duration analytics**: `curl http://localhost:8000/api/pipeline-runs`
- **Monitor active debug files**: `tail -f logs/jobradar.log`
