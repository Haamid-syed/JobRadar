#!/usr/bin/env python3
"""Controlled replay benchmark for JobRadar's scraper scheduler."""

import argparse
import hashlib
import json
import statistics
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from loguru import logger

from scheduler import run_scrapers
from scrapers.base import BaseScraper, RawJob


DEFAULT_FIXTURE = Path(__file__).parent / "fixtures" / "replay_sources.json"


@dataclass
class Health:
    is_circuit_broken: bool = False
    circuit_broken_until: object = None
    consecutive_failures: int = 0


class ReplayHealthStore:
    """In-memory implementation of the health methods used by the scheduler."""

    def __init__(self):
        self._health = {}
        self._lock = threading.Lock()

    def get_scraper_health(self, source):
        with self._lock:
            return self._health.setdefault(source, Health())

    def update_scraper_health(self, source, success, error=None, jobs_found=0):
        with self._lock:
            health = self._health.setdefault(source, Health())
            health.consecutive_failures = 0 if success else health.consecutive_failures + 1

    def break_circuit(self, source, cooldown_minutes=60):
        with self._lock:
            self._health.setdefault(source, Health()).is_circuit_broken = True


class ReplayScraper(BaseScraper):
    def __init__(self, fixture):
        self.source_name = fixture["name"]
        self.delay_seconds = fixture["delay_ms"] / 1000
        self.job_count = fixture["job_count"]

    def scrape(self):
        time.sleep(self.delay_seconds)
        return [
            RawJob(
                title=f"Software Engineer {index:03d}",
                company=f"Replay Company {self.source_name}",
                apply_url=f"https://fixture.test/{self.source_name}/{index}",
                source=self.source_name,
                description="Saved deterministic replay listing",
            )
            for index in range(self.job_count)
        ]


def output_digest(jobs):
    payload = [
        (job.source, job.company, job.title, job.apply_url)
        for job in jobs
    ]
    encoded = json.dumps(sorted(payload), separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def execute_once(source_fixtures, workers):
    scrapers = [ReplayScraper(fixture) for fixture in source_fixtures]
    health_store = ReplayHealthStore()
    started = time.perf_counter()
    jobs, source_results = run_scrapers(scrapers, health_store, max_workers=workers)
    duration = time.perf_counter() - started

    if any(result["status"] != "ok" for result in source_results.values()):
        raise RuntimeError(f"A replay source failed: {source_results}")

    return duration, jobs, source_results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()

    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    source_fixtures = fixture["sources"]
    expected_jobs = sum(source["job_count"] for source in source_fixtures)
    expected_sources = len(source_fixtures)

    logger.remove()
    measured = {}
    reference_digest = None

    for workers in (1, 2, 4):
        # One unmeasured warm-up per worker configuration.
        _, warmup_jobs, warmup_sources = execute_once(source_fixtures, workers)
        if len(warmup_jobs) != expected_jobs or len(warmup_sources) != expected_sources:
            raise RuntimeError("Warm-up output count did not match the saved fixture")

        durations = []
        for _ in range(args.repetitions):
            duration, jobs, source_results = execute_once(source_fixtures, workers)
            digest = output_digest(jobs)
            if len(jobs) != expected_jobs or len(source_results) != expected_sources:
                raise RuntimeError("Measured output count did not match the saved fixture")
            if reference_digest is None:
                reference_digest = digest
            elif digest != reference_digest:
                raise RuntimeError("Worker configurations produced non-equivalent outputs")
            durations.append(duration)
        measured[workers] = durations

    baseline = statistics.median(measured[1])
    print("CONTROLLED REPLAY — not live scraping performance")
    print(
        f"fixture={args.fixture.relative_to(PROJECT_ROOT)} "
        f"sources={expected_sources} listings={expected_jobs} "
        f"warmups=1 repetitions={args.repetitions}"
    )
    print("workers  median_s  listings_per_s  speedup  measured_s")
    for workers in (1, 2, 4):
        median = statistics.median(measured[workers])
        throughput = expected_jobs / median
        speedup = baseline / median
        samples = ",".join(f"{sample:.4f}" for sample in measured[workers])
        print(
            f"{workers:>7}  {median:>8.4f}  {throughput:>14.1f}  "
            f"{speedup:>7.2f}x  {samples}"
        )
    print(f"equivalent_outputs=yes sha256={reference_digest}")


if __name__ == "__main__":
    main()
