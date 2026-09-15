import tempfile
import unittest
from datetime import datetime

from tests.support import make_config, working_directory

from intelligence.filters import HardFilter
from scrapers.base import RawJob
from storage.db import Database


def make_job(
    *,
    title,
    company,
    source,
    description="Python backend role",
    location="Remote",
    apply_url=None,
    requisition_id="",
):
    return RawJob(
        title=title,
        company=company,
        source=source,
        apply_url=apply_url
        or f"https://example.test/{source}/{title.replace(' ', '-').lower()}",
        description=description,
        location=location,
        job_type="Full-Time",
        posted_at=datetime.now(),
        requisition_id=requisition_id,
    )


class DeduplicationTests(unittest.TestCase):
    def test_duplicate_within_batch_keeps_richer_cross_source_record(self):
        config = make_config()
        short = make_job(
            title="Backend Engineer",
            company="Acme, Inc.",
            source="linkedin",
            description="Python role",
            location="Bengaluru, India",
        )
        rich = make_job(
            title="backend engineer",
            company="ACME INC",
            source="hn",
            description="Python backend role with FastAPI, ownership, and detailed requirements.",
            location="Bengaluru, India",
        )

        filtered, _ = HardFilter(config).filter_with_signals([short, rich])

        self.assertEqual(1, len(filtered))
        self.assertEqual("hn", filtered[0].source)
        self.assertEqual(rich.description, filtered[0].description)

    def test_duplicate_across_runs_and_sources_is_rejected(self):
        config = make_config()
        first = make_job(
            title="Backend Engineer",
            company="Acme, Inc.",
            source="linkedin",
            location="Bengaluru, India",
        )
        later = make_job(
            title="backend engineer",
            company="ACME INC",
            source="hn",
            description="A later and richer copy of the same opening.",
            location="Bengaluru, India",
        )

        with tempfile.TemporaryDirectory() as temp_dir, working_directory(temp_dir):
            db = Database(config)
            db.init()
            self.assertEqual(1, db.insert_raw_jobs([first], {first.id: {}}))

            filtered, _ = HardFilter(config, db).filter_with_signals([later])

            self.assertEqual([], filtered)
            self.assertEqual(1, len(db.get_jobs(show_hidden=True)))
            db.engine.dispose()

    def test_similar_but_distinct_openings_are_preserved(self):
        config = make_config()
        jobs = [
            make_job(title="Software Engineer I", company="Acme Labs", source="linkedin"),
            make_job(title="Software Engineer II", company="Acme Labs", source="linkedin"),
            make_job(title="Software Engineer I", company="Acme AI", source="hn"),
        ]

        filtered, _ = HardFilter(config).filter_with_signals(jobs)

        self.assertEqual(3, len(filtered))
        self.assertEqual(3, len({job.canonical_id for job in filtered}))

    def test_same_title_with_different_requisition_ids_is_preserved(self):
        config = make_config()
        jobs = [
            make_job(
                title="Backend Engineer",
                company="Acme",
                source="ats_boards",
                location="Bengaluru, India",
                requisition_id="greenhouse:REQ-100",
            ),
            make_job(
                title="Backend Engineer",
                company="Acme",
                source="linkedin",
                location="Bengaluru, India",
                requisition_id="greenhouse:REQ-200",
            ),
        ]

        filtered, _ = HardFilter(config).filter_with_signals(jobs)

        self.assertEqual(2, len(filtered))
        self.assertEqual(2, len({job.id for job in filtered}))

    def test_different_requisition_id_survives_a_later_run(self):
        config = make_config()
        first = make_job(
            title="Backend Engineer",
            company="Acme",
            source="ats_boards",
            location="Bengaluru, India",
            requisition_id="greenhouse:REQ-100",
        )
        later = make_job(
            title="Backend Engineer",
            company="Acme",
            source="linkedin",
            location="Bengaluru, India",
            requisition_id="greenhouse:REQ-200",
        )

        with tempfile.TemporaryDirectory() as temp_dir, working_directory(temp_dir):
            db = Database(config)
            db.init()
            self.assertEqual(1, db.insert_raw_jobs([first], {first.id: {}}))

            filtered, _ = HardFilter(config, db).filter_with_signals([later])

            self.assertEqual([later], filtered)
            db.engine.dispose()

    def test_same_title_with_different_explicit_locations_is_preserved(self):
        config = make_config()
        jobs = [
            make_job(
                title="Backend Engineer",
                company="Acme",
                source="linkedin",
                location="Bengaluru, India",
            ),
            make_job(
                title="Backend Engineer",
                company="Acme",
                source="indeed",
                location="Mumbai, India",
            ),
        ]

        filtered, _ = HardFilter(config).filter_with_signals(jobs)

        self.assertEqual(2, len(filtered))
        self.assertEqual(2, len({job.id for job in filtered}))

    def test_different_explicit_location_survives_a_later_run(self):
        config = make_config()
        first = make_job(
            title="Backend Engineer",
            company="Acme",
            source="linkedin",
            location="Bengaluru, India",
        )
        later = make_job(
            title="Backend Engineer",
            company="Acme",
            source="indeed",
            location="Mumbai, India",
        )

        with tempfile.TemporaryDirectory() as temp_dir, working_directory(temp_dir):
            db = Database(config)
            db.init()
            self.assertEqual(1, db.insert_raw_jobs([first], {first.id: {}}))

            filtered, _ = HardFilter(config, db).filter_with_signals([later])

            self.assertEqual([later], filtered)
            db.engine.dispose()

    def test_matching_requisition_and_location_dedupes_across_sources(self):
        config = make_config()
        short = make_job(
            title="Backend Engineer",
            company="Acme",
            source="ats_boards",
            description="Python role",
            location="Bengaluru, India",
            requisition_id="greenhouse:REQ-100",
        )
        rich = make_job(
            title="backend engineer",
            company="ACME",
            source="linkedin",
            description="Detailed Python and FastAPI role",
            location="Bengaluru, India",
            requisition_id="greenhouse:req-100",
        )

        filtered, _ = HardFilter(config).filter_with_signals([short, rich])

        self.assertEqual([rich], filtered)

    def test_ambiguous_remote_records_with_different_urls_are_preserved(self):
        config = make_config()
        jobs = [
            make_job(
                title="Backend Engineer",
                company="Acme",
                source="linkedin",
                location="Remote",
                apply_url="https://linkedin.example/jobs/111",
            ),
            make_job(
                title="Backend Engineer",
                company="Acme",
                source="indeed",
                location="Remote",
                apply_url="https://indeed.example/jobs/222",
            ),
        ]

        filtered, _ = HardFilter(config).filter_with_signals(jobs)

        self.assertEqual(2, len(filtered))

    def test_shared_apply_url_dedupes_when_location_is_generic(self):
        config = make_config()
        short = make_job(
            title="Backend Engineer",
            company="Acme",
            source="linkedin",
            description="Python role",
            location="Remote",
            apply_url="https://jobs.acme.test/apply/req-100?utm_source=linkedin",
        )
        rich = make_job(
            title="backend engineer",
            company="ACME",
            source="indeed",
            description="Detailed Python and FastAPI role",
            location="Worldwide",
            apply_url="https://jobs.acme.test/apply/req-100?utm_source=indeed",
        )

        filtered, _ = HardFilter(config).filter_with_signals([short, rich])

        self.assertEqual([rich], filtered)


if __name__ == "__main__":
    unittest.main()
