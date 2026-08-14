"""从已有文章和知识点中发现值得关注的候选事件。"""

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

from sqlalchemy.orm import Session, selectinload

from app.models.article import Article, ArticleKeyword


class EventDiscoveryService:
    """基于近期文档信号生成候选事件，不要求用户预先输入主题。"""

    EVENT_MARKERS = (
        "发布", "获批", "突破", "完成", "签约", "启动", "建成", "上线",
        "增长", "下降", "投资", "计划", "试验", "发现", "合作", "入选",
    )

    # Two documents must share a meaningful topic signal and be close enough in
    # time to be treated as evidence for one developing event.  These are kept
    # deliberately conservative: a false cross-document event is worse than a
    # single-document item that needs more evidence.
    CLUSTER_MIN_SHARED = 2
    CLUSTER_SIMILARITY = 0.18
    MAX_EVENT_GAP_DAYS = 60

    # 低区分度词（大量出现在政府/季度报告中，无主题区分能力），token 化时排除
    _STOPWORDS = frozenset([
        "相关", "运行", "情况", "工作", "建设", "推进", "落实", "组织",
        "管理", "服务", "支持", "加强", "提高", "促进", "实现", "完成", "继续",
        "保持", "进一步", "主要", "重要", "全面", "深入", "持续", "有效", "积极",
        "月份", "年度", "季度", "初期", "末期", "同期",
        "印发", "转发", "联合", "按照", "根据", "通过", "依法", "规范", "标准",
        "互联网", "信息化", "数字化", "智能化", "网络", "平台", "系统",
        "会议", "座谈", "召开", "举行", "参加", "出席", "讲话", "发言", "总结",
        "交流", "听取", "汇报", "部署",
        "单位", "部门", "机构", "企业", "行业",
        "发展", "研究", "社会", "国家",
        "20", "24", "23", "22", "21", "25", "26",
    ])

    @staticmethod
    def _canonical_url(url: str) -> str:
        parsed = urlparse(url or "")
        if parsed.netloc.lower().endswith("thepaper.cn") and re.search(
            r"/newsDetail_forward_\d+", parsed.path, re.IGNORECASE
        ):
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
        return url

    @staticmethod
    def _tokens(text: str) -> set[str]:
        words = set(re.findall(r"[A-Za-z0-9+#.-]{2,}|[一-鿿]{2}", text or ""))
        compact = re.sub(r"[^一-鿿]", "", text or "")
        words.update(compact[i:i + 2] for i in range(max(0, len(compact) - 1)))
        return {word for word in words
                if len(word) >= 2 and word not in EventDiscoveryService._STOPWORDS}

    @staticmethod
    def _article_date(article: Article) -> datetime:
        """Use publication time when available; scraping time is a fallback."""
        if article.published_at:
            return datetime.combine(article.published_at, datetime.min.time())
        return article.scraped_at or article.created_at or datetime.utcnow()

    @classmethod
    def _profile(cls, article: Article) -> Dict[str, Any]:
        """Build a bounded, explainable feature profile for event matching."""
        title = article.title or ""
        summary = article.summary or ""
        # Full text matters, but only a bounded lead section is used here.  It
        # prevents one long article from dominating matching or API latency.
        body = (article.content or "")[:6000]
        keyword_tokens = {
            str(link.keyword.name).strip()
            for link in (article.keywords or [])
            if link.keyword and str(link.keyword.name).strip()
        }
        return {
            "title": cls._tokens(title),
            "summary": cls._tokens(summary),
            "body": cls._tokens(body),
            "keywords": keyword_tokens,
            "date": cls._article_date(article),
        }

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

    @classmethod
    def _is_duplicate(cls, left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        """Collapse near copies, while retaining same-source periodic updates."""
        title_score = cls._similarity(left["title"], right["title"])
        summary_score = cls._similarity(left["summary"], right["summary"])
        body_score = cls._similarity(left["body"], right["body"])
        return summary_score >= 0.88 or (
            body_score >= 0.82 and (title_score >= 0.65 or summary_score >= 0.65)
        )

    @classmethod
    def _match_evidence(cls, left: Dict[str, Any], right: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return match evidence only when two documents support one event."""
        gap_days = abs((left["date"] - right["date"]).days)
        if gap_days > cls.MAX_EVENT_GAP_DAYS:
            return None

        shared_title = left["title"] & right["title"]
        shared_summary = left["summary"] & right["summary"]
        shared_keywords = left["keywords"] & right["keywords"]
        shared_body = left["body"] & right["body"]
        title_score = cls._similarity(left["title"], right["title"])
        summary_score = cls._similarity(left["summary"], right["summary"])
        body_score = cls._similarity(left["body"], right["body"])
        keyword_score = cls._similarity(left["keywords"], right["keywords"])
        # The generated summary is the best bounded representation of the full
        # article.  Body overlap remains a weak supporting signal so boilerplate
        # in long pages cannot merge unrelated events by itself.
        score = round(
            0.40 * title_score
            + 0.35 * summary_score
            + 0.10 * body_score
            + 0.15 * keyword_score,
            4,
        )

        # A keyword/entity match can anchor a paraphrased report.  Without it,
        # require at least two specific title phrases; body-only overlap is too
        # noisy for Chinese news prose.
        has_topic_anchor = bool(shared_keywords) or len(shared_title) >= cls.CLUSTER_MIN_SHARED
        if not has_topic_anchor or score < cls.CLUSTER_SIMILARITY:
            return None
        return {
            "score": score,
            "gap_days": gap_days,
            "shared_title_terms": sorted(shared_title)[:6],
            "shared_summary_terms": sorted(shared_summary)[:8],
            "shared_keywords": sorted(shared_keywords)[:6],
            "shared_body_terms": sorted(shared_body)[:6],
        }

    def discover(self, db: Session, limit: int = 20, days: int = 90) -> List[Dict[str, Any]]:
        cutoff = datetime.utcnow() - timedelta(days=max(1, min(days, 3650)))
        articles = db.query(Article).options(
            selectinload(Article.keywords).selectinload(ArticleKeyword.keyword)
        ).filter(
            Article.status.in_(["completed", "success"]),
            Article.scraped_at >= cutoff,
        ).order_by(Article.scraped_at.desc()).limit(300).all()
        if not articles:
            articles = db.query(Article).options(
                selectinload(Article.keywords).selectinload(ArticleKeyword.keyword)
            ).filter(
                Article.status.in_(["completed", "success"]),
            ).order_by(Article.scraped_at.desc()).limit(100).all()

        # canonical URL 去重
        unique_articles = []
        seen_urls: Dict[str, Article] = {}
        canonical_duplicate_counts: Dict[str, int] = {}
        for article in articles:
            canonical_url = self._canonical_url(article.url)
            if canonical_url and canonical_url in seen_urls:
                kept = seen_urls[canonical_url]
                canonical_duplicate_counts[kept.id] = canonical_duplicate_counts.get(kept.id, 0) + 1
                continue
            if canonical_url:
                seen_urls[canonical_url] = article
            unique_articles.append(article)
        articles = unique_articles

        profiles = {article.id: self._profile(article) for article in articles}

        # ---------- 按内容相似度贪心聚类 ----------
        # 内容基本相同的命题文章聚为一组，每组只形成一个交叉事件，
        # 避免"标题略异但讲同一件事"的文章各自生成候选事件（重复命题）。
        groups: List[List[Article]] = []
        assigned: set[str] = set()
        for article in sorted(articles, key=lambda a: a.scraped_at or datetime.min, reverse=True):
            if article.id in assigned:
                continue
            group = [article]
            assigned.add(article.id)
            for other in articles:
                if other.id in assigned:
                    continue
                # Compare against every item in the group, rather than just the
                # seed.  This admits paraphrases while retaining a direct,
                # inspectable evidence path for each addition.
                if any(self._match_evidence(profiles[item.id], profiles[other.id]) for item in group):
                    group.append(other)
                    assigned.add(other.id)
            groups.append(group)

        candidates = []
        for group in groups:
            if not group:
                continue
            # Only collapse actual near copies.  Periodic reports from the same
            # publisher are retained as a time series, but are not counted as
            # independent-source corroboration.
            deduped: List[Article] = []
            duplicate_count = sum(canonical_duplicate_counts.get(article.id, 0) for article in group)
            for article in sorted(group, key=lambda item: self._article_date(item), reverse=True):
                if any(self._is_duplicate(profiles[article.id], profiles[kept.id]) for kept in deduped):
                    duplicate_count += 1
                    continue
                deduped.append(article)
            independent_sources = {
                self._source_name(article.url) or f"article:{article.id}"
                for article in deduped
            }

            # 选来源去重后覆盖文章数最多的命题作为代表，只形成一个交叉事件
            representative = max(
                deduped,
                key=lambda a: sum(
                    1 if self._match_evidence(profiles[a.id], profiles[o.id]) else 0
                    for o in deduped
                ),
            )
            title = (representative.title or "").strip()
            if len(title) < 2 or not title:
                continue
            marker_text = f"{title} {(representative.summary or '')} {(representative.content or '')[:1200]}"
            marker_hits = [marker for marker in self.EVENT_MARKERS if marker in marker_text]
            evidence_articles = sorted(deduped, key=lambda a: a.scraped_at or datetime.min, reverse=True)
            pair_matches = [
                self._match_evidence(profiles[representative.id], profiles[item.id])
                for item in evidence_articles
                if item.id != representative.id
            ]
            pair_matches = [item for item in pair_matches if item]
            average_match = (
                sum(item["score"] for item in pair_matches) / len(pair_matches)
                if pair_matches else 0.0
            )
            shared_keywords = sorted({
                term for item in pair_matches for term in item["shared_keywords"]
            })[:8]
            shared_title_terms = sorted({
                term for item in pair_matches for term in item["shared_title_terms"]
            })[:8]
            # 置信度衡量材料覆盖，不是发生概率。独立来源比同源时序文档权重更高。
            signal = (
                0.35
                + min(0.25, len(marker_hits) * 0.08)
                + min(0.18, len(evidence_articles) * 0.05)
                + min(0.16, len(independent_sources) * 0.06)
                + min(0.15, average_match * 0.5)
            )
            age_days = max(0, (datetime.utcnow() - (representative.scraped_at or datetime.utcnow())).days)
            signal += max(0, 0.25 - age_days / 365)
            candidate_id = "event-" + hashlib.sha256(representative.id.encode("utf-8")).hexdigest()[:24]
            candidates.append({
                "id": candidate_id,
                "title": title,
                "topic": title,
                "confidence": round(min(signal, 0.95), 4),
                "signal_type": (
                    "cross_document" if len(independent_sources) > 1
                    else "series_trend" if len(evidence_articles) > 1
                    else "event_marker" if marker_hits
                    else "recent_article"
                ),
                "signal_reasons": (
                    (marker_hits or ["近期出现的新文档"])
                    + (
                        [f"{len(independent_sources)} 个独立来源在 {self.MAX_EVENT_GAP_DAYS} 天内交叉印证"]
                        if len(independent_sources) > 1
                        else [f"同一来源的 {len(evidence_articles)} 期相关材料，可用于时序走向分析"]
                        if len(evidence_articles) > 1
                        else ["证据不足：仅1篇文档"]
                    )
                    + ([f"共同关键词：{'、'.join(shared_keywords)}"] if shared_keywords else [])
                    + ([f"共同标题短语：{'、'.join(shared_title_terms)}"] if shared_title_terms else [])
                ),
                "match_evidence": {
                    "average_similarity": round(average_match, 4),
                    "shared_keywords": shared_keywords,
                    "shared_title_terms": shared_title_terms,
                    "max_time_gap_days": max((item["gap_days"] for item in pair_matches), default=0),
                },
                "independent_source_count": len(independent_sources),
                "duplicate_count": duplicate_count,
                "evidence_articles": [{
                    "id": source.id,
                    "title": source.title,
                    "summary": (source.summary or source.content or "")[:300],
                    "published_at": source.published_at.isoformat() if source.published_at else None,
                    "scraped_at": source.scraped_at.isoformat() if source.scraped_at else None,
                    "url": source.url,
                    "source_domain": self._source_name(source.url),
                } for source in evidence_articles],
                "discovered_at": datetime.utcnow().isoformat(),
            })

        candidates.sort(key=lambda item: (item["confidence"], item["evidence_articles"][0]["scraped_at"] or ""), reverse=True)
        return candidates[:max(1, min(limit, 100))]
