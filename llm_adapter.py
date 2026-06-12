"""
LLM Adapter for Apply-Nav.

Provider-agnostic interface for AI operations: job scoring,
screening question answering, and form field mapping.

Supports: Gemini, Ollama (local), and keyword heuristic fallback.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from answer_cache import AnswerCache

logger = logging.getLogger("apply_nav.llm")

# ─── Prompt Templates ───

SCORE_JOB_PROMPT = """Evaluate how well this candidate's resume matches the job description.

Resume:
{resume_text}

Job Description:
{job_description}

Output a JSON object with these fields:
- score: integer 0-100 (how well the candidate matches)
- rationale: string explaining the score
- matched_skills: array of skills the candidate has that the job requires
- missing_skills: array of skills the job requires that the candidate lacks
- outreach_note: a personalized 2-3 sentence message the candidate could send to the hiring manager"""

SCREENING_QUESTION_PROMPT = """You are helping a job applicant answer a screening question on a job application form.

Applicant Profile:
- Name: {first_name} {last_name}
- Location: {city}
- Work Authorization: {work_authorization}
- Experience: {years_of_experience} years

Resume (abbreviated):
{resume_text}

Question: "{question}"
Field Type: {field_type}
Available Options: {options}

Instructions:
- Answer concisely and accurately based on the resume
- If it's a yes/no or radio question, respond with ONLY the answer text
- If it's a text field, respond with a brief, professional answer
- If the resume doesn't contain the information, make a reasonable inference
- Output ONLY the answer, no explanation"""

FORM_FIELD_MAPPING_PROMPT = """Map the following form fields to the applicant's data.

Applicant Data:
{user_data_json}

Form Fields (label → field_id):
{form_fields_json}

For each field, output a JSON object mapping field_id to the value to fill in.
Only include fields you can confidently fill. Skip fields where you're unsure."""


class LLMAdapter:
    """Routes LLM calls to the configured provider."""

    def __init__(self, config: Dict[str, Any], answer_cache: Optional[AnswerCache] = None):
        self._config = config.get("llm", {})
        self._provider = self._config.get("provider", "gemini")
        self._user_config = config.get("user", {})
        self._answer_cache = answer_cache or AnswerCache()
        logger.info("LLM adapter initialized: provider=%s", self._provider)

    @property
    def provider_name(self) -> str:
        return self._provider

    def has_api_key(self) -> bool:
        """Check if the current provider has a usable API key configured."""
        if self._provider == "gemini":
            key = self._get_gemini_key()
            return bool(key)
        elif self._provider == "ollama":
            return True  # Ollama runs locally, no key needed
        return False

    def _get_gemini_key(self) -> str:
        """Get Gemini API key from config or environment."""
        return (
            self._config.get("gemini", {}).get("api_key", "")
            or os.environ.get("GEMINI_API_KEY", "")
        )

    def override_api_key(self, key: str) -> None:
        """Override the API key at runtime (e.g., from frontend input)."""
        if self._provider == "gemini":
            if "gemini" not in self._config:
                self._config["gemini"] = {}
            self._config["gemini"]["api_key"] = key

    # ─── Job Scoring ───

    async def score_job(self, resume_text: str, job_description: str) -> Dict[str, Any]:
        """Score how well a resume matches a job description.
        
        Returns: {score, rationale, matched_skills, missing_skills, outreach_note}
        """
        if not self.has_api_key() and self._provider != "ollama":
            return self._heuristic_score(resume_text, job_description)

        prompt = SCORE_JOB_PROMPT.format(
            resume_text=resume_text[:4000],
            job_description=job_description[:3000],
        )

        try:
            if self._provider == "gemini":
                return await self._gemini_score(prompt)
            elif self._provider == "ollama":
                return await self._ollama_call(prompt, json_mode=True)
            else:
                return self._heuristic_score(resume_text, job_description)
        except Exception as e:
            logger.error("LLM scoring failed: %s — falling back to heuristic", e)
            result = self._heuristic_score(resume_text, job_description)
            result["rationale"] = f"AI scoring failed ({e}). Using keyword heuristic."
            return result

    async def _gemini_score(self, prompt: str) -> Dict[str, Any]:
        """Call Gemini for job scoring with structured JSON output."""
        from google import genai
        from google.genai import types

        key = self._get_gemini_key()
        model = self._config.get("gemini", {}).get("model", "gemini-2.5-flash")

        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "score": types.Schema(type=types.Type.INTEGER),
                        "rationale": types.Schema(type=types.Type.STRING),
                        "matched_skills": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.STRING),
                        ),
                        "missing_skills": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.STRING),
                        ),
                        "outreach_note": types.Schema(type=types.Type.STRING),
                    },
                    required=["score", "rationale", "matched_skills", "missing_skills"],
                ),
            ),
        )
        return json.loads(response.text)

    # ─── Screening Questions ───

    async def answer_screening_question(
        self,
        question: str,
        field_type: str,
        options: List[str],
        resume_text: str,
    ) -> str:
        """Generate an answer for a screening question.
        
        Checks the answer cache first. On cache miss, calls the LLM
        and caches the result for future use.
        """
        # Check cache first
        cached = self._answer_cache.get(question)
        if cached:
            logger.info("Answer cache HIT for: %s", question[:60])
            return cached

        if not self.has_api_key() and self._provider != "ollama":
            return ""

        prompt = SCREENING_QUESTION_PROMPT.format(
            first_name=self._user_config.get("first_name", ""),
            last_name=self._user_config.get("last_name", ""),
            city=self._user_config.get("city", ""),
            work_authorization=self._user_config.get("work_authorization", ""),
            years_of_experience=self._user_config.get("years_of_experience", ""),
            resume_text=resume_text[:2000],
            question=question,
            field_type=field_type,
            options=", ".join(options) if options else "N/A",
        )

        try:
            if self._provider == "gemini":
                answer = await self._gemini_text(prompt)
            elif self._provider == "ollama":
                result = await self._ollama_call(prompt, json_mode=False)
                answer = result if isinstance(result, str) else str(result)
            else:
                return ""

            # Cache the answer for future use
            if answer:
                self._answer_cache.set(question, answer, field_type)
            return answer
        except Exception as e:
            logger.error("LLM screening question failed: %s", e)
            return ""

    async def _gemini_text(self, prompt: str) -> str:
        """Call Gemini for plain text response."""
        from google import genai

        key = self._get_gemini_key()
        model = self._config.get("gemini", {}).get("model", "gemini-2.5-flash")

        client = genai.Client(api_key=key)
        response = client.models.generate_content(model=model, contents=prompt)
        return response.text.strip()

    # ─── Form Field Mapping (for external ATS) ───

    async def map_form_fields(
        self, form_fields: List[Dict], user_data: Dict
    ) -> Dict[str, str]:
        """Map user data to ATS form fields using AI."""
        if not self.has_api_key() and self._provider != "ollama":
            return {}

        prompt = FORM_FIELD_MAPPING_PROMPT.format(
            user_data_json=json.dumps(user_data, indent=2),
            form_fields_json=json.dumps(form_fields, indent=2),
        )

        try:
            if self._provider == "gemini":
                result_text = await self._gemini_text(prompt)
                # Try to parse as JSON
                return json.loads(result_text)
            elif self._provider == "ollama":
                return await self._ollama_call(prompt, json_mode=True)
            return {}
        except Exception as e:
            logger.error("LLM form mapping failed: %s", e)
            return {}

    # ─── Ollama Provider ───

    async def _ollama_call(self, prompt: str, json_mode: bool = False) -> Any:
        """Call Ollama local inference API."""
        import httpx

        base_url = self._config.get("ollama", {}).get("base_url", "http://localhost:11434")
        model = self._config.get("ollama", {}).get("model", "llama3.1")

        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
        if json_mode:
            payload["format"] = "json"

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{base_url}/api/generate", json=payload)
            resp.raise_for_status()
            data = resp.json()
            response_text = data.get("response", "").strip()

            if json_mode:
                return json.loads(response_text)
            return response_text

    # ─── Heuristic Fallback ───

    def _heuristic_score(self, resume_text: str, job_description: str) -> Dict[str, Any]:
        """Keyword-matching fallback when no LLM API key is available."""
        keywords_map = {
            "java": "Java", "spring": "Spring Boot", "kafka": "Kafka",
            "postgresql": "PostgreSQL", "angular": "Angular", "aws": "AWS",
            "gcp": "GCP", "azure": "Azure", "docker": "Docker",
            "kubernetes": "Kubernetes", "react": "React", "node": "Node.js",
            "python": "Python", "microservices": "Microservices",
            "rest": "REST API", "sql": "SQL", "mongodb": "MongoDB",
            "redis": "Redis", "jenkins": "Jenkins", "ci/cd": "CI/CD",
            "git": "Git", "typescript": "TypeScript", "javascript": "JavaScript",
            "html": "HTML", "css": "CSS", "graphql": "GraphQL",
            "terraform": "Terraform", "ansible": "Ansible",
            "linux": "Linux", "agile": "Agile", "scrum": "Scrum",
            "jira": "Jira", "machine learning": "Machine Learning",
            "deep learning": "Deep Learning", "nlp": "NLP",
            "data pipeline": "Data Pipeline", "etl": "ETL",
            "spark": "Apache Spark", "hadoop": "Hadoop",
            "golang": "Go", "rust": "Rust", "c++": "C++",
            "swift": "Swift", "kotlin": "Kotlin", "flutter": "Flutter",
        }

        score = 50
        matched, gaps = [], []
        dl = job_description.lower()
        rl = resume_text.lower()

        for keyword, display_name in keywords_map.items():
            if keyword in dl:
                if keyword in rl:
                    matched.append(display_name)
                    score += 4
                else:
                    gaps.append(display_name)
                    score -= 2

        score = max(10, min(100, score))

        first_name = self._user_config.get("first_name", "the applicant")
        return {
            "score": score,
            "rationale": "Keyword heuristic scoring. Add an LLM API key for AI-powered analysis.",
            "matched_skills": matched,
            "missing_skills": gaps,
            "outreach_note": (
                f"Hi, I'm {first_name} and I'm excited about this opportunity. "
                f"I bring experience with {', '.join(matched[:3]) if matched else 'relevant technologies'} "
                f"and I'm eager to contribute to your team."
            ),
        }
