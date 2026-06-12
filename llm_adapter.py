"""
llm_adapter.py — Multi-provider LLM abstraction for Apply-Nav.

Providers: Gemini 2.5 Flash → Ollama → Keyword Heuristic (always works).
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("apply_nav.llm")


# ─── Prompt Templates (exact per spec) ────────────────────────

SCORE_JOB_PROMPT = """You are a technical recruiter. Compare the candidate resume to the job description.
Return ONLY valid JSON with these exact keys: score (0-100 integer), matched_skills (list of strings), skill_gaps (list of strings), outreach_note (string, max 280 chars, first-person, professional).
Do not include any text outside the JSON object.

RESUME:
{resume_text}

JOB DESCRIPTION:
{job_description}"""

ANSWER_QUESTION_PROMPT = """You are filling out a job application form. Based on the candidate profile below, answer the application question concisely.
Return ONLY the answer text, no explanation, no JSON.

QUESTION: {question}
OPTIONS (if any): {options}

CANDIDATE PROFILE:
{profile_json}"""

MAP_FORM_FIELDS_PROMPT = """You are filling out a job application form. Map the form fields to the candidate's profile data.
Return ONLY valid JSON mapping field labels to their values. Use empty string for unknown fields.

FORM FIELDS: {field_labels}

CANDIDATE PROFILE:
{profile_json}"""

EXTRACT_RESUME_PROMPT = """Extract structured information from this resume.
Return ONLY valid JSON with these exact keys: name (string), email (string), phone (string), skills (list of strings), experience_years (string), current_title (string), education (string), summary (string).

RESUME TEXT:
{resume_text}"""


# ─── Heuristic Keywords ───────────────────────────────────────

_HEURISTIC_KEYWORDS = [
    ("java", "Java"), ("spring boot", "Spring Boot"), ("spring", "Spring"),
    ("kafka", "Kafka"), ("angular", "Angular"), ("postgresql", "PostgreSQL"),
    ("docker", "Docker"), ("kubernetes", "Kubernetes"), ("rest", "REST"),
    ("microservices", "Microservices"), ("python", "Python"), ("react", "React"),
    ("node", "Node.js"), ("aws", "AWS"), ("gcp", "GCP"), ("azure", "Azure"),
    ("mongodb", "MongoDB"), ("redis", "Redis"), ("graphql", "GraphQL"),
    ("typescript", "TypeScript"), ("javascript", "JavaScript"),
    ("terraform", "Terraform"), ("jenkins", "Jenkins"), ("ci/cd", "CI/CD"),
    ("git", "Git"), ("sql", "SQL"), ("linux", "Linux"), ("agile", "Agile"),
    ("spark", "Apache Spark"), ("golang", "Go"), ("fastapi", "FastAPI"),
]


class LLMAdapter:
    """Routes LLM calls to Gemini → Ollama → Heuristic fallback chain."""

    def __init__(self, config: Dict[str, Any], answer_cache=None):
        self._config = config.get("llm", {})
        self._user_config = config.get("user", {}) or config.get("candidate", {})
        self._provider = self._config.get("provider", "gemini")
        self._answer_cache = answer_cache
        logger.info("LLM adapter initialized: provider=%s", self._provider)

    @property
    def provider_name(self) -> str:
        return self._provider

    def has_api_key(self) -> bool:
        if self._provider == "gemini":
            return bool(self._get_gemini_key())
        if self._provider == "ollama":
            return True  # local, no key needed
        return False

    def _get_gemini_key(self) -> str:
        return (
            self._config.get("gemini_api_key", "")
            or self._config.get("gemini", {}).get("api_key", "")
            or os.environ.get("GEMINI_API_KEY", "")
        )

    def override_api_key(self, key: str) -> None:
        if "gemini" not in self._config:
            self._config["gemini"] = {}
        self._config["gemini"]["api_key"] = key
        self._config["gemini_api_key"] = key

    # ─── Job Scoring ──────────────────────────────────────────

    async def score_job(self, resume_text: str, job_description: str) -> Dict[str, Any]:
        """Score resume vs job description. Returns {score, matched_skills, skill_gaps, outreach_note}."""
        prompt = SCORE_JOB_PROMPT.format(
            resume_text=resume_text[:4000],
            job_description=job_description[:3000],
        )

        try:
            if self._provider == "gemini" and self._get_gemini_key():
                result = await self._gemini_json(prompt)
                return self._normalize_score_result(result)
            elif self._provider == "ollama":
                result = await self._ollama_call(prompt, json_mode=True)
                return self._normalize_score_result(result)
        except Exception as e:
            logger.warning("LLM scoring failed (%s): %s — using heuristic", self._provider, e)

        return self._heuristic_score(resume_text, job_description)

    def _normalize_score_result(self, result: dict) -> dict:
        """Normalize LLM score response to expected shape."""
        return {
            "score": int(result.get("score", 0)),
            "matched_skills": result.get("matched_skills", result.get("skills_matched", [])),
            "skill_gaps": result.get("skill_gaps", result.get("missing_skills", result.get("skills_missing", []))),
            "outreach_note": result.get("outreach_note", result.get("outreach", "")),
            "rationale": result.get("rationale", ""),
        }

    # ─── Answer Question ──────────────────────────────────────

    async def answer_question(self, question: str, options: List[str], resume_structured: dict) -> str:
        """Answer a form question. Check cache first, then LLM, then heuristic."""
        # Check cache
        if self._answer_cache:
            cached = self._answer_cache.get(question)
            if cached:
                return cached

        prompt = ANSWER_QUESTION_PROMPT.format(
            question=question,
            options=", ".join(options) if options else "N/A",
            profile_json=json.dumps(resume_structured, indent=2)[:2000],
        )

        answer = ""
        try:
            if self._provider == "gemini" and self._get_gemini_key():
                answer = await self._gemini_text(prompt)
            elif self._provider == "ollama":
                answer = await self._ollama_call(prompt, json_mode=False)
        except Exception as e:
            logger.warning("LLM answer_question failed: %s — using heuristic", e)

        if not answer:
            # Heuristic fallback
            if options:
                mid = len(options) // 2
                answer = options[mid]
            else:
                answer = "Yes"

        # Cache the answer
        if self._answer_cache and answer:
            self._answer_cache.set(question, answer)

        return answer

    # Legacy method name
    async def answer_screening_question(
        self, question: str, field_type: str, options: List[str], resume_text: str
    ) -> str:
        structured = {}
        try:
            from resume_manager import get_manager
            structured = get_manager().get_structured()
        except Exception:
            pass
        return await self.answer_question(question, options, structured)

    # ─── Form Field Mapping ───────────────────────────────────

    async def map_form_fields(self, field_labels: List[str], resume_structured: dict) -> Dict[str, str]:
        """Map field labels to profile values. Returns {label: value}."""
        prompt = MAP_FORM_FIELDS_PROMPT.format(
            field_labels=json.dumps(field_labels),
            profile_json=json.dumps(resume_structured, indent=2)[:2000],
        )

        try:
            if self._provider == "gemini" and self._get_gemini_key():
                result = await self._gemini_json(prompt)
                if isinstance(result, dict):
                    return result
            elif self._provider == "ollama":
                result = await self._ollama_call(prompt, json_mode=True)
                if isinstance(result, dict):
                    return result
        except Exception as e:
            logger.warning("LLM form field mapping failed: %s", e)

        # Heuristic: match label keywords to profile fields
        return self._heuristic_map_fields(field_labels, resume_structured)

    def _heuristic_map_fields(self, field_labels: List[str], profile: dict) -> Dict[str, str]:
        mapping = {}
        pii = {
            "first name": profile.get("name", "").split()[0] if profile.get("name") else "",
            "last name": profile.get("name", "").split()[-1] if profile.get("name") else "",
            "email": profile.get("email", ""),
            "phone": profile.get("phone", ""),
            "city": "",
            "location": "",
        }
        for label in field_labels:
            ll = label.lower()
            for key, val in pii.items():
                if key in ll and val:
                    mapping[label] = val
                    break
        return mapping

    # ─── Resume Structure Extraction ──────────────────────────

    async def extract_resume_structure(self, resume_text: str) -> dict:
        """Extract structured fields from raw resume text using LLM."""
        from resume_manager import ResumeManager
        prompt = EXTRACT_RESUME_PROMPT.format(resume_text=resume_text[:5000])

        try:
            if self._provider == "gemini" and self._get_gemini_key():
                return await self._gemini_json(prompt)
            elif self._provider == "ollama":
                return await self._ollama_call(prompt, json_mode=True)
        except Exception as e:
            logger.warning("LLM resume extraction failed: %s", e)

        mgr = ResumeManager()
        return mgr._heuristic_extract(resume_text)

    # ─── Gemini Provider ──────────────────────────────────────

    async def _gemini_json(self, prompt: str) -> dict:
        """Call Gemini and parse JSON response."""
        key = self._get_gemini_key()
        model = (
            self._config.get("gemini", {}).get("model", "")
            or "gemini-2.5-flash"
        )

        try:
            # Try new google.genai SDK first
            from google import genai as genai_new
            client = genai_new.Client(api_key=key)
            response = client.models.generate_content(model=model, contents=prompt)
            return self._parse_json_response(response.text)
        except (ImportError, AttributeError):
            pass

        # Fall back to google.generativeai
        import google.generativeai as genai
        genai.configure(api_key=key)
        model_obj = genai.GenerativeModel(model)
        response = model_obj.generate_content(prompt)
        return self._parse_json_response(response.text)

    async def _gemini_text(self, prompt: str) -> str:
        """Call Gemini and return plain text response."""
        key = self._get_gemini_key()
        model = self._config.get("gemini", {}).get("model", "") or "gemini-2.5-flash"

        try:
            from google import genai as genai_new
            client = genai_new.Client(api_key=key)
            response = client.models.generate_content(model=model, contents=prompt)
            return response.text.strip()
        except (ImportError, AttributeError):
            pass

        import google.generativeai as genai
        genai.configure(api_key=key)
        model_obj = genai.GenerativeModel(model)
        response = model_obj.generate_content(prompt)
        return response.text.strip()

    # ─── Ollama Provider ──────────────────────────────────────

    async def _ollama_call(self, prompt: str, json_mode: bool = False) -> Any:
        """Call Ollama local inference API."""
        import httpx

        base_url = (
            self._config.get("ollama_url", "")
            or self._config.get("ollama", {}).get("base_url", "http://localhost:11434")
        )
        model = (
            self._config.get("ollama_model", "")
            or self._config.get("ollama", {}).get("model", "llama3")
        )

        payload: dict = {"model": model, "prompt": prompt, "stream": False}
        if json_mode:
            payload["format"] = "json"

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{base_url}/api/generate", json=payload)
            resp.raise_for_status()
            data = resp.json()
            text = data.get("response", "").strip()
            if json_mode:
                return self._parse_json_response(text)
            return text

    @staticmethod
    def _parse_json_response(text: str) -> dict:
        """Strip markdown fences and parse JSON."""
        # Remove markdown code fences
        text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
        text = re.sub(r"```\s*$", "", text.strip(), flags=re.MULTILINE)
        text = text.strip()
        return json.loads(text)

    # ─── Heuristic Fallback ───────────────────────────────────

    def _heuristic_score(self, resume_text: str, job_description: str) -> Dict[str, Any]:
        """Keyword-overlap heuristic for offline/no-key operation."""
        jd_lower = job_description.lower()
        resume_lower = resume_text.lower()

        matched = []
        gaps = []
        for kw, display in _HEURISTIC_KEYWORDS:
            if kw in jd_lower:
                if kw in resume_lower:
                    matched.append(display)
                else:
                    gaps.append(display)

        total = len(matched) + len(gaps)
        score = int((len(matched) / total) * 100) if total > 0 else 50
        score = max(10, min(100, score))

        return {
            "score": score,
            "matched_skills": matched,
            "skill_gaps": gaps,
            "outreach_note": "Strong match on core backend skills.",
            "rationale": "Keyword heuristic scoring. Add an LLM API key for AI-powered analysis.",
        }
