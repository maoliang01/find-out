"""Small, auditable first-pass risk detector.

Rules identify candidate risk evidence; they do not assert that an allegation is
true.  Human review and multi-source corroboration remain required for alerts.
"""

from __future__ import annotations

import re
from typing import Any

RISK_RULES = {
    "safety_incident": ("事故", "爆炸", "坍塌", "伤亡", "火灾", "泄漏"),
    "regulatory": ("立案", "处罚", "罚款", "警示函", "监管措施", "调查"),
    "delivery": ("延期", "停工", "停产", "违约", "交付承压", "进度滞后"),
    "reputation": ("投诉", "举报", "质疑", "争议", "造假", "欺诈"),
    "data_security": ("泄密", "数据泄露", "信息泄露", "网络攻击", "勒索"),
}

SEVERITY = {"safety_incident": 90, "data_security": 80, "regulatory": 70, "delivery": 55, "reputation": 45}


def detect_risk(text: str) -> dict[str, Any]:
    text = text or ""
    matches: list[dict[str, str]] = []
    for category, terms in RISK_RULES.items():
        for term in terms:
            match = re.search(re.escape(term), text, re.IGNORECASE)
            if not match:
                continue
            start = max(0, match.start() - 40)
            end = min(len(text), match.end() + 80)
            matches.append({"category": category, "term": term, "evidence": text[start:end].strip()})
            break
    categories = sorted({item["category"] for item in matches})
    severity = max((SEVERITY[item] for item in categories), default=0)
    return {"categories": categories, "severity": severity, "evidence": matches}
