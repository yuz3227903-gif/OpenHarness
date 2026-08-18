"""Deterministic, conservative source-quality classification."""

from __future__ import annotations

from urllib.parse import urlparse


_GRADE_SCORE = {"A": 4, "B": 3, "C": 2, "D": 1}

_GRADE_A_DOMAINS = (
    "cninfo.com.cn",
    "sse.com.cn",
    "szse.cn",
    "hkexnews.hk",
    "sec.gov",
    "gov.cn",
    "stats.gov.cn",
)

_GRADE_B_DOMAINS = (
    "iea.org",
    "worldbank.org",
    "imf.org",
    "oecd.org",
    "miit.gov.cn",
)


def classify_source_grade(
    url_or_file: str,
    *,
    title: str = "",
    publisher: str | None = None,
    declared_grade: str = "C",
) -> str:
    """Return the strongest grade supported by deterministic domain rules.

    Unknown web sources retain their declared grade.  The classifier therefore
    improves known official sources without pretending that an unfamiliar
    domain is authoritative.
    """

    current = declared_grade if declared_grade in _GRADE_SCORE else "C"
    hostname = (urlparse(url_or_file).hostname or "").lower().strip(".")
    inferred = "C"
    if _matches_domain(hostname, _GRADE_A_DOMAINS):
        inferred = "A"
    elif _matches_domain(hostname, _GRADE_B_DOMAINS):
        inferred = "B"
    elif not hostname and not str(url_or_file).lower().startswith(("http://", "https://")):
        inferred = "B" if _looks_like_formal_filing(title, publisher) else current
    return inferred if _GRADE_SCORE[inferred] > _GRADE_SCORE[current] else current


def _matches_domain(hostname: str, domains: tuple[str, ...]) -> bool:
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains)


def _looks_like_formal_filing(title: str, publisher: str | None) -> bool:
    text = f"{title} {publisher or ''}".lower()
    return any(
        marker in text
        for marker in (
            "年度报告",
            "半年度报告",
            "季度报告",
            "annual report",
            "interim report",
            "公司公告",
        )
    )


__all__ = ["classify_source_grade"]
