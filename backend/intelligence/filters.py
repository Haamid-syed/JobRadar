import re
from datetime import datetime, timedelta
from typing import List, Dict, Tuple

from loguru import logger

from scrapers.base import RawJob, jobs_are_duplicates


class HardFilter:
    """
    Applies hard rejection rules to raw jobs and computes boost signals
    for the LLM scorer. Also deduplicates against the database.
    """

    def __init__(self, config, db=None):
        self.config = config
        self.db = db
        self.max_age_days = config.filters.max_age_days
        self.exclude_keywords = [kw.lower() for kw in config.filters.exclude_keywords]
        self.exclude_company_keywords = [kw.lower() for kw in config.filters.exclude_company_keywords]
        self.experience_years = config.profile.experience_years
        self.niche_skills = [s.lower() for s in config.profile.skills.niche]
        
        # Compile dynamic positive technical title keywords
        self.role_types = [r.lower() for r in getattr(config.filters, "role_types", [])]
        self.primary_skills = [s.lower() for s in getattr(config.profile.skills, "primary", [])]
        self.secondary_skills = [s.lower() for s in getattr(config.profile.skills, "secondary", [])]
        
        self.allowed_terms = set()
        # Add all words from role types
        for rt in self.role_types:
            self.allowed_terms.update(rt.split())
        # Add all words from skills
        for s in self.primary_skills + self.secondary_skills + self.niche_skills:
            self.allowed_terms.update(s.split())
        # Add standard software engineering/tech base words
        base_words = [
            "developer", "engineer", "programmer", "architect", "intern", 
            "development", "backend", "frontend", "coder", "programming", 
            "software", "fullstack", "front-end", "back-end", "full-stack", "tech",
            "web", "dev", "internship", "engineering"
        ]
        self.allowed_terms.update(base_words)
        
        # Clean punctuation and normalize terms
        self.allowed_terms = {re.sub(r"[^\w\-]", "", w).lower() for w in self.allowed_terms if w}

    def _compute_boost_signals(self, job: RawJob) -> dict:
        """Compute pre-score boost signals for the LLM scorer."""
        desc_lower = job.description.lower()
        title_lower = job.title.lower()
        text = f"{title_lower} {desc_lower}"

        # Direct email in description
        has_direct_email = bool(re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", job.description))

        # Entry-level signals
        entry_level_terms = ["intern", "junior", "entry", "new grad", "0-2 years", "fresher", "graduate"]
        is_entry_level = any(t in text for t in entry_level_terms)

        # Company size
        is_small_company = False
        if job.company_size:
            nums = re.findall(r"\d+", job.company_size.replace(",", ""))
            if nums:
                try:
                    max_size = max(int(n) for n in nums)
                    is_small_company = max_size < 150
                except Exception:
                    pass

        # Recency
        recency_hours = 9999
        if job.posted_at:
            delta = datetime.now() - job.posted_at
            recency_hours = int(delta.total_seconds() / 3600)

        # Niche skill matches
        niche_matches = [skill for skill in self.niche_skills if skill in desc_lower]

        # Founder post: HN source + direct email
        is_founder_post = job.source == "hn" and has_direct_email

        return {
            "has_direct_email": has_direct_email,
            "is_entry_level": is_entry_level,
            "is_small_company": is_small_company,
            "recency_hours": recency_hours,
            "niche_skill_matches": niche_matches,
            "is_founder_post": is_founder_post,
        }

    def _should_reject(self, job: RawJob) -> Tuple[bool, str]:
        """Returns (should_reject, reason)."""
        # 1. Too old
        if job.posted_at:
            cutoff = datetime.now() - timedelta(days=self.max_age_days)
            if job.posted_at < cutoff:
                return True, f"Too old: posted {job.posted_at.date()}"

        # 2. LinkedIn Easy Apply
        if "linkedin.com/jobs/apply" in job.apply_url:
            return True, "LinkedIn Easy Apply"

        # 3. Exclude company keywords
        company_lower = job.company.lower()
        for kw in self.exclude_company_keywords:
            if kw in company_lower:
                return True, f"Company excluded: '{kw}' in '{job.company}'"

        # 4. Exclude keywords check (experience limits checked in both; seniority checked strictly in title)
        title_lower = job.title.lower()
        desc_lower = job.description.lower()
        for kw in self.exclude_keywords:
            if "years" in kw or "experience" in kw:
                if kw in title_lower or kw in desc_lower:
                    return True, f"Excluded experience limit keyword: '{kw}'"
            else:
                if kw in title_lower:
                    return True, f"Excluded seniority keyword in title: '{kw}'"

        # 4b. Strict Regex Experience Filter (strictly reject 2, 2+, 3+, 4+, 5+ years of required experience)
        # Parses explicit ranges like "2-6 years", "2+ years", "3 years", "5yrs"
        exp_matches = re.findall(r"(\d+)\s*(?:-\s*(\d+))?\s*(?:or more)?\s*(?:plus|\+)?\s*(?:years?|yrs?)\s*(?:of\s*)?(?:experience|exp)?", desc_lower + " " + title_lower)
        for min_exp_str, max_exp_str in exp_matches:
            try:
                min_exp = int(min_exp_str)
                # If the required minimum experience is 2 or more, strictly reject!
                # We want to allow 0-1, 0-2, 1-2, 1 year, etc., so we only reject if min_exp >= 2.
                if min_exp >= 2:
                    return True, f"Requires {min_exp}+ years of experience (cap is strictly < 2 years)"
            except ValueError:
                pass
                
        # Regex check for other explicit phrasing: "minimum of 2 years", "at least 2 years", "2 or more years", "5yrs", "2yrs"
        explicit_exp_patterns = [
            r"minimum of [2-9]\s*(?:years?|yrs?)",
            r"at least [2-9]\s*(?:years?|yrs?)",
            r"[2-9]\s*or more\s*(?:years?|yrs?)",
            r"\b[2-9]\s*(?:years?|yrs?)\s*exp\b",
            r"\b[2-9]yrs?\b",
            r"\b[2-9]\s*years?\s*of\s*experience\b",
        ]
        for pattern in explicit_exp_patterns:
            if re.search(pattern, desc_lower) or re.search(pattern, title_lower):
                return True, f"Requires 2+ years of experience: matched '{pattern}'"

        # 4c. US Visa & Work Authorization Filter
        # Excludes jobs that strictly require US citizenship, green card, existing US authorization, or refuse sponsorship
        visa_reject_patterns = [
            r"must be (?:authorized|eligible) to work in the (?:us|u\.s\.|united states)",
            r"us (?:work )?authorization (?:is )?required",
            r"(?:u\.s\.|us) citizen(?:ship)? (?:is )?required",
            r"green card (?:holders? )?(?:is )?required",
            r"no (?:visa )?sponsorship (?:available|offered|provided)",
            r"we (?:cannot|do not) (?:offer|provide|sponsor) (?:h1b|h1-b|visa) sponsorship",
            r"not open to (?:candidates|applicants) (?:outside|outside of) the (?:us|u\.s\.|united states)",
            r"requires (?:us|u\.s\.|united states) citizenship",
            r"only (?:hiring|open to) (?:candidates|applicants) (?:located|residing) in the (?:us|u\.s\.|united states)",
            r"sponsorship is not (?:offered|available)",
            r"visa sponsorship (?:is )?not available",
            r"must reside in the (?:us|u\.s\.|united states)",
            r"authorized to work in the united states",
        ]
        for pattern in visa_reject_patterns:
            if re.search(pattern, desc_lower) or re.search(pattern, title_lower):
                # Only filter out if the job is NOT explicit about remote-worldwide/India
                is_india_friendly = "india" in title_lower or "india" in job.location.lower() or "remote (worldwide)" in desc_lower
                if not is_india_friendly:
                    return True, f"Requires US work authorization / Visa constraint found: '{pattern}'"

        # 5. Dedicated title-based negative keywords (seniority/non-tech roles)
        negative_title_keywords = {
            "senior", "principal", "consultant", "wordpress", "sales", "lead", 
            "sr", "sr.", "director", "manager", "vp", "head", "staff", "architect",
            "wordpress developer", "sales executive", "lead engineer", "lead developer"
        }
        
        # Extract word tokens from title for boundary-safe matching
        title_words = {re.sub(r"[^\w\-]", "", w).lower() for w in title_lower.split()}
        title_words = {w for w in title_words if w}
        
        for kw in negative_title_keywords:
            if " " in kw or "." in kw:
                if kw in title_lower:
                    return True, f"Excluded title keyword (phrase): '{kw}'"
            else:
                if kw in title_words:
                    return True, f"Excluded title keyword (word): '{kw}'"

        # 6. Senior role filter (explicit experience cap check)
        if "senior" in title_lower and self.experience_years < 3:
            return True, "Senior role filtered (< 3 years experience)"

        # 7. Technical Role Filter (Dynamic Title Word Overlap)
        if not title_words.intersection(self.allowed_terms):
            return True, f"Non-technical role (no matching keyword in title: '{job.title}')"

        return False, ""

    def filter(self, jobs: List[RawJob]) -> List[RawJob]:
        """Filter list of jobs, return only those that pass."""
        filtered, _ = self.filter_with_signals(jobs)
        return filtered

    def filter_with_signals(
        self, jobs: List[RawJob]
    ) -> Tuple[List[RawJob], Dict[str, dict]]:
        """
        Filter jobs and return (filtered_jobs, boost_signals_map).
        boost_signals_map: {job.id -> boost_signals_dict}
        """
        # First pass: apply hard rejection rules
        candidates: List[RawJob] = []
        rejected_count = 0

        for job in jobs:
            should_reject, reason = self._should_reject(job)
            if should_reject:
                logger.debug(f"[filter] REJECT '{job.title}' @ '{job.company}' — {reason}")
                rejected_count += 1
            else:
                candidates.append(job)

        # Group by normalized company/title, then merge only when requisition,
        # location, or URL evidence supports the match.
        cross_dedup_count = 0
        deduped_candidates: List[RawJob] = []
        canonical_groups: Dict[str, List[int]] = {}
        for job in candidates:
            cid = job.canonical_id
            duplicate_index = next(
                (
                    index
                    for index in canonical_groups.get(cid, [])
                    if jobs_are_duplicates(job.identity, deduped_candidates[index].identity)
                ),
                None,
            )
            if duplicate_index is not None:
                existing = deduped_candidates[duplicate_index]
                if len(job.description) > len(existing.description):
                    deduped_candidates[duplicate_index] = job
                cross_dedup_count += 1
            else:
                canonical_groups.setdefault(cid, []).append(len(deduped_candidates))
                deduped_candidates.append(job)
        candidates = deduped_candidates

        # Batch dedup against DB (1 query instead of N)
        duplicate_count = 0
        if self.db and candidates:
            candidate_ids = [job.id for job in candidates]
            existing_map = self.db.get_existing_job_ids(candidate_ids)
            existing_identities = self.db.get_existing_job_identities(
                [job.canonical_id for job in candidates]
            )
            deduped: List[RawJob] = []
            for job in candidates:
                identity_match = any(
                    jobs_are_duplicates(job.identity, existing)
                    for existing in existing_identities.get(job.canonical_id, [])
                )
                if job.id in existing_map or identity_match:
                    duplicate_count += 1
                else:
                    deduped.append(job)
            candidates = deduped

        # Compute boost signals for surviving jobs
        signals_map: Dict[str, dict] = {}
        for job in candidates:
            signals_map[job.id] = self._compute_boost_signals(job)

        logger.info(
            f"[filter] {len(candidates)} passed / {rejected_count} rejected / "
            f"{duplicate_count} db-dupes / {cross_dedup_count} cross-source dupes "
            f"(from {len(jobs)} total)"
        )
        return candidates, signals_map
