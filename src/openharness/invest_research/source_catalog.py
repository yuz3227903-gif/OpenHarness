"""Compact shared source catalog passed to parallel research Agents."""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urlparse

from openharness.invest_research.evidence_store import EvidenceStore


def build_shared_source_catalog(
    store: EvidenceStore,
    run_id: str,
    *,
    entities: Iterable[str] = (),
    limit: int = 24,
) -> list[dict[str, Any]]:
    """Create a small, quality-ordered directory without source page content."""

    normalized_entities = [
        str(entity).strip() for entity in entities if str(entity).strip()
    ]
    catalog: list[dict[str, Any]] = []
    for row in store.source_catalog(run_id, limit=limit):
        title = str(row.get("title") or "")
        url = str(row.get("url_or_file") or "")
        searchable = f"{title} {url}".casefold()
        related_entity = next(
            (
                entity
                for entity in normalized_entities
                if entity.casefold() in searchable
            ),
            None,
        )
        catalog.append(
            {
                "source_id": row["source_id"],
                "title": title,
                "domain": (urlparse(url).hostname or "").casefold(),
                "published_at": row.get("published_at"),
                "source_grade": row.get("source_grade") or "C",
                "status": row.get("status") or "discovered",
                "related_entity": related_entity,
            }
        )
    return catalog


__all__ = ["build_shared_source_catalog"]
