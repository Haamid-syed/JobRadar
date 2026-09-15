import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from scrapers.base import RawJob
from scrapers.hn import HNHiringScraper
from scrapers.remotive import RemotiveScraper
from scrapers.yc import YCJobsScraper
from scrapers.linkedin import LinkedInScraper
from scrapers.wellfound import WellfoundScraper
from scrapers.google_search import GoogleSearchScraper
from scrapers.ats_boards import ATSBoardsScraper
from scrapers.remoteok import RemoteOKScraper
from scrapers.indeed import IndeedScraper
from scrapers.devto import DevToScraper
from scrapers.glassdoor import GlassdoorScraper
from scrapers.remotehunter import RemoteHunterScraper
from scrapers.sourcingxpress import SourcingXpressScraper
from intelligence.filters import HardFilter
from intelligence.scorer import GeminiScorer
from intelligence.enricher import enrich_descriptions

_refresh_lock = threading.Lock()
_stats_lock = threading.Lock()
_is_refreshing = False
_last_run_stats: dict = {}


def get_enabled_scrapers(config) -> list:
    scrapers = []
    if config.sources.hn_hiring.enabled:
        scrapers.append(HNHiringScraper(config))
    if config.sources.yc_jobs.enabled:
        scrapers.append(YCJobsScraper(config))
    if config.sources.remotive.enabled:
        scrapers.append(RemotiveScraper(config))
    if config.sources.linkedin.enabled:
        scrapers.append(LinkedInScraper(config))
    if config.sources.wellfound.enabled:
        scrapers.append(WellfoundScraper(config))
    if config.sources.google_search.enabled:
        scrapers.append(GoogleSearchScraper(config))
    # V2 scrapers
    if config.sources.ats_boards.enabled:
        scrapers.append(ATSBoardsScraper(config))
    if config.sources.remoteok.enabled:
        scrapers.append(RemoteOKScraper(config))
    if config.sources.indeed.enabled:
        scrapers.append(IndeedScraper(config))
    if config.sources.devto.enabled:
        scrapers.append(DevToScraper(config))
    if config.sources.glassdoor.enabled:
        scrapers.append(GlassdoorScraper(config))
    if config.sources.remotehunter.enabled:
        scrapers.append(RemoteHunterScraper(config))
    if config.sources.sourcingxpress.enabled:
        scrapers.append(SourcingXpressScraper(config))
    return scrapers


def is_refreshing() -> bool:
    with _stats_lock:
        return _is_refreshing


def get_last_run_stats() -> dict:
    with _stats_lock:
        return _last_run_stats.copy()  # Return copy to prevent mutation during read


# ─── Phase 1: Scrape + Filter + Store Raw ────────────────────────────────────


def _run_single_scraper(scraper, db):
    """Run a single scraper with circuit-breaker awareness. Thread-safe."""
    source = scraper.source_name
    health = db.get_scraper_health(source)

    # Circuit breaker check
    if health.is_circuit_broken and health.circuit_broken_until and health.circuit_broken_until > datetime.now():
        logger.warning(f"[scheduler] [{source}] Circuit broken until {health.circuit_broken_until} — skipping")
        return source, [], None, 0.0, "circuit_broken"

    scrape_start = time.time()
    jobs, error = scraper.safe_scrape()
    duration = time.time() - scrape_start

    if error:
        logger.error(f"[scheduler] [{source}] FAILED in {duration:.1f}s: {error}")
        db.update_scraper_health(source, success=False, error=error)

        # Circuit breaker: activate after 3 consecutive failures
        health_updated = db.get_scraper_health(source)
        if (health_updated.consecutive_failures or 0) >= 3:
            db.break_circuit(source, cooldown_minutes=60)

        return source, [], error, duration, "error"
    else:
        logger.info(f"[scheduler] [{source}] OK — {len(jobs)} jobs in {duration:.1f}s")
        db.update_scraper_health(source, success=True, jobs_found=len(jobs))
        return source, jobs, None, duration, "ok"


def run_scrapers(scrapers, db, max_workers: int = 4) -> tuple[list, dict]:
    """Execute scraper adapters concurrently and return jobs plus per-source stats."""
    all_raw_jobs = []
    source_results = {}
    worker_count = min(max(1, max_workers), len(scrapers)) if scrapers else 1

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(_run_single_scraper, scraper, db): scraper
            for scraper in scrapers
        }

        for future in as_completed(futures):
            scraper = futures[future]
            try:
                source, jobs, error, duration, status = future.result()
                source_results[source] = {
                    "status": status,
                    "jobs": len(jobs),
                    "duration": round(duration, 1),
                }
                if error:
                    source_results[source]["error"] = error
                else:
                    all_raw_jobs.extend(jobs)
            except Exception as e:
                source = scraper.source_name
                logger.exception(f"[scheduler] [{source}] Thread crashed: {e}")
                source_results[source] = {
                    "status": "crash",
                    "jobs": 0,
                    "error": str(e),
                }

    return all_raw_jobs, source_results


def run_scrape_phase(config, db, run_id: str = "") -> dict:
    """
    Phase 1: Scrape all sources in parallel → filter → store raw (unscored) jobs.
    Returns stats dict.
    """
    scrape_start = time.time()
    scrapers = get_enabled_scrapers(config)
    all_raw_jobs, source_results = run_scrapers(scrapers, db, max_workers=4)

    # Filter
    hard_filter = HardFilter(config, db)
    filtered_jobs, boost_signals_map = hard_filter.filter_with_signals(all_raw_jobs)
    logger.info(f"[scheduler] After hard filter: {len(filtered_jobs)} / {len(all_raw_jobs)} jobs")

    # Enrich sparse descriptions (fetch full page content for short listings)
    if filtered_jobs:
        enriched = enrich_descriptions(filtered_jobs)
        if enriched:
            logger.info(f"[scheduler] Enriched {enriched} sparse job descriptions")

    # Store raw jobs (unscored) — crash-safe persistence
    new_count = 0
    if filtered_jobs:
        new_count = db.insert_raw_jobs(filtered_jobs, boost_signals_map)

    scrape_duration = time.time() - scrape_start

    return {
        "total_raw": len(all_raw_jobs),
        "total_filtered": len(filtered_jobs),
        "total_new": new_count,
        "scrape_duration": round(scrape_duration, 1),
        "sources": source_results,
    }


# ─── Phase 2: Score Pending Jobs ──────────────────────────────────────────────


def run_score_phase(config, db) -> dict:
    """
    Phase 2: Pick up unscored jobs from DB → score → update records.
    Can be triggered independently (e.g., after a failed scoring run).
    """
    score_start = time.time()
    unscored = db.get_unscored_jobs(limit=500)  # Score all pending (was 50 — caused accumulation)

    if not unscored:
        logger.info("[scheduler] No unscored jobs to process")
        return {"scored": 0, "duration": 0}

    logger.info(f"[scheduler] Filtering & scoring {len(unscored)} pending jobs...")
    
    # Self-healing clean room: Filter out any legacy non-technical roles
    hard_filter = HardFilter(config, db)
    to_score = []
    skipped_scores = []

    for job_dict in unscored:
        # Reconstruct RawJob for validation
        raw_job = RawJob(
            title=job_dict["title"],
            company=job_dict["company"],
            location=job_dict["location"],
            job_type=job_dict["job_type"],
            description=job_dict["description"],
            apply_url=job_dict["apply_url"],
            source=job_dict["source"],
            posted_at=datetime.fromisoformat(job_dict["posted_at"]) if job_dict.get("posted_at") else None,
            company_size=job_dict.get("company_size"),
            salary_range=job_dict.get("salary_range"),
            stack_mentioned=job_dict.get("stack_mentioned", []),
            requisition_id=job_dict.get("requisition_id", ""),
        )
        should_reject, reason = hard_filter._should_reject(raw_job)
        if should_reject:
            logger.info(f"[scheduler] Secondary Filter: Instantly skipping legacy job '{raw_job.title}' @ '{raw_job.company}' — {reason}")
            skipped_scores.append({
                "job_id": job_dict["id"],
                "role_match": 0,
                "seniority_fit": 0,
                "reply_odds": 0,
                "recency": 0,
                "niche_bonus": 0,
                "total": 0,
                "verdict": "skip",
                "reason": f"Filtered during scoring: {reason}",
                "red_flags": [reason],
                "scoring_method": "heuristic",
            })
        else:
            to_score.append(job_dict)

    # Score only the remaining valid technical jobs
    scores = []
    if to_score:
        logger.info(f"[scheduler] Sending {len(to_score)} technical jobs to LLM Scorer")
        scorer = GeminiScorer(config)
        scores = scorer.score_from_db_records(to_score)
    else:
        logger.info("[scheduler] All pending jobs filtered out as non-technical!")

    # Combine LLM scores with skipped heuristic scores
    all_scores = scores + skipped_scores

    # Persist all updates to DB
    updated = db.update_job_scores(all_scores)
    db.update_setting("last_global_refresh", datetime.now().isoformat())

    score_duration = time.time() - score_start

    return {
        "scored": updated,
        "duration": round(score_duration, 1),
    }


# ─── Combined Pipeline ───────────────────────────────────────────────────────


def run_full_pipeline(config, db, run_id: Optional[str] = None):
    """
    Full pipeline: Phase 1 (scrape) + Phase 2 (score).
    
    Key improvement: if Phase 2 fails, scraped jobs are still persisted
    in DB with score_verdict='unscored' and will be picked up next run.
    """
    global _is_refreshing, _last_run_stats

    if not _refresh_lock.acquire(blocking=False):
        logger.info("[scheduler] Pipeline already running, skipping trigger")
        return

    run_id = run_id or str(uuid.uuid4())[:8]
    with _stats_lock:
        _is_refreshing = True
    pipeline_start = time.time()

    try:
        logger.info(f"=== Pipeline START (run={run_id}) ===")

        # Phase 1: Scrape + Filter + Store Raw
        scrape_stats = run_scrape_phase(config, db, run_id)

        # Phase 2: Score Pending Jobs (includes any from prior failed runs)
        score_stats = run_score_phase(config, db)

        total_duration = time.time() - pipeline_start
        logger.info(
            f"=== Pipeline END (run={run_id}) — {scrape_stats['total_new']} new jobs, "
            f"{score_stats['scored']} scored in {total_duration:.1f}s ==="
        )

        _last_run_stats = {
            "run_id": run_id,
            "ran_at": datetime.now().isoformat(),
            "total_raw": scrape_stats["total_raw"],
            "total_filtered": scrape_stats["total_filtered"],
            "total_new": scrape_stats["total_new"],
            "total_scored": score_stats["scored"],
            "duration_seconds": round(total_duration, 1),
            "scrape_duration": scrape_stats["scrape_duration"],
            "score_duration": score_stats["duration"],
            "sources": scrape_stats["sources"],
        }

        # Persist run history
        db.record_pipeline_run(_last_run_stats)

    except Exception as e:
        logger.exception(f"[scheduler] Pipeline crashed (run={run_id}): {e}")
        _last_run_stats = {
            "run_id": run_id,
            "ran_at": datetime.now().isoformat(),
            "error": str(e),
        }
        db.record_pipeline_run(_last_run_stats)
    finally:
        with _stats_lock:
            _is_refreshing = False
        _refresh_lock.release()


def setup_scheduler(config, db) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_full_pipeline,
        trigger=IntervalTrigger(hours=config.scheduler.refresh_every_hours),
        args=[config, db],
        id="main_pipeline",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info(
        f"[scheduler] Started — pipeline runs every {config.scheduler.refresh_every_hours}h"
    )
    return scheduler
