"""
LLM Answer Cache for Apply-Nav.

Caches screening question answers to avoid repeated Gemini API calls
for identical or near-identical questions like "Years of Java experience?",
"Notice period?", "Expected CTC?", etc.

Persistence: JSON file at data/answer_cache.json
Key strategy: MD5 of normalized question text (lowercase, strip punctuation/whitespace)
"""

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("apply_nav.cache")

DATA_DIR = Path(__file__).parent / "data"
CACHE_FILE = DATA_DIR / "answer_cache.json"


class AnswerCache:
    """Persistent cache for LLM-generated screening question answers.
    
    Questions are normalized before hashing so that trivial variations
    (whitespace, punctuation, casing) map to the same cache key.
    
    Example:
        "Years of Java experience?"
        "years of java experience"
        "Years of Java Experience ?"
        → all hash to the same key
    """

    def __init__(self, cache_path: Optional[Path] = None):
        self._path = cache_path or CACHE_FILE
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._hits = 0
        self._misses = 0
        self._load()

    def _load(self) -> None:
        """Load cache from disk."""
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
                logger.info("Answer cache loaded: %d entries from %s", len(self._cache), self._path.name)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("Failed to load answer cache: %s — starting fresh", e)
                self._cache = {}
        else:
            logger.info("No answer cache file found — starting fresh")

    def _save(self) -> None:
        """Persist cache to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2, ensure_ascii=False)
        except IOError as e:
            logger.error("Failed to save answer cache: %s", e)

    @staticmethod
    def _normalize(question: str) -> str:
        """Normalize question text for consistent hashing.
        
        Strips punctuation, collapses whitespace, lowercases.
        "Years of Java experience?" → "years of java experience"
        """
        text = question.lower().strip()
        text = re.sub(r"[^\w\s]", "", text)  # Remove punctuation
        text = re.sub(r"\s+", " ", text)      # Collapse whitespace
        return text.strip()

    @staticmethod
    def _hash(normalized_text: str) -> str:
        """Generate MD5 hash of normalized question text."""
        return hashlib.md5(normalized_text.encode("utf-8")).hexdigest()

    def get(self, question: str) -> Optional[str]:
        """Look up a cached answer for a question.
        
        Args:
            question: The screening question text (will be normalized)
            
        Returns:
            The cached answer string, or None if not in cache
        """
        key = self._hash(self._normalize(question))
        entry = self._cache.get(key)
        if entry:
            self._hits += 1
            logger.debug("Cache HIT for '%s' → '%s'", question[:50], entry["answer"][:50])
            return entry["answer"]
        self._misses += 1
        logger.debug("Cache MISS for '%s'", question[:50])
        return None

    def set(self, question: str, answer: str, field_type: str = "text") -> None:
        """Cache an answer for a question.
        
        Args:
            question: The original question text
            answer: The answer to cache
            field_type: The form field type (text, radio, select, etc.)
        """
        if not answer or not question:
            return

        normalized = self._normalize(question)
        key = self._hash(normalized)
        self._cache[key] = {
            "question": question.strip(),
            "normalized": normalized,
            "answer": answer,
            "field_type": field_type,
            "cached_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._save()
        logger.info("Cached answer for '%s' → '%s'", question[:50], answer[:50])

    def stats(self) -> Dict[str, Any]:
        """Return cache statistics."""
        total_lookups = self._hits + self._misses
        return {
            "total_entries": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total_lookups * 100, 1) if total_lookups > 0 else 0,
            "total_lookups": total_lookups,
        }

    def clear(self) -> None:
        """Clear all cached answers."""
        self._cache = {}
        self._hits = 0
        self._misses = 0
        self._save()
        logger.info("Answer cache cleared")

    def entries(self) -> list:
        """Return all cache entries for inspection."""
        return [
            {
                "question": v["question"],
                "answer": v["answer"],
                "field_type": v.get("field_type", "text"),
                "cached_at": v.get("cached_at", ""),
            }
            for v in self._cache.values()
        ]
