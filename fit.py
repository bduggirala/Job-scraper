"""Explainable fit scoring against a configurable skill list.

The number is the least useful part. A bare score tells you nothing about
*why* a posting ranked where it did, so :class:`FitResult` carries the skills
that matched, the ones looked for and absent, and a sentence naming them. Rank
on ``score`` if you like; act on ``matched``.

Two deliberate refusals:

* **A job with no description is unscored, not zero.** Several collectors
  (Cornerstone, Radancy, Jobvite, SmartRecruiters) return no description at
  all, and scoring those as 0 would rank them below genuinely poor matches
  while looking like a judgement. ``score`` is None and the explanation says so.
* **Frequency is capped.** A posting repeating "Python" thirty times is not
  thirty times a better match, so each skill counts once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from settings import Settings, load_settings

#: Default skills, from the brief. Overridable via ``fit.skills`` in settings.
#: Each entry is ``name: [regex alternatives]``; the name is what gets
#: reported, the patterns are what get searched.
DEFAULT_SKILLS: dict[str, list[str]] = {
    "python": [r"\bpython\b"],
    "sql": [r"\bsql\b"],
    "pyspark": [r"\bpy-?spark\b"],
    "spark": [r"\bspark\b"],
    "snowflake": [r"\bsnowflake\b"],
    "databricks": [r"\bdatabricks\b"],
    "aws": [r"\baws\b", r"\bamazon web services\b"],
    "azure": [r"\bazure\b"],
    "gcp": [r"\bgcp\b", r"\bgoogle cloud\b"],
    "kafka": [r"\bkafka\b"],
    "etl": [r"\betl\b", r"\belt\b"],
    "airflow": [r"\bairflow\b"],
    "dbt": [r"\bdbt\b"],
}


@dataclass
class FitResult:
    """A score with its reasoning attached."""

    score: int | None
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    explanation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "fit_score": self.score,
            "fit_matched": ", ".join(self.matched),
            "fit_explanation": self.explanation,
        }


def _skills(settings: Settings | None = None) -> dict[str, list[str]]:
    cfg = settings or load_settings()
    configured = cfg.get("fit.skills")
    if isinstance(configured, dict) and configured:
        return {
            str(name): [str(p) for p in (patterns or [])] if isinstance(patterns, list)
            else [rf"\b{re.escape(str(patterns))}\b"]
            for name, patterns in configured.items()
        }
    return DEFAULT_SKILLS


def score_fit(job: dict[str, Any], settings: Settings | None = None) -> FitResult:
    """Score one posting against the configured skills.

    The title is searched alongside the description: "Snowflake Engineer" is a
    Snowflake match however thin its body text is.
    """
    description = (job.get("description") or "").strip()
    title = (job.get("title") or "").strip()

    if not description:
        return FitResult(
            score=None,
            explanation="no description available",
        )

    haystack = f"{title}\n{description}".lower()
    skills = _skills(settings)

    matched = [
        name for name, patterns in skills.items()
        if any(re.search(p, haystack, re.I) for p in patterns)
    ]
    missing = [name for name in skills if name not in matched]

    # Each skill counts once, so repetition cannot inflate a score.
    score = round(100 * len(matched) / max(1, len(skills)))

    if matched:
        explanation = f"matched {', '.join(matched)}"
        if missing:
            explanation += f"; no mention of {', '.join(missing[:4])}"
    else:
        explanation = "no configured skills mentioned"

    return FitResult(score=score, matched=matched, missing=missing,
                     explanation=explanation)
