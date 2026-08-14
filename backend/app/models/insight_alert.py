"""Persisted, evidence-first alerts produced by the insight monitor."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import DateTime, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InsightAlert(Base):
    """A reviewable alert, not a claim that an outcome will occur."""

    __tablename__ = "insight_alerts"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    event_id: Mapped[str] = mapped_column(String(80), index=True)
    topic: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(500))
    severity: Mapped[str] = mapped_column(String(20), default="medium", index=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    evidence_article_ids: Mapped[List[str]] = mapped_column(JSON, default=list)
    signal_reasons: Mapped[List[str]] = mapped_column(JSON, default=list)
    match_evidence: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_insight_alerts_status_seen", "status", "last_seen_at"),
    )
