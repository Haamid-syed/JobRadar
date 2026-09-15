from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit


def canonical_job_id(company: str, title: str) -> str:
    """Build the company/title grouping key used during identity comparison."""
    norm_company = re.sub(r"[^a-z0-9]", "", company.lower().strip())
    norm_title = re.sub(r"[^a-z0-9]", "", title.lower().strip())
    return hashlib.sha256(f"{norm_company}{norm_title}".encode()).hexdigest()[:16]


def _normalize_requisition_id(requisition_id: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (requisition_id or "").lower())


def _normalize_explicit_location(location: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", (location or "").lower())
    normalized = normalized.replace("bengaluru", "bangalore")
    generic_locations = {
        "",
        "anywhere",
        "flexible",
        "global",
        "multiplelocations",
        "remote",
        "unspecified",
        "worldwide",
    }
    return "" if normalized in generic_locations else normalized


def _normalize_apply_url(apply_url: str) -> str:
    if not apply_url:
        return ""
    parsed = urlsplit(apply_url.strip())
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
        )
    )
    host_and_path = f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
    return f"{host_and_path}?{query}" if query else host_and_path


@dataclass(frozen=True)
class JobIdentity:
    company: str
    title: str
    apply_url: str = ""
    location: str = ""
    requisition_id: str = ""

    @property
    def canonical_id(self) -> str:
        return canonical_job_id(self.company, self.title)


def jobs_are_duplicates(left: JobIdentity, right: JobIdentity) -> bool:
    """Compare two jobs using explicit evidence within a company/title group.

    Conflicting requisition IDs or specific locations identify distinct openings.
    A matching requisition ID, specific location, or application URL supports a
    duplicate match. Without such evidence, the pair is ambiguous and preserved.
    """
    if left.canonical_id != right.canonical_id:
        return False

    left_location = _normalize_explicit_location(left.location)
    right_location = _normalize_explicit_location(right.location)
    if left_location and right_location and left_location != right_location:
        return False

    left_requisition = _normalize_requisition_id(left.requisition_id)
    right_requisition = _normalize_requisition_id(right.requisition_id)
    if left_requisition and right_requisition and left_requisition != right_requisition:
        return False
    if left_requisition and right_requisition:
        return True

    left_url = _normalize_apply_url(left.apply_url)
    right_url = _normalize_apply_url(right.apply_url)
    if left_url and right_url and left_url == right_url:
        return True

    return bool(left_location and right_location)


@dataclass
class RawJob:
    title: str
    company: str
    apply_url: str
    source: str
    description: str = ""
    location: str = ""
    job_type: str = ""
    posted_at: Optional[datetime] = None
    salary_range: str = ""
    company_size: str = ""
    stack_mentioned: List[str] = field(default_factory=list)
    requisition_id: str = ""

    @property
    def id(self) -> str:
        requisition = _normalize_requisition_id(self.requisition_id)
        location = _normalize_explicit_location(self.location)
        evidence = f"{requisition}|{location}"
        if not requisition and not location:
            evidence = _normalize_apply_url(self.apply_url)
        raw = (
            f"{self.company.lower().strip()}|{self.title.lower().strip()}|"
            f"{self.source}|{evidence}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @property
    def canonical_id(self) -> str:
        """Company/title grouping key; evidence decides whether grouped jobs match."""
        return canonical_job_id(self.company, self.title)

    @property
    def identity(self) -> JobIdentity:
        return JobIdentity(
            company=self.company,
            title=self.title,
            apply_url=self.apply_url,
            location=self.location,
            requisition_id=self.requisition_id,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "company": self.company,
            "apply_url": self.apply_url,
            "source": self.source,
            "description": self.description,
            "location": self.location,
            "job_type": self.job_type,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "salary_range": self.salary_range,
            "company_size": self.company_size,
            "stack_mentioned": json.dumps(self.stack_mentioned),
            "requisition_id": self.requisition_id,
        }


class BaseScraper:
    """All scrapers inherit this. Provides retry wrapper and circuit-breaker awareness."""
    source_name: str = "base"
    timeout_seconds: int = 30

    def scrape(self) -> List[RawJob]:
        raise NotImplementedError

    def safe_scrape(self) -> tuple:
        """Returns (jobs, error_message). Never raises."""
        try:
            jobs = self.scrape()
            return jobs, None
        except Exception as e:
            return [], str(e)
