"""Explainable article-level signals used before event aggregation.

Duplicate reports are kept as provenance, but never treated as independent event
evidence.  The implementation deliberately uses deterministic fingerprints so it
works with the project's current SQLite deployment and does not require an LLM.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Iterable, Optional

from app.models.article import Article


def normalize_for_fingerprint(text: str) -> str:
    """Remove presentation noise while preserving the article's lexical content."""
    text = re.sub(r"^摘要[:：].*?(?:\n|$)", "", text or "", flags=re.MULTILINE)
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).casefold()


def content_fingerprint(title: str, content: str) -> str:
    normalized = normalize_for_fingerprint(f"{title or ''}\n{content or ''}")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _similarity(left: Article, right: Article) -> float:
    """High-threshold lexical comparison for syndicated or lightly edited copies."""
    left_text = normalize_for_fingerprint(f"{left.title}\n{left.content}")[:6000]
    right_text = normalize_for_fingerprint(f"{right.title}\n{right.content}")[:6000]
    if not left_text or not right_text:
        return 0.0
    length_ratio = min(len(left_text), len(right_text)) / max(len(left_text), len(right_text))
    if length_ratio < 0.82:
        return 0.0
    # Most unrelated reports can be rejected using cheap character trigrams.
    # Full SequenceMatcher comparisons are comparatively expensive during a
    # historical backfill and are reserved for plausible copies only.
    left_grams = {left_text[index:index + 3] for index in range(0, min(len(left_text), 2400) - 2, 3)}
    right_grams = {right_text[index:index + 3] for index in range(0, min(len(right_text), 2400) - 2, 3)}
    overlap = len(left_grams & right_grams) / max(1, len(left_grams | right_grams))
    if overlap < 0.65:
        return 0.0
    return SequenceMatcher(None, left_text, right_text, autojunk=False).ratio()


def _earliest(articles: Iterable[Article]) -> Article:
    return min(articles, key=lambda item: item.published_at or item.scraped_at or datetime.max)


def classify_article_provenance(
    db,
    article: Article,
    *,
    lookback_days: int = 30,
    copy_threshold: float = 0.93,
) -> str:
    """Assign a durable duplicate group and an evidence role to one article.

    ``original`` and ``update`` are independent evidence. ``syndication`` is a
    propagation record only.  The comparison is bounded to recent articles so
    insertion stays cheap as the corpus grows.
    """
    fingerprint = content_fingerprint(article.title or "", article.content or "")
    article.content_fingerprint = fingerprint
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    candidates = (
        db.query(Article)
        .filter(Article.id != article.id, Article.scraped_at >= cutoff)
        .order_by(Article.scraped_at.desc())
        .limit(100)
        .all()
    )

    exact = [item for item in candidates if item.content_fingerprint == fingerprint]
    near = exact or [item for item in candidates if _similarity(article, item) >= copy_threshold]
    if not near:
        article.duplicate_group_id = f"dup-{fingerprint[:24]}"
        article.article_role = "original"
        return article.article_role

    group_articles = near + [article]
    primary = _earliest(group_articles)
    group_id = primary.duplicate_group_id or f"dup-{content_fingerprint(primary.title or '', primary.content or '')[:24]}"
    for item in group_articles:
        item.duplicate_group_id = group_id
        item.article_role = "original" if item.id == primary.id else "syndication"
    return article.article_role


def backfill_recent_provenance(db, *, limit: int = 500) -> int:
    """Idempotently classify recent stored articles after the feature is enabled."""
    articles = (
        db.query(Article)
        .filter(Article.status == "success")
        .order_by(Article.scraped_at.desc())
        .limit(max(1, min(limit, 2000)))
        .all()
    )
    # Avoid an implicit flush before every candidate query.  The backfill is
    # intentionally bounded and commits once at the caller, which keeps its
    # cost linear enough for a live SQLite database.
    with db.no_autoflush:
        for article in reversed(articles):
            classify_article_provenance(db, article)
    return len(articles)
