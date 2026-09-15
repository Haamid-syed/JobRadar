import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from loguru import logger

from scrapers.base import JobIdentity, RawJob, canonical_job_id


# ─── SQLAlchemy Models ────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


class JobModel(Base):
    __tablename__ = "jobs"

    id = Column(Text, primary_key=True)
    title = Column(Text, nullable=False)
    company = Column(Text, nullable=False)
    location = Column(Text)
    job_type = Column(Text)
    description = Column(Text)
    apply_url = Column(Text, nullable=False)
    source = Column(Text, nullable=False)
    posted_at = Column(DateTime)
    scraped_at = Column(DateTime, nullable=False, default=datetime.now)
    salary_range = Column(Text)
    company_size = Column(Text)
    stack_mentioned = Column(Text)  # JSON array
    boost_signals = Column(Text)  # JSON dict — persisted filter signals for scoring
    requisition_id = Column(Text)

    score_role_match = Column(Integer, default=0)
    score_seniority = Column(Integer, default=0)
    score_reply_odds = Column(Integer, default=0)
    score_recency = Column(Integer, default=0)
    score_niche_bonus = Column(Integer, default=0)
    score_total = Column(Integer, default=0)
    score_verdict = Column(Text, default="unscored")
    score_reason = Column(Text)
    score_red_flags = Column(Text)  # JSON array
    scored_at = Column(DateTime)
    scoring_method = Column(Text, default="llm")

    status = Column(Text, default="new")
    status_updated_at = Column(DateTime)
    notes = Column(Text)
    draft_email = Column(Text)  # JSON {"subject": ..., "body": ...}


class ScraperHealthModel(Base):
    __tablename__ = "scraper_health"

    source = Column(Text, primary_key=True)
    last_run_at = Column(DateTime)
    last_success_at = Column(DateTime)
    consecutive_failures = Column(Integer, default=0)
    last_error = Column(Text)
    is_circuit_broken = Column(Boolean, default=False)
    circuit_broken_until = Column(DateTime)
    jobs_found_last_run = Column(Integer, default=0)


class SettingsModel(Base):
    __tablename__ = "settings"

    key = Column(Text, primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime)


class PipelineRunModel(Base):
    __tablename__ = "pipeline_runs"

    run_id = Column(Text, primary_key=True)
    started_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime)
    total_raw = Column(Integer, default=0)
    total_filtered = Column(Integer, default=0)
    total_new = Column(Integer, default=0)
    total_scored = Column(Integer, default=0)
    duration_seconds = Column(Float, default=0.0)
    scrape_duration = Column(Float, default=0.0)
    score_duration = Column(Float, default=0.0)
    source_breakdown = Column(Text)  # JSON dict
    error = Column(Text)


# ─── WAL Mode Event ───────────────────────────────────────────────────────────


def _set_wal_mode(dbapi_conn, connection_record):
    """Enable WAL mode on every new SQLite connection."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


# ─── Database Class ───────────────────────────────────────────────────────────


class Database:
    def __init__(self, config):
        db_path = "jobradar.db"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        event.listen(self.engine, "connect", _set_wal_mode)
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)

    def init(self):
        """Create all tables if they don't exist."""
        Base.metadata.create_all(self.engine)
        
        # Self-healing schema migration for backward compatibility
        try:
            with self._session() as session:
                conn = session.connection()
                
                # 1. Migrate "jobs" table
                result_jobs = conn.exec_driver_sql("PRAGMA table_info(jobs)")
                existing_jobs_cols = {row[1] for row in result_jobs.all()}
                
                if "boost_signals" not in existing_jobs_cols:
                    logger.info("[db] Auto-Migration: Adding missing column 'boost_signals' to 'jobs' table")
                    conn.exec_driver_sql("ALTER TABLE jobs ADD COLUMN boost_signals TEXT")
                
                if "scoring_method" not in existing_jobs_cols:
                    logger.info("[db] Auto-Migration: Adding missing column 'scoring_method' to 'jobs' table")
                    conn.exec_driver_sql("ALTER TABLE jobs ADD COLUMN scoring_method TEXT DEFAULT 'llm'")
                
                if "company_size" not in existing_jobs_cols:
                    logger.info("[db] Auto-Migration: Adding missing column 'company_size' to 'jobs' table")
                    conn.exec_driver_sql("ALTER TABLE jobs ADD COLUMN company_size TEXT")
                
                if "salary_range" not in existing_jobs_cols:
                    logger.info("[db] Auto-Migration: Adding missing column 'salary_range' to 'jobs' table")
                    conn.exec_driver_sql("ALTER TABLE jobs ADD COLUMN salary_range TEXT")

                if "requisition_id" not in existing_jobs_cols:
                    logger.info("[db] Auto-Migration: Adding missing column 'requisition_id' to 'jobs' table")
                    conn.exec_driver_sql("ALTER TABLE jobs ADD COLUMN requisition_id TEXT")

                # 2. Migrate "scraper_health" table
                result_health = conn.exec_driver_sql("PRAGMA table_info(scraper_health)")
                existing_health_cols = {row[1] for row in result_health.all()}
                
                if "is_circuit_broken" not in existing_health_cols:
                    logger.info("[db] Auto-Migration: Adding missing column 'is_circuit_broken' to 'scraper_health' table")
                    conn.exec_driver_sql("ALTER TABLE scraper_health ADD COLUMN is_circuit_broken BOOLEAN DEFAULT 0")
                
                if "circuit_broken_until" not in existing_health_cols:
                    logger.info("[db] Auto-Migration: Adding missing column 'circuit_broken_until' to 'scraper_health' table")
                    conn.exec_driver_sql("ALTER TABLE scraper_health ADD COLUMN circuit_broken_until DATETIME")
                
                if "jobs_found_last_run" not in existing_health_cols:
                    logger.info("[db] Auto-Migration: Adding missing column 'jobs_found_last_run' to 'scraper_health' table")
                    conn.exec_driver_sql("ALTER TABLE scraper_health ADD COLUMN jobs_found_last_run INTEGER DEFAULT 0")
                
                session.commit()
                logger.info("[db] Auto-migration check completed successfully")
        except Exception as e:
            logger.warning(f"[db] Auto-migration check encountered an issue: {e}")

        logger.info("[db] Tables initialized (WAL mode enabled)")

    def _session(self) -> Session:
        return self.SessionLocal()

    # ─── Jobs ─────────────────────────────────────────────────────────────────

    def insert_raw_jobs(
        self, jobs: List[RawJob], boost_signals_map: dict
    ) -> int:
        """
        Phase 1 of two-phase pipeline: store filtered jobs WITHOUT scores.
        Jobs are stored with score_verdict='unscored' and their boost_signals
        persisted for later scoring. Returns the number of new jobs inserted.
        """
        now = datetime.now()
        new_count = 0

        with self._session() as session:
            try:
                for job in jobs:
                    existing = session.get(JobModel, job.id)
                    if existing:
                        # Job already exists (from prior scrape) — skip
                        continue

                    job_data = job.to_dict()
                    signals = boost_signals_map.get(job.id, {})
                    new_job = JobModel(
                        id=job.id,
                        title=job_data["title"],
                        company=job_data["company"],
                        location=job_data["location"],
                        job_type=job_data["job_type"],
                        description=job_data["description"],
                        apply_url=job_data["apply_url"],
                        source=job_data["source"],
                        posted_at=job.posted_at,
                        scraped_at=now,
                        salary_range=job_data["salary_range"],
                        company_size=job_data["company_size"],
                        stack_mentioned=job_data["stack_mentioned"],
                        boost_signals=json.dumps(signals),
                        requisition_id=job_data["requisition_id"],
                        score_verdict="unscored",
                        status="new",
                    )
                    session.add(new_job)
                    new_count += 1

                session.commit()
                logger.info(f"[db] Stored {new_count} raw jobs (unscored)")
                return new_count
            except Exception as e:
                session.rollback()
                logger.exception(f"[db] insert_raw_jobs failed: {e}")
                raise

    def get_unscored_jobs(self, limit: int = 50) -> List[dict]:
        """
        Phase 2 of two-phase pipeline: retrieve jobs that need scoring.
        Returns dicts with boost_signals parsed back from JSON.
        """
        with self._session() as session:
            rows = (
                session.query(JobModel)
                .filter(JobModel.score_verdict == "unscored")
                .order_by(JobModel.scraped_at.desc())
                .limit(limit)
                .all()
            )
            results = []
            for job in rows:
                d = self._job_to_dict(job)
                # Parse stored boost signals back
                try:
                    d["_boost_signals"] = json.loads(job.boost_signals or "{}")
                except Exception:
                    d["_boost_signals"] = {}
                results.append(d)
            return results

    def update_job_scores(self, scores: List[dict]) -> int:
        """
        Phase 2: apply scores to previously stored raw jobs.
        Returns count of jobs updated.
        """
        now = datetime.now()
        updated = 0

        with self._session() as session:
            try:
                for score in scores:
                    job = session.get(JobModel, score["job_id"])
                    if not job:
                        continue
                    job.score_role_match = score.get("role_match", 0)
                    job.score_seniority = score.get("seniority_fit", 0)
                    job.score_reply_odds = score.get("reply_odds", 0)
                    job.score_recency = score.get("recency", 0)
                    job.score_niche_bonus = score.get("niche_bonus", 0)
                    job.score_total = score.get("total", 0)
                    job.score_verdict = score.get("verdict", "unscored")
                    job.score_reason = score.get("reason", "")
                    job.score_red_flags = json.dumps(score.get("red_flags", []))
                    job.scored_at = now
                    job.scoring_method = score.get("scoring_method", "llm")
                    updated += 1

                session.commit()
                logger.info(f"[db] Scored {updated} jobs")
                return updated
            except Exception as e:
                session.rollback()
                logger.exception(f"[db] update_job_scores failed: {e}")
                raise

    def upsert_jobs(self, jobs: List[RawJob], scores: List[dict]) -> int:
        """
        Legacy single-pass upsert (kept for backward compatibility).
        Insert or update jobs with their scores in a single transaction.
        Returns the number of new jobs inserted.
        """
        scores_by_id = {s["job_id"]: s for s in scores}
        now = datetime.now()
        new_count = 0

        with self._session() as session:
            try:
                for job in jobs:
                    score = scores_by_id.get(job.id, {})
                    existing = session.get(JobModel, job.id)

                    if existing:
                        # Only update score fields if job exists — preserve status/notes
                        existing.score_role_match = score.get("role_match", 0)
                        existing.score_seniority = score.get("seniority_fit", 0)
                        existing.score_reply_odds = score.get("reply_odds", 0)
                        existing.score_recency = score.get("recency", 0)
                        existing.score_niche_bonus = score.get("niche_bonus", 0)
                        existing.score_total = score.get("total", 0)
                        existing.score_verdict = score.get("verdict", "unscored")
                        existing.score_reason = score.get("reason", "")
                        existing.score_red_flags = json.dumps(score.get("red_flags", []))
                        existing.scored_at = now
                        existing.scoring_method = score.get("scoring_method", "llm")
                    else:
                        job_data = job.to_dict()
                        new_job = JobModel(
                            id=job.id,
                            title=job_data["title"],
                            company=job_data["company"],
                            location=job_data["location"],
                            job_type=job_data["job_type"],
                            description=job_data["description"],
                            apply_url=job_data["apply_url"],
                            source=job_data["source"],
                            posted_at=job.posted_at,
                            scraped_at=now,
                            salary_range=job_data["salary_range"],
                            company_size=job_data["company_size"],
                            stack_mentioned=job_data["stack_mentioned"],
                            requisition_id=job_data["requisition_id"],
                            score_role_match=score.get("role_match", 0),
                            score_seniority=score.get("seniority_fit", 0),
                            score_reply_odds=score.get("reply_odds", 0),
                            score_recency=score.get("recency", 0),
                            score_niche_bonus=score.get("niche_bonus", 0),
                            score_total=score.get("total", 0),
                            score_verdict=score.get("verdict", "unscored"),
                            score_reason=score.get("reason", ""),
                            score_red_flags=json.dumps(score.get("red_flags", [])),
                            scored_at=now,
                            scoring_method=score.get("scoring_method", "llm"),
                            status="new",
                        )
                        session.add(new_job)
                        new_count += 1

                session.commit()
                logger.info(f"[db] Upserted {len(jobs)} jobs ({new_count} new)")
                return new_count
            except Exception as e:
                session.rollback()
                logger.exception(f"[db] upsert_jobs failed: {e}")
                raise

    def get_jobs(
        self,
        verdict: Optional[str] = None,
        source: Optional[str] = None,
        min_score: Optional[int] = None,
        status: Optional[str] = None,
        show_hidden: bool = False,
        q: Optional[str] = None,
    ) -> List[dict]:
        """Fetch jobs with optional filters."""
        with self._session() as session:
            query = session.query(JobModel)

            if verdict and verdict != "all":
                query = query.filter(JobModel.score_verdict == verdict)

            if source and source != "all":
                query = query.filter(JobModel.source == source)

            if min_score is not None:
                query = query.filter(JobModel.score_total >= min_score)

            if status and status != "all":
                query = query.filter(JobModel.status == status)

            if not show_hidden:
                query = query.filter(JobModel.status != "skipped")

            if q:
                search_term = f"%{q}%"
                query = query.filter(
                    JobModel.title.ilike(search_term)
                    | JobModel.company.ilike(search_term)
                    | JobModel.description.ilike(search_term)
                )

            query = query.order_by(JobModel.score_total.desc(), JobModel.scraped_at.desc())
            jobs = query.all()
            return [self._job_to_dict(j) for j in jobs]

    def get_job_by_id(self, job_id: str) -> Optional[dict]:
        """Get a single job by ID."""
        with self._session() as session:
            job = session.get(JobModel, job_id)
            return self._job_to_dict(job) if job else None

    def update_job_status(
        self, job_id: str, status: str, notes: Optional[str] = None
    ) -> bool:
        """Update job status and optionally notes."""
        with self._session() as session:
            try:
                job = session.get(JobModel, job_id)
                if not job:
                    return False
                job.status = status
                job.status_updated_at = datetime.now()
                if notes is not None:
                    job.notes = notes
                session.commit()
                return True
            except Exception as e:
                session.rollback()
                logger.exception(f"[db] update_job_status failed: {e}")
                raise

    def update_job_draft(self, job_id: str, draft: dict) -> bool:
        """Cache the generated email draft on the job record."""
        with self._session() as session:
            try:
                job = session.get(JobModel, job_id)
                if not job:
                    return False
                job.draft_email = json.dumps(draft)
                session.commit()
                return True
            except Exception as e:
                session.rollback()
                logger.exception(f"[db] update_job_draft failed: {e}")
                raise

    def get_stats(self) -> dict:
        """Compute aggregate stats for the dashboard."""
        with self._session() as session:
            from sqlalchemy import func

            total = session.query(func.count(JobModel.id)).scalar() or 0

            today_cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            new_today = (
                session.query(func.count(JobModel.id))
                .filter(JobModel.scraped_at >= today_cutoff)
                .scalar()
                or 0
            )

            applied = (
                session.query(func.count(JobModel.id))
                .filter(JobModel.status == "applied")
                .scalar()
                or 0
            )

            by_source_rows = session.query(
                JobModel.source, func.count(JobModel.id)
            ).group_by(JobModel.source).all()
            by_source = {row[0]: row[1] for row in by_source_rows}

            by_verdict_rows = session.query(
                JobModel.score_verdict, func.count(JobModel.id)
            ).group_by(JobModel.score_verdict).all()
            by_verdict = {row[0]: row[1] for row in by_verdict_rows}

            niche_matches = (
                session.query(func.count(JobModel.id))
                .filter(JobModel.score_niche_bonus >= 1)
                .scalar()
                or 0
            )

            return {
                "total_jobs": total,
                "new_today": new_today,
                "applied": applied,
                "by_source": by_source,
                "by_verdict": by_verdict,
                "niche_matches": niche_matches,
            }

    def get_existing_job_ids(self, job_ids: List[str]) -> dict:
        """
        Batch-check which job IDs already exist in the DB.
        Returns {id: status} for all existing jobs.
        Much more efficient than calling get_job_by_id N times.
        """
        if not job_ids:
            return {}
        with self._session() as session:
            rows = (
                session.query(JobModel.id, JobModel.status)
                .filter(JobModel.id.in_(job_ids))
                .all()
            )
            return {row[0]: row[1] for row in rows}

    def get_existing_job_identities(
        self, canonical_ids: List[str]
    ) -> Dict[str, List[JobIdentity]]:
        """Return stored identity evidence for requested company/title groups."""
        if not canonical_ids:
            return {}

        wanted = set(canonical_ids)
        with self._session() as session:
            rows = session.query(
                JobModel.company,
                JobModel.title,
                JobModel.apply_url,
                JobModel.location,
                JobModel.requisition_id,
            ).all()
            existing: Dict[str, List[JobIdentity]] = {}
            for company, title, apply_url, location, requisition_id in rows:
                canonical_id = canonical_job_id(company, title)
                if canonical_id in wanted:
                    existing.setdefault(canonical_id, []).append(
                        JobIdentity(
                            company=company,
                            title=title,
                            apply_url=apply_url or "",
                            location=location or "",
                            requisition_id=requisition_id or "",
                        )
                    )
            return existing

    # ─── Scraper Health ───────────────────────────────────────────────────────

    def get_scraper_health(self, source: str) -> ScraperHealthModel:
        """Get or create scraper health record."""
        with self._session() as session:
            health = session.get(ScraperHealthModel, source)
            if not health:
                health = ScraperHealthModel(source=source)
                session.add(health)
                session.commit()
                session.refresh(health)
            # Detach from session to avoid issues
            session.expunge(health)
            return health

    def get_all_scraper_health(self) -> List[dict]:
        """Get health for all sources."""
        with self._session() as session:
            records = session.query(ScraperHealthModel).all()
            return [
                {
                    "source": r.source,
                    "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
                    "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
                    "consecutive_failures": r.consecutive_failures,
                    "last_error": r.last_error,
                    "is_circuit_broken": r.is_circuit_broken,
                    "circuit_broken_until": r.circuit_broken_until.isoformat() if r.circuit_broken_until else None,
                    "jobs_found_last_run": r.jobs_found_last_run,
                }
                for r in records
            ]

    def update_scraper_health(
        self,
        source: str,
        success: bool,
        error: Optional[str] = None,
        jobs_found: int = 0,
    ):
        """Update scraper health after a run."""
        with self._session() as session:
            try:
                health = session.get(ScraperHealthModel, source)
                if not health:
                    health = ScraperHealthModel(source=source)
                    session.add(health)

                now = datetime.now()
                health.last_run_at = now

                if success:
                    health.last_success_at = now
                    health.consecutive_failures = 0
                    health.last_error = None
                    health.jobs_found_last_run = jobs_found
                    # Reset circuit breaker on success
                    if health.is_circuit_broken:
                        health.is_circuit_broken = False
                        health.circuit_broken_until = None
                        logger.info(f"[db] Circuit breaker RESET for {source}")
                else:
                    health.consecutive_failures = (health.consecutive_failures or 0) + 1
                    health.last_error = error

                session.commit()
            except Exception as e:
                session.rollback()
                logger.exception(f"[db] update_scraper_health failed: {e}")
                raise

    def break_circuit(self, source: str, cooldown_minutes: int = 60):
        """Activate circuit breaker for a source."""
        with self._session() as session:
            try:
                health = session.get(ScraperHealthModel, source)
                if not health:
                    health = ScraperHealthModel(source=source)
                    session.add(health)

                health.is_circuit_broken = True
                health.circuit_broken_until = datetime.now() + timedelta(minutes=cooldown_minutes)
                session.commit()
                logger.warning(f"[db] Circuit breaker ACTIVATED for {source} (cooldown: {cooldown_minutes}min)")
            except Exception as e:
                session.rollback()
                logger.exception(f"[db] break_circuit failed: {e}")
                raise

    # ─── Settings ─────────────────────────────────────────────────────────────

    def get_setting(self, key: str) -> Optional[str]:
        with self._session() as session:
            setting = session.get(SettingsModel, key)
            return setting.value if setting else None

    def update_setting(self, key: str, value: str):
        with self._session() as session:
            try:
                setting = session.get(SettingsModel, key)
                if setting:
                    setting.value = value
                    setting.updated_at = datetime.now()
                else:
                    session.add(SettingsModel(key=key, value=value, updated_at=datetime.now()))
                session.commit()
            except Exception as e:
                session.rollback()
                logger.exception(f"[db] update_setting failed: {e}")
                raise

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _job_to_dict(self, job: JobModel) -> dict:
        """Convert ORM model to plain dict."""
        stack = []
        try:
            stack = json.loads(job.stack_mentioned or "[]")
        except Exception:
            pass

        red_flags = []
        try:
            red_flags = json.loads(job.score_red_flags or "[]")
        except Exception:
            pass

        draft_email = None
        if job.draft_email:
            try:
                draft_email = json.loads(job.draft_email)
            except Exception:
                draft_email = None

        boost_signals = {}
        if job.boost_signals:
            try:
                boost_signals = json.loads(job.boost_signals)
            except Exception:
                pass

        return {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "job_type": job.job_type,
            "description": job.description,
            "apply_url": job.apply_url,
            "source": job.source,
            "posted_at": job.posted_at.isoformat() if job.posted_at else None,
            "scraped_at": job.scraped_at.isoformat() if job.scraped_at else None,
            "salary_range": job.salary_range,
            "company_size": job.company_size,
            "requisition_id": job.requisition_id or "",
            "stack_mentioned": stack,
            "boost_signals": boost_signals,
            "score_role_match": job.score_role_match or 0,
            "score_seniority": job.score_seniority or 0,
            "score_reply_odds": job.score_reply_odds or 0,
            "score_recency": job.score_recency or 0,
            "score_niche_bonus": job.score_niche_bonus or 0,
            "score_total": job.score_total or 0,
            "score_verdict": job.score_verdict or "unscored",
            "score_reason": job.score_reason or "",
            "score_red_flags": red_flags,
            "scored_at": job.scored_at.isoformat() if job.scored_at else None,
            "scoring_method": job.scoring_method or "llm",
            "status": job.status or "new",
            "status_updated_at": job.status_updated_at.isoformat() if job.status_updated_at else None,
            "notes": job.notes or "",
            "draft_email": draft_email,
        }

    # ─── Pipeline Run History ─────────────────────────────────────────────────

    def record_pipeline_run(self, stats: dict):
        """Record a completed pipeline run for historical tracking."""
        with self._session() as session:
            try:
                run = PipelineRunModel(
                    run_id=stats.get("run_id", "unknown"),
                    started_at=datetime.fromisoformat(stats["ran_at"]) if stats.get("ran_at") else datetime.now(),
                    completed_at=datetime.now(),
                    total_raw=stats.get("total_raw", 0),
                    total_filtered=stats.get("total_filtered", 0),
                    total_new=stats.get("total_new", 0),
                    total_scored=stats.get("total_scored", 0),
                    duration_seconds=stats.get("duration_seconds", 0),
                    scrape_duration=stats.get("scrape_duration", 0),
                    score_duration=stats.get("score_duration", 0),
                    source_breakdown=json.dumps(stats.get("sources", {})),
                    error=stats.get("error"),
                )
                session.add(run)
                session.commit()
                logger.debug(f"[db] Recorded pipeline run: {run.run_id}")
            except Exception as e:
                session.rollback()
                logger.warning(f"[db] Failed to record pipeline run: {e}")

    def get_pipeline_runs(self, limit: int = 10) -> List[dict]:
        """Retrieve recent pipeline run history."""
        with self._session() as session:
            rows = (
                session.query(PipelineRunModel)
                .order_by(PipelineRunModel.started_at.desc())
                .limit(limit)
                .all()
            )
            results = []
            for run in rows:
                sources = {}
                try:
                    sources = json.loads(run.source_breakdown or "{}")
                except Exception:
                    pass

                results.append({
                    "run_id": run.run_id,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                    "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                    "total_raw": run.total_raw or 0,
                    "total_filtered": run.total_filtered or 0,
                    "total_new": run.total_new or 0,
                    "total_scored": run.total_scored or 0,
                    "duration_seconds": run.duration_seconds or 0,
                    "scrape_duration": run.scrape_duration or 0,
                    "score_duration": run.score_duration or 0,
                    "sources": sources,
                    "error": run.error,
                })
            return results
