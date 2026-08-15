"""Scheduled evidence monitor for actionable, reviewable insight alerts."""

import hashlib
from datetime import datetime
from typing import Any, Dict

from app.core.database import get_session_local
from app.models.insight_alert import InsightAlert
from app.services.kg.event_discovery import EventDiscoveryService


def _fingerprint(event: Dict[str, Any]) -> str:
    article_ids = sorted(
        str(item.get("id"))
        for item in event.get("evidence_articles", [])
        if item.get("id") and item.get("article_role") != "syndication"
    )
    return hashlib.sha256("\x1f".join(article_ids).encode("utf-8")).hexdigest()


def _severity(event: Dict[str, Any]) -> str:
    evidence_count = sum(
        1 for item in event.get("evidence_articles") or []
        if item.get("article_role") != "syndication"
    )
    confidence = float(event.get("confidence") or 0)
    risk_score = int((event.get("risk_assessment") or {}).get("severity") or 0)
    if risk_score >= 80 and evidence_count >= 2:
        return "high"
    if risk_score >= 55:
        return "medium"
    negative_terms = {"下降", "风险", "暂停", "争议"}
    reasons = set(event.get("signal_reasons") or [])
    if evidence_count >= 3 and confidence >= 0.75:
        return "high"
    if evidence_count >= 2 or reasons & negative_terms:
        return "medium"
    return "low"


def scan_insight_alerts(limit: int = 100, days: int = 90) -> Dict[str, int]:
    """Upsert only multi-source candidates; do not invoke an LLM or auto-predict."""
    session = get_session_local()()
    created = updated = 0
    try:
        events = EventDiscoveryService().discover(session, limit=limit, days=days)
        now = datetime.utcnow()
        for event in events:
            evidence = [
                item for item in event.get("evidence_articles") or []
                if item.get("article_role") != "syndication"
            ]
            if int(event.get("independent_source_count") or 0) < 2:
                continue
            fingerprint = _fingerprint(event)
            alert = session.query(InsightAlert).filter(InsightAlert.fingerprint == fingerprint).first()
            payload = {
                "event_id": event["id"],
                "topic": event.get("topic") or event.get("title") or "未命名事件",
                "title": event.get("title") or "未命名事件",
                "severity": _severity(event),
                "confidence": float(event.get("confidence") or 0),
                "independent_source_count": int(event.get("independent_source_count") or 0),
                "evidence_article_ids": [str(item["id"]) for item in evidence if item.get("id")],
                "signal_reasons": (
                    (event.get("signal_reasons") or [])
                    + [
                        f"风险类别：{item}"
                        for item in (event.get("risk_assessment") or {}).get("categories", [])
                    ]
                ),
                "risk_assessment": event.get("risk_assessment") or {},
                "match_evidence": event.get("match_evidence") or {},
                "last_seen_at": now,
            }
            if alert is None:
                alert = InsightAlert(
                    id=f"alert-{hashlib.sha256(fingerprint.encode()).hexdigest()[:32]}",
                    fingerprint=fingerprint,
                    **payload,
                )
                session.add(alert)
                created += 1
            else:
                for field, value in payload.items():
                    setattr(alert, field, value)
                updated += 1
        session.commit()
        return {"created": created, "updated": updated, "scanned": len(events)}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
