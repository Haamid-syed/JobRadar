import tempfile
import unittest
from datetime import datetime
from types import MethodType
from unittest.mock import patch

from tests.support import make_config, working_directory

from intelligence.scorer import GeminiScorer
from scheduler import run_score_phase
from scrapers.base import RawJob
from storage.db import Database


PROVIDER_RESPONSE = """{
  "role_match": 3,
  "seniority_fit": 3,
  "niche_bonus": 0,
  "reason": "Controlled provider response",
  "red_flags": []
}"""


class FakeProvider:
    def __init__(self, response=PROVIDER_RESPONSE):
        self.response = response
        self.calls = 0

    def is_available(self):
        return True

    def generate(self, prompt):
        self.calls += 1
        return self.response


def exhausted_gemini(self, job, boost_signals):
    self.use_heuristic_only = True
    raise RuntimeError("simulated Gemini chain exhaustion")


def make_scorer(*, openrouter=None, local=None):
    scorer = object.__new__(GeminiScorer)
    scorer.use_heuristic_only = False
    scorer._openrouter = openrouter
    scorer._local_scorer = local
    scorer._score_single = MethodType(exhausted_gemini, scorer)
    return scorer


def make_job(index):
    return RawJob(
        title=f"Backend Developer {index}",
        company=f"Company {index}",
        source="fixture",
        apply_url=f"https://example.test/jobs/{index}",
        description="Entry-level Python and FastAPI role",
        location="Remote",
        job_type="Full-Time",
        posted_at=datetime.now(),
    )


class FallbackTests(unittest.TestCase):
    def test_openrouter_remains_available_for_subsequent_jobs(self):
        provider = FakeProvider()
        scorer = make_scorer(openrouter=provider)

        methods = [
            scorer.score_with_fallback(make_job(index), {})[1]
            for index in range(2)
        ]

        self.assertEqual(["openrouter", "openrouter"], methods)
        self.assertEqual(2, provider.calls)

    def test_mlx_remains_available_for_subsequent_jobs(self):
        provider = FakeProvider()
        scorer = make_scorer(local=provider)

        methods = [
            scorer.score_with_fallback(make_job(index), {})[1]
            for index in range(2)
        ]

        self.assertEqual(["local_mlx", "local_mlx"], methods)
        self.assertEqual(2, provider.calls)


class InterruptedScorer:
    def __init__(self, config):
        pass

    def score_from_db_records(self, records):
        # Simulate work beginning and the process dying before the score transaction.
        _ = records[0]
        raise RuntimeError("simulated scoring interruption")


class SuccessfulScorer:
    def __init__(self, config):
        pass

    def score_from_db_records(self, records):
        return [
            {
                "job_id": record["id"],
                "role_match": 3,
                "seniority_fit": 3,
                "reply_odds": 0,
                "recency": 2,
                "niche_bonus": 0,
                "total": 8,
                "verdict": "maybe",
                "reason": "Controlled recovery score",
                "red_flags": [],
                "scoring_method": "heuristic",
            }
            for record in records
        ]


class RecoveryTests(unittest.TestCase):
    def test_restart_resumes_committed_pending_jobs_without_duplicates(self):
        config = make_config()
        jobs = [make_job(index) for index in range(3)]
        signals = {job.id: {} for job in jobs}

        with tempfile.TemporaryDirectory() as temp_dir, working_directory(temp_dir):
            db = Database(config)
            db.init()
            self.assertEqual(3, db.insert_raw_jobs(jobs, signals))

            with patch("scheduler.GeminiScorer", InterruptedScorer):
                with self.assertRaisesRegex(RuntimeError, "scoring interruption"):
                    run_score_phase(config, db)

            self.assertEqual(3, len(db.get_unscored_jobs(limit=10)))
            self.assertEqual(3, len(db.get_jobs(show_hidden=True)))
            db.engine.dispose()

            restarted_db = Database(config)
            restarted_db.init()
            with patch("scheduler.GeminiScorer", SuccessfulScorer):
                result = run_score_phase(config, restarted_db)

            self.assertEqual(3, result["scored"])
            self.assertEqual([], restarted_db.get_unscored_jobs(limit=10))
            rows = restarted_db.get_jobs(show_hidden=True)
            self.assertEqual(3, len(rows))
            self.assertEqual(3, len({row["id"] for row in rows}))
            self.assertEqual(0, restarted_db.insert_raw_jobs(jobs, signals))
            self.assertEqual(3, len(restarted_db.get_jobs(show_hidden=True)))
            restarted_db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
