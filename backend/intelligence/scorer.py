import json
import time
from typing import List, Optional, Tuple

import google.generativeai as genai
from loguru import logger

from scrapers.base import RawJob
from intelligence.rate_limiter import get_rate_limiter

SCORE_PROMPT = """You are scoring a job listing for a specific developer.

DEVELOPER PROFILE:
- Name: Haamid, B.Tech CS student (2023-2027), ~1 year experience
- Stack: React/Next.js, Node.js, Express, TypeScript, Python, FastAPI, PostgreSQL, Prisma, WebSockets, WebRTC, Socket.IO, LiveKit, Docker, AWS
- Niche strength: real-time systems, WebRTC infrastructure (SFU, mediasoup, LiveKit)
- Seeking: internship or entry-level full-time
- Notable projects: built real-time collaboration tools, WebRTC video systems

BOOST SIGNALS PROVIDED:
{boost_signals}

JOB LISTING:
Title: {title}
Company: {company}
Location: {location}
Description: {description}

PRE-COMPUTED SCORES (do NOT override these — they are deterministic):
- recency: {recency_score}/2
- reply_odds: {reply_odds_score}/2

Score on the remaining dimensions (return ONLY valid JSON, no markdown):
{{
  "role_match": <0-3>,      // Stack alignment:
                            // 3 = Perfect match (React.js, Next.js, Node.js, Express.js, TypeScript, Python)
                            // 2 = Good tech fit (Fullstack, Frontend, Backend, Web Dev, Software Engineer)
                            // 1 = Tangential (C++, Go, Rust, database-only, devops-only)
                            // 0 = Unrelated (Java, C#, .NET, Salesforce, PHP, WordPress, Sales, QA)
  
  "seniority_fit": <0-3>,   // Seniority and Experience Fit:
                            // 3 = EXPLICIT INTERN / INTERNSHIP or New Grad (0-1 yrs exp)
                            // 2 = Junior / Entry Level (1-2 yrs exp)
                            // 1 = Mid-level (2-3 yrs exp)
                            // 0 = Senior (Requires 3+ years, L3/L4 roles, or mentions "senior", "lead", "principal")
  
  "niche_bonus": <0-2>,     // WebRTC / WebSockets / Real-time match:
                            // 2 = Direct match (WebRTC, WebSockets, Socket.IO, LiveKit, mediasoup, SFU, real-time systems)
                            // 1 = Adjacent (streaming, low-latency, VoIP, audio/video streaming, chat apps)
                            // 0 = No relation
  
  "total": <sum of role_match + seniority_fit + niche_bonus + {recency_score} + {reply_odds_score}>,
  "verdict": "apply"|"maybe"|"skip",
  "reason": "<one sentence explaining the verdict>",
  "red_flags": ["<specific concern>", ...]  // empty array if none
}}

Verdict rules: total >= 9 → "apply", 6-8 → "maybe", <6 → "skip"

STRICT CALIBRATION RULES:
1. If the job requires a US Work Visa or US Citizenship, or specifies "US only" without offering international remote/sponsorship, you MUST score seniority_fit=0 and verdict="skip".
2. If the title contains "Senior", "Lead", "Principal", "Consultant", "WordPress", "Sales", "Sr.", or "Sr", you MUST score seniority_fit=0 and verdict="skip".
3. If the job requires 2+ years of experience, or is a mid/senior role (L3/L4), you MUST score seniority_fit=0 and verdict="skip".
4. Internships and Fullstack/Frontend/Backend/Web Dev roles with 0-1 years of experience matching our stack MUST get the absolute highest scores (role_match=3, seniority_fit=3, and niche_bonus=2 if WebRTC/real-time).
"""


import re

class QuotaExceededError(Exception):
    """Raised when a Gemini model hits a rate limit or quota restriction."""
    pass


def _smart_truncate(description: str, limit: int = 2000) -> str:
    """
    Smart description truncation that preserves tech stack sections.
    
    Job descriptions often list stack requirements at the bottom.
    Naive [:2000] slicing cuts them off. This function:
    1. Takes the first 1500 chars (context about the role)
    2. Scans the remaining text for tech stack markers
    3. Appends those sections to ensure the LLM sees full stack requirements
    """
    if len(description) <= limit:
        return description
    
    # Take the first chunk (role context)
    head_size = int(limit * 0.75)  # 1500 chars
    head = description[:head_size]
    
    # Scan the rest for stack/requirements sections
    remainder = description[head_size:]
    stack_markers = [
        "tech stack", "technologies", "requirements", "qualifications",
        "what you'll need", "skills", "must have", "nice to have",
        "tools we use", "our stack", "experience with",
    ]
    
    remainder_lower = remainder.lower()
    best_start = -1
    for marker in stack_markers:
        idx = remainder_lower.find(marker)
        if idx >= 0 and (best_start < 0 or idx < best_start):
            best_start = idx
    
    if best_start >= 0:
        # Found a stack section — append it
        tail_budget = limit - head_size
        tail = remainder[best_start:best_start + tail_budget]
        return head + "\n...\n" + tail
    else:
        # No stack section found — just take the last 500 chars (often has stack info)
        tail_budget = limit - head_size
        tail = description[-tail_budget:]
        return head + "\n...\n" + tail

def _compute_recency_score(boost_signals: dict) -> int:
    """Deterministic recency score — no LLM needed."""
    recency_hours = boost_signals.get("recency_hours", 9999)
    if recency_hours < 72:
        return 2
    elif recency_hours < 168:
        return 1
    return 0


def _compute_reply_odds_score(boost_signals: dict) -> int:
    """Deterministic reply odds score — no LLM needed."""
    score = 0
    if boost_signals.get("has_direct_email") or boost_signals.get("is_founder_post"):
        score += 1
    if boost_signals.get("is_small_company"):
        score += 1
    return min(2, score)


class GeminiScorer:
    def __init__(self, config):
        genai.configure(api_key=config.llm.api_key)
        self.config = config
        self.use_heuristic_only = False
        self.rate_limiter = get_rate_limiter("gemini", calls_per_minute=10)
        
        # Tier 1: Gemini direct API fallback chain
        default_chain = ["gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemini-3-flash-preview", "gemini-2.0-flash"]
        
        primary_model = config.llm.model
        if primary_model and primary_model not in default_chain:
            self.models_chain = [primary_model] + default_chain
        else:
            self.models_chain = default_chain
            if primary_model in default_chain:
                idx = default_chain.index(primary_model)
                self.models_chain = default_chain[idx:] + default_chain[:idx]
        
        self.current_model_index = 0
        
        # Tier 2: OpenRouter free models
        self._openrouter = None
        openrouter_key = getattr(config.llm, "openrouter_api_key", "")
        if openrouter_key:
            try:
                from intelligence.openrouter_client import OpenRouterClient
                self._openrouter = OpenRouterClient(openrouter_key)
                logger.info("[scorer] OpenRouter fallback enabled (Tier 2)")
            except Exception as e:
                logger.warning(f"[scorer] OpenRouter init failed: {e}")
        
        # Tier 3: Local MLX models
        self._local_scorer = None
        local_cfg = getattr(config.llm, "local_fallback", None)
        if local_cfg and getattr(local_cfg, "enabled", False):
            try:
                from intelligence.local_scorer import LocalMLXScorer
                model_names = getattr(local_cfg, "models", [])
                if model_names:
                    self._local_scorer = LocalMLXScorer(model_names)
                    if self._local_scorer.is_available():
                        logger.info(f"[scorer] Local MLX fallback enabled (Tier 3): {model_names}")
                    else:
                        self._local_scorer = None
            except Exception as e:
                logger.warning(f"[scorer] Local MLX init failed: {e}")
        
        logger.info(f"[scorer] Initialized — Gemini chain: {self.models_chain}")

    def _call_gemini_api_with_retry(self, model_name: str, prompt: str) -> str:
        """Call Gemini API for a specific model with simple retry logic for non-quota errors."""
        max_attempts = self.config.llm.max_retries or 3
        delay = self.config.llm.retry_delay_seconds or 5

        for attempt in range(max_attempts):
            try:
                self.rate_limiter.acquire()
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                self.rate_limiter.report_success()
                return response.text.strip()
            except TimeoutError:
                raise QuotaExceededError(f"Rate limiter timeout for {model_name}")
            except Exception as e:
                err_str = str(e).lower()
                err_name = e.__class__.__name__.lower()
                
                # Check for rate-limiting or quota errors
                is_quota_err = (
                    "resourceexhausted" in err_str or 
                    "resourceexhausted" in err_name or 
                    "quota" in err_str or 
                    "limit" in err_str or 
                    "429" in err_str
                )
                
                if is_quota_err:
                    self.rate_limiter.report_429()
                    raise QuotaExceededError(f"Quota exceeded for {model_name}: {e}")
                
                logger.warning(
                    f"[scorer] Model {model_name} failed (attempt {attempt + 1}/{max_attempts}): {e}. "
                    f"Retrying in {delay}s..."
                )
                if attempt < max_attempts - 1:
                    time.sleep(delay)
                else:
                    raise e

    def _score_single(self, job: RawJob, boost_signals: dict) -> dict:
        """Score one job. Automatically falls back to next models on rate-limit errors."""
        if self.use_heuristic_only:
            raise ValueError("LLM scoring disabled (heuristic-only fallback active).")

        # Compute deterministic scores
        recency_score = _compute_recency_score(boost_signals)
        reply_odds_score = _compute_reply_odds_score(boost_signals)

        prompt = SCORE_PROMPT.format(
            boost_signals=json.dumps(boost_signals, indent=2),
            title=job.title,
            company=job.company,
            location=job.location,
            description=_smart_truncate(job.description),
            recency_score=recency_score,
            reply_odds_score=reply_odds_score,
        )

        while self.current_model_index < len(self.models_chain):
            model_name = self.models_chain[self.current_model_index]
            logger.info(f"[scorer] Attempting to score '{job.title}' with {model_name}...")
            
            try:
                text = self._call_gemini_api_with_retry(model_name, prompt)
                
                # Strip markdown code fences if present
                if text.startswith("```"):
                    parts = text.split("```")
                    text = parts[1] if len(parts) > 1 else text
                    if text.startswith("json"):
                        text = text[4:]
                text = text.strip()

                data = json.loads(text)

                # Inject deterministic scores (override LLM if it hallucinated them)
                data["recency"] = recency_score
                data["reply_odds"] = reply_odds_score
                
                # Recompute total with deterministic values
                data["total"] = (
                    data.get("role_match", 0) +
                    data.get("seniority_fit", 0) +
                    data["reply_odds"] +
                    data["recency"] +
                    data.get("niche_bonus", 0)
                )
                
                # Re-derive verdict from corrected total
                if data["total"] >= 9:
                    data["verdict"] = "apply"
                elif data["total"] >= 6:
                    data["verdict"] = "maybe"
                else:
                    data["verdict"] = "skip"

                # Validate required keys
                required = ["role_match", "seniority_fit", "reply_odds", "recency", "niche_bonus", "total", "verdict", "reason"]
                for key in required:
                    if key not in data:
                        raise ValueError(f"LLM response missing key: {key}")

                return data

            except QuotaExceededError:
                logger.warning(
                    f"[scorer] Model {model_name} quota exceeded for '{job.title}'. "
                    f"Transitioning to the next model in the fallback chain."
                )
                self.current_model_index += 1

            except Exception as e:
                logger.error(
                    f"[scorer] Model {model_name} failed with general error for '{job.title}': {e}. "
                    f"Trying next model in fallback chain."
                )
                self.current_model_index += 1

        self.use_heuristic_only = True
        logger.error("[scorer] All models in the fallback chain have been exhausted. Switching to HEURISTIC mode.")
        raise ValueError("Fallback chain exhausted.")

    def _heuristic_score(self, job: RawJob, boost_signals: dict) -> dict:
        """Rule-based fallback scorer when LLM is unavailable."""
        niche_count = len(boost_signals.get("niche_skill_matches", []))
        is_entry = boost_signals.get("is_entry_level", False)

        desc_lower = job.description.lower()
        title_lower = job.title.lower()

        # 1. Determine stack alignment
        role_match = 1
        # Perfect stack matches
        perfect_stack = ["react", "next.js", "nextjs", "node.js", "nodejs", "typescript", "python"]
        if any(s in desc_lower or s in title_lower for s in perfect_stack):
            role_match = 3
        # Good general match
        elif any(s in title_lower for s in ["fullstack", "frontend", "backend", "web dev", "software engineer"]):
            role_match = 2

        # 2. Determine Seniority Fit with strict rules
        seniority_fit = 2 if is_entry else 1
        red_flags = []
        
        # Internships get the absolute highest boost
        if "intern" in title_lower or "internship" in title_lower or "intern" in job.job_type.lower():
            seniority_fit = 3

        # Seniority title rules
        is_senior = any(s in title_lower for s in ["senior", "lead", "principal", "consultant", "wordpress", "sales", "sr.", "sr"])
        if is_senior:
            seniority_fit = 0
            red_flags.append("Seniority or irrelevant non-technical role keywords in title")

        # Visa / Work Authorization Rules
        is_us_only = False
        visa_reject_patterns = [
            r"must be (?:authorized|eligible) to work in the (?:us|u\.s\.|united states)",
            r"us (?:work )?authorization (?:is )?required",
            r"(?:u\.s\.|us) citizen(?:ship)? (?:is )?required",
            r"green card (?:holders? )?(?:is )?required",
            r"no (?:visa )?sponsorship (?:available|offered|provided)",
            r"we (?:cannot|do not) (?:offer|provide|sponsor) (?:h1b|h1-b|visa) sponsorship",
            r"not open to (?:candidates|applicants) (?:outside|outside of) the (?:us|u\.s\.|united states)",
            r"only (?:hiring|open to) (?:candidates|applicants) (?:located|residing) in the (?:us|u\.s\.|united states)",
            r"must reside in the (?:us|u\.s\.|united states)",
        ]
        for pattern in visa_reject_patterns:
            if re.search(pattern, desc_lower) or re.search(pattern, title_lower):
                is_india_friendly = "india" in title_lower or "india" in job.location.lower() or "remote (worldwide)" in desc_lower
                if not is_india_friendly:
                    is_us_only = True
                    red_flags.append(f"US visa/work authorization constraint: '{pattern}'")
                    break

        if is_us_only:
            seniority_fit = 0

        # 3. Deterministic scores
        reply_odds = _compute_reply_odds_score(boost_signals)
        recency = _compute_recency_score(boost_signals)
        niche_bonus = min(2, niche_count)
        
        # Calculate total
        total = role_match + seniority_fit + reply_odds + recency + niche_bonus
        if is_senior or is_us_only:
            total = min(5, total)  # Strictly enforce skip

        verdict = "apply" if total >= 9 else ("maybe" if total >= 6 else "skip")

        return {
            "role_match": role_match,
            "seniority_fit": seniority_fit,
            "reply_odds": reply_odds,
            "recency": recency,
            "niche_bonus": niche_bonus,
            "total": total,
            "verdict": verdict,
            "reason": "Heuristic score (US visa restriction or Seniority mismatch)" if (is_senior or is_us_only) else "Heuristic score (LLM unavailable)",
            "red_flags": red_flags,
        }

    def _parse_and_fix_score(self, text: str, boost_signals: dict) -> dict:
        """Parse LLM text response into score dict with deterministic corrections."""
        # Strip markdown code fences if present
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
        
        # Find JSON in text
        text = text.strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]

        data = json.loads(text)

        # Inject deterministic scores
        recency_score = _compute_recency_score(boost_signals)
        reply_odds_score = _compute_reply_odds_score(boost_signals)
        data["recency"] = recency_score
        data["reply_odds"] = reply_odds_score

        # Recompute total
        data["total"] = (
            data.get("role_match", 0) +
            data.get("seniority_fit", 0) +
            data["reply_odds"] +
            data["recency"] +
            data.get("niche_bonus", 0)
        )

        # Re-derive verdict
        if data["total"] >= 9:
            data["verdict"] = "apply"
        elif data["total"] >= 6:
            data["verdict"] = "maybe"
        else:
            data["verdict"] = "skip"

        # Ensure required keys
        data.setdefault("reason", "")
        data.setdefault("red_flags", [])
        
        return data

    def _try_openrouter(self, prompt: str, boost_signals: dict) -> Optional[dict]:
        """Tier 2: Try scoring via OpenRouter free models."""
        if not self._openrouter or not self._openrouter.is_available():
            return None

        logger.info("[scorer] Trying OpenRouter fallback (Tier 2)...")
        text = self._openrouter.generate(prompt)
        if not text:
            return None

        try:
            return self._parse_and_fix_score(text, boost_signals)
        except Exception as e:
            logger.warning(f"[scorer] OpenRouter response parse failed: {e}")
            return None

    def _try_local_mlx(self, prompt: str, boost_signals: dict) -> Optional[dict]:
        """Tier 3: Try scoring via local MLX model."""
        if not self._local_scorer:
            return None

        logger.info("[scorer] Trying local MLX fallback (Tier 3)...")
        text = self._local_scorer.generate(prompt)
        if not text:
            return None

        try:
            return self._parse_and_fix_score(text, boost_signals)
        except Exception as e:
            logger.warning(f"[scorer] Local MLX response parse failed: {e}")
            return None

    def score_with_fallback(self, job: RawJob, boost_signals: dict) -> Tuple[dict, str]:
        """
        4-tier scoring fallback:
        Tier 1: Gemini direct API (4 models)
        Tier 2: OpenRouter free models (3 models)
        Tier 3: Local MLX models (2 models)
        Tier 4: Heuristic (deterministic rules)
        
        Returns (score_dict, method) where method is 'llm', 'openrouter', 'local_mlx', or 'heuristic'.
        """
        # Build prompt once for all tiers
        recency_score = _compute_recency_score(boost_signals)
        reply_odds_score = _compute_reply_odds_score(boost_signals)
        prompt = SCORE_PROMPT.format(
            boost_signals=json.dumps(boost_signals, indent=2),
            title=job.title,
            company=job.company,
            location=job.location,
            description=_smart_truncate(job.description),
            recency_score=recency_score,
            reply_odds_score=reply_odds_score,
        )

        # Tier 1: Gemini. Once exhausted, skip only this tier on later jobs;
        # provider fallbacks must remain available for the rest of the batch.
        if not self.use_heuristic_only:
            try:
                score = self._score_single(job, boost_signals)
                logger.debug(
                    f"[scorer] Gemini scored '{job.title}' @ '{job.company}': "
                    f"{score['total']}/12 ({score['verdict']})"
                )
                return score, "llm"
            except Exception as e:
                logger.warning(f"[scorer] Gemini chain exhausted for '{job.title}': {e}")

        # Tier 2: OpenRouter
        score = self._try_openrouter(prompt, boost_signals)
        if score:
            logger.debug(
                f"[scorer] OpenRouter scored '{job.title}': "
                f"{score['total']}/12 ({score['verdict']})"
            )
            return score, "openrouter"

        # Tier 3: Local MLX
        score = self._try_local_mlx(prompt, boost_signals)
        if score:
            logger.debug(
                f"[scorer] Local MLX scored '{job.title}': "
                f"{score['total']}/12 ({score['verdict']})"
            )
            return score, "local_mlx"

        # Tier 4: Heuristic
        logger.warning(
            f"[scorer] All LLM tiers failed for '{job.title}'. Using heuristic."
        )
        return self._heuristic_score(job, boost_signals), "heuristic"

    def score_batch(self, jobs: List[Tuple[RawJob, dict]]) -> List[dict]:
        """Score a list of (job, boost_signals) pairs. Never raises."""
        results = []
        for job, boost in jobs:
            score, method = self.score_with_fallback(job, boost)
            score["job_id"] = job.id
            score["scoring_method"] = method
            results.append(score)
            # Rate limiter handles pacing — no need for sleep here
        return results

    def score_from_db_records(self, unscored_jobs: List[dict]) -> List[dict]:
        """
        Score jobs from DB records (two-phase pipeline).
        Takes list of dicts from db.get_unscored_jobs() which include _boost_signals.
        """
        results = []
        for job_dict in unscored_jobs:
            boost = job_dict.get("_boost_signals", {})
            
            # Create a minimal RawJob for scoring
            raw = RawJob(
                title=job_dict["title"],
                company=job_dict["company"],
                apply_url=job_dict["apply_url"],
                source=job_dict["source"],
                description=job_dict.get("description", ""),
                location=job_dict.get("location", ""),
                job_type=job_dict.get("job_type", ""),
                requisition_id=job_dict.get("requisition_id", ""),
            )
            
            score, method = self.score_with_fallback(raw, boost)
            score["job_id"] = job_dict["id"]
            score["scoring_method"] = method
            results.append(score)
        
        return results
