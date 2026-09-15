"""
ATS Board Scraper — directly queries Lever, Greenhouse, and Ashby job board APIs.

Replaces the old Google Search dorking approach. These are official, public, free
APIs with structured JSON responses — no rate limiting, no scraping, no LLM extraction.
"""

import time
import re
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlparse

import requests
from loguru import logger

from .base import BaseScraper, RawJob

TECH_KEYWORDS = [
    "react", "node.js", "typescript", "python", "fastapi", "postgresql",
    "docker", "aws", "kubernetes", "redis", "graphql", "rust", "go",
    "webrtc", "websocket", "socket.io", "livekit", "next.js", "vue",
    "angular", "java", "express", "django",
]

HEADERS = {
    "User-Agent": "JobRadar/2.0",
    "Accept": "application/json",
}


class ATSBoardsScraper(BaseScraper):
    """
    Scrapes job listings directly from ATS board APIs.
    Supports: Lever, Greenhouse, and Ashby.
    """

    source_name = "ats_boards"

    def __init__(self, config):
        self.config = config
        ats_cfg = getattr(config.sources, "ats_boards", None)
        self.companies = getattr(ats_cfg, "companies", []) if ats_cfg else []

    def _scrape_lever(self, company_slug: str) -> List[RawJob]:
        """Lever API: https://api.lever.co/v0/postings/{company}?mode=json"""
        jobs = []
        try:
            resp = requests.get(
                f"https://api.lever.co/v0/postings/{company_slug}",
                params={"mode": "json"},
                headers=HEADERS,
                timeout=10,
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            listings = resp.json()

            for entry in listings:
                text = entry.get("descriptionPlain", "") or entry.get("description", "")
                desc_lower = text.lower()
                
                # Extract location
                categories = entry.get("categories", {})
                location = categories.get("location", "Remote") or "Remote"
                
                # Stack detection
                stack = [kw for kw in TECH_KEYWORDS if kw in desc_lower]
                
                # Job type
                commitment = categories.get("commitment", "").lower()
                if "intern" in commitment or "intern" in entry.get("text", "").lower():
                    job_type = "internship"
                else:
                    job_type = "full-time"

                # Parse date (Lever uses millisecond timestamps)
                posted_at = None
                created_at = entry.get("createdAt")
                if created_at:
                    try:
                        posted_at = datetime.fromtimestamp(created_at / 1000)
                    except Exception:
                        pass

                apply_url = entry.get("applyUrl") or entry.get("hostedUrl", "")
                
                jobs.append(RawJob(
                    title=entry.get("text", "Software Engineer")[:200],
                    company=company_slug.replace("-", " ").title()[:100],
                    apply_url=apply_url,
                    source=self.source_name,
                    description=text[:3000],
                    location=location,
                    job_type=job_type,
                    posted_at=posted_at,
                    stack_mentioned=stack,
                    requisition_id=(
                        f"lever:{entry['id']}" if entry.get("id") else ""
                    ),
                ))

        except Exception as e:
            logger.debug(f"[ats_boards] Lever '{company_slug}' failed: {e}")
        
        return jobs

    def _scrape_greenhouse(self, company_slug: str) -> List[RawJob]:
        """Greenhouse API: https://boards-api.greenhouse.io/v1/boards/{company}/jobs"""
        jobs = []
        try:
            resp = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs",
                params={"content": "true"},
                headers=HEADERS,
                timeout=10,
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()
            listings = data.get("jobs", [])

            for entry in listings:
                content = entry.get("content", "")
                # Greenhouse content is HTML — strip tags for text
                text = re.sub(r"<[^>]+>", " ", content)
                text = re.sub(r"\s+", " ", text).strip()
                desc_lower = text.lower()

                # Location
                location_data = entry.get("location", {})
                location = location_data.get("name", "Remote") if location_data else "Remote"

                stack = [kw for kw in TECH_KEYWORDS if kw in desc_lower]

                title = entry.get("title", "Software Engineer")
                job_type = "internship" if "intern" in title.lower() else "full-time"

                # Parse date
                posted_at = None
                updated = entry.get("updated_at", "")
                if updated:
                    try:
                        posted_at = datetime.fromisoformat(
                            updated.replace("Z", "+00:00")
                        ).replace(tzinfo=None)
                    except Exception:
                        pass

                abs_url = entry.get("absolute_url", "")

                jobs.append(RawJob(
                    title=title[:200],
                    company=company_slug.replace("-", " ").title()[:100],
                    apply_url=abs_url,
                    source=self.source_name,
                    description=text[:3000],
                    location=location,
                    job_type=job_type,
                    posted_at=posted_at,
                    stack_mentioned=stack,
                    requisition_id=(
                        f"greenhouse:{entry['id']}" if entry.get("id") else ""
                    ),
                ))

        except Exception as e:
            logger.debug(f"[ats_boards] Greenhouse '{company_slug}' failed: {e}")

        return jobs

    def _scrape_ashby(self, company_slug: str) -> List[RawJob]:
        """Ashby API: https://api.ashbyhq.com/posting-api/job-board/{company}"""
        jobs = []
        try:
            resp = requests.get(
                f"https://api.ashbyhq.com/posting-api/job-board/{company_slug}",
                headers=HEADERS,
                timeout=10,
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()
            listings = data.get("jobs", [])

            for entry in listings:
                desc = entry.get("descriptionPlain", "") or entry.get("description", "")
                desc_lower = desc.lower()

                location = entry.get("location", "Remote") or "Remote"
                if isinstance(location, dict):
                    location = location.get("name", "Remote")

                stack = [kw for kw in TECH_KEYWORDS if kw in desc_lower]

                title = entry.get("title", "Software Engineer")
                job_type = "internship" if "intern" in title.lower() else "full-time"

                # Parse date
                posted_at = None
                published = entry.get("publishedAt", "")
                if published:
                    try:
                        posted_at = datetime.fromisoformat(
                            published.replace("Z", "+00:00")
                        ).replace(tzinfo=None)
                    except Exception:
                        pass

                job_url = entry.get("jobUrl", "") or entry.get("applyUrl", "")

                jobs.append(RawJob(
                    title=title[:200],
                    company=company_slug.replace("-", " ").title()[:100],
                    apply_url=job_url,
                    source=self.source_name,
                    description=desc[:3000],
                    location=location,
                    job_type=job_type,
                    posted_at=posted_at,
                    stack_mentioned=stack,
                    requisition_id=(
                        f"ashby:{entry['id']}" if entry.get("id") else ""
                    ),
                ))

        except Exception as e:
            logger.debug(f"[ats_boards] Ashby '{company_slug}' failed: {e}")

        return jobs

    def scrape(self) -> List[RawJob]:
        start = time.time()
        logger.info(f"[{self.source_name}] Starting scrape across {len(self.companies)} companies...")

        all_jobs: List[RawJob] = []

        for entry in self.companies:
            # Each entry is {"slug": "company-name", "ats": "lever|greenhouse|ashby"}
            if isinstance(entry, str):
                # Simple string — try all ATS platforms
                slug = entry
                ats_type = "auto"
            elif isinstance(entry, dict):
                slug = entry.get("slug", "")
                ats_type = entry.get("ats", "auto")
            else:
                continue

            if not slug:
                continue

            if ats_type == "lever" or ats_type == "auto":
                jobs = self._scrape_lever(slug)
                if jobs:
                    all_jobs.extend(jobs)
                    logger.debug(f"[ats_boards] Lever '{slug}': {len(jobs)} jobs")
                    if ats_type == "auto":
                        continue  # Found on Lever, skip others

            if ats_type == "greenhouse" or ats_type == "auto":
                jobs = self._scrape_greenhouse(slug)
                if jobs:
                    all_jobs.extend(jobs)
                    logger.debug(f"[ats_boards] Greenhouse '{slug}': {len(jobs)} jobs")
                    if ats_type == "auto":
                        continue

            if ats_type == "ashby" or ats_type == "auto":
                jobs = self._scrape_ashby(slug)
                if jobs:
                    all_jobs.extend(jobs)
                    logger.debug(f"[ats_boards] Ashby '{slug}': {len(jobs)} jobs")

            time.sleep(0.3)  # Gentle pacing between companies

        duration = time.time() - start
        logger.info(f"[{self.source_name}] Done — {len(all_jobs)} jobs in {duration:.1f}s")
        return all_jobs
