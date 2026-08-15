"""One-shot: rewrite EventDiscoveryService.discover() to cluster same-proposition
articles into one cross-event, dedup by source, pick the representative with the
most evidence articles.  Numbers below are copied from the original file.
"""
from pathlib import Path

TARGET = Path(r"G:\ClaudeCode\ai-studio\backend\app\services\kg\event_discovery.py")

SIM_THRESHOLD = 0.16
MIN_SHARED = 2
MAX_EVIDENCE = 7
DEFAULT_LIMIT = 20
DEFAULT_DAYS = 90
QUERY_MAX = 300
FALLBACK_MAX = 100
CUTOFF_DAYS = 3650
BASE_SIGNAL = 0.35
MARKER_WEIGHT = 0.08
MARKER_CAP = 0.25
EVIDENCE_WEIGHT = 0.08
EVIDENCE_CAP = 0.3
AGE_WEIGHT = 0.25
AGE_DIVISOR = 365
CONF_CAP = 0.95
MAX_RETURN = 100

NEW_CONTENT = f'''"""从已有文章和知识点中发现值得关注的候选事件。"""

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

from sqlalchemy.orm import Session

from app.models.article import Article


class EventDiscoveryService:
    """基于近期文档信号生成候选事件，不要求用户预先输入主题。"""

    EVENT_MARKERS = (
        "发布", "获批", "突破", "完成", "签约", "启动", "建成", "上线",
        "增长", "下降", "投资", "计划", "试验", "发现", "合作", "入选",
    )

    # 判定两篇文章属于同一命题的阈值
    CLUSTER_MIN_SHARED = {MIN_SHARED}
    CLUSTER_SIMILARITY = {SIM_THRESHOLD}

    @staticmethod
    def _canonical_url(url: str) -> str:
        parsed = urlparse(url or "")
        if parsed.netloc.lower().endswith("thepaper.cn") and re.search(
            r"/newsDetail_forward_\\d+", parsed.path, re.IGNORECASE
        ):
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
        return url

    @staticmethod
    def _tokens(text: str) -> set[str]:
        words = set(re.findall(r"[A-Za-z0-9+#.-]{{2,}}|[一-鿿]{{2}}", text or ""))
        compact = re.sub(r"[^一-鿿]", "", text or "")
        words.update(compact[i:i + {MIN_SHARED}] for i in range(max(0, len(compact) - 1)))
        return {{word for word in words if len(word) >= {MIN_SHARED}}}

    @staticmethod
    def _source_name(url: str) -> str:
        """从 URL 提取来源域名（不含端口与 www 前缀），用于相同来源去重。"""
        parsed = urlparse(url or "")
        return (parsed.netloc or "").lower().removeprefix("www.")

    @staticmethod
    def _similarity(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        union = a | b
        return len(a & b) / len(union) if union else 0.0

    @staticmethod
    def _is_same_proposition(a: set[str], b: set[str]) -> bool:
        """两篇文章是否属于同一命题。"""
        if not a or not b:
            return False
        shared = len(a & b)
        union = a | b
        if not union:
            return False
        return shared >= {MIN_SHARED} and (shared / len(union)) >= {SIM_THRESHOLD}

    def discover(self, db: Session, limit: int = {DEFAULT_LIMIT}, days: int = {DEFAULT_DAYS}) -> List[Dict[str, Any]]:
        cutoff = datetime.utcnow() - timedelta(days=max(1, min(days, {CUTOFF_DAYS})))
        articles = db.query(Article).filter(
            Article.status.in_(["completed", "success"]),
            Article.scraped_at >= cutoff,
        ).order_by(Article.scraped_at.desc()).limit({QUERY_MAX}).all()
        if not articles:
            articles = db.query(Article).filter(
                Article.status.in_(["completed", "success"]),
            ).order_by(Article.scraped_at.desc()).limit({FALLBACK_MAX}).all()

        # canonical URL 去重
        unique_articles = []
        seen_urls = set()
        for article in articles:
            canonical_url = self._canonical_url(article.url)
            if canonical_url in seen_urls:
                continue
            seen_urls.add(canonical_url)
            unique_articles.append(article)
        articles = unique_articles

        # 预计算每篇文章的 token 集合
        coarse_tokens = {{article.id: self._tokens(f"{{article.title}} {{article.summary}}") for article in articles}}

        # ---------- 按内容相似度贪心聚类 ----------
        # 内容基本相同的命题文章聚为一组，每组只形成一个交叉事件，
        # 避免"标题略异但讲同一件事"的文章各自生成候选事件（重复命题）。
        groups: List[List[Article]] = []
        assigned: set[str] = set()
        for article in sorted(articles, key=lambda a: a.scraped_at or datetime.min, reverse=True):
            if article.id in assigned:
                continue
            group_tokens = coarse_tokens[article.id]
            group = [article]
            assigned.add(article.id)
            for other in articles:
                if other.id in assigned:
                    continue
                other_tokens = coarse_tokens[other.id]
                if EventDiscoveryService._is_same_proposition(group_tokens, other_tokens):
                    group.append(other)
                    assigned.add(other.id)
            groups.append(group)

        candidates = []
        for group in groups:
            if not group:
                continue
            # 相同来源只保留一篇（与代表重叠最多者），避免相同来源重复命题
            source_dedup: Dict[str, Article] = {{}}
            for article in group:
                source = EventDiscoveryService._source_name(article.url)
                if source in source_dedup:
                    keep = source_dedup[source]
                    base_tokens = coarse_tokens[group[0].id]
                    keep_tokens = coarse_tokens[keep.id]
                    cand_tokens = coarse_tokens[article.id]
                    if EventDiscoveryService._similarity(base_tokens, cand_tokens) > EventDiscoveryService._similarity(base_tokens, keep_tokens):
                        source_dedup[source] = article
                    continue
                source_dedup[source] = article
            deduped = list(source_dedup.values())

            # 选来源去重后覆盖文章数最多的命题作为代表，只形成一个交叉事件
            representative = max(
                deduped,
                key=lambda a: sum(
                    1 if EventDiscoveryService._is_same_proposition(coarse_tokens[a.id], coarse_tokens[o.id]) else 0
                    for o in deduped
                ),
            )
            title = (representative.title or "").strip()
            if len(title) < {MIN_SHARED} or not title:
                continue
            marker_hits = [marker for marker in self.EVENT_MARKERS if marker in title]
            evidence_articles = sorted(deduped, key=lambda a: a.scraped_at or datetime.min, reverse=True)
            # 置信度：与命题簇内证据篇数、来源数、事件标记相关
            signal = (
                {BASE_SIGNAL}
                + min({MARKER_CAP}, len(marker_hits) * {MARKER_WEIGHT})
                + min({EVIDENCE_CAP}, len(evidence_articles) * {EVIDENCE_WEIGHT})
            )
            age_days = max(0, (datetime.utcnow() - (representative.scraped_at or datetime.utcnow())).days)
            signal += max(0, {AGE_WEIGHT} - age_days / {AGE_DIVISOR})
            candidate_id = "event-" + hashlib.sha256(representative.id.encode("utf-8")).hexdigest()[:24]
            candidates.append({{
                "id": candidate_id,
                "title": title,
                "topic": title,
                "confidence": round(min(signal, {CONF_CAP}), 4),
                "signal_type": "cross_document" if len(evidence_articles) > 1 else ("event_marker" if marker_hits else "recent_article"),
                "signal_reasons": (marker_hits or ["近期出现的新文档"]) + ([f"{{len(evidence_articles)}}篇相关文档交叉印证"] if len(evidence_articles) > 1 else ["证据不足：仅1篇文档"]),
                "evidence_articles": [{{
                    "id": source.id,
                    "title": source.title,
                    "summary": (source.summary or source.content or "")[:300],
                    "published_at": source.published_at.isoformat() if source.published_at else None,
                    "scraped_at": source.scraped_at.isoformat() if source.scraped_at else None,
                    "url": source.url,
                }} for source in evidence_articles],
                "discovered_at": datetime.utcnow().isoformat(),
            }})

        candidates.sort(key=lambda item: (item["confidence"], item["evidence_articles"][0]["scraped_at"] or ""), reverse=True)
        return candidates[:max(1, min(limit, {MAX_RETURN}))]
'''

TARGET.write_text(NEW_CONTENT, encoding="utf-8")
print("written:", TARGET)