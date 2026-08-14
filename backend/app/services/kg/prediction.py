"""
趋势预测引擎

基于知识图谱进行趋势预测，包括技术趋势、舆情趋势等。
"""

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass
class PredictionResult:
    """预测结果"""
    topic: str
    trend: str  # up/down/stable
    confidence: float
    factors: List[Dict[str, Any]]
    timeline: List[Dict[str, Any]]
    prediction_type: str
    generated_at: str
    knowledge_basis: Dict[str, Any] = field(default_factory=dict)
    scenarios: List[Dict[str, Any]] = field(default_factory=list)
    interpretation: Dict[str, Any] = field(default_factory=dict)


class TrendPredictionEngine:
    """趋势预测引擎"""

    def __init__(self, kg_service, llm_client=None):
        """
        初始化预测引擎

        Args:
            kg_service: Neo4jService 实例
        """
        self.kg_service = kg_service
        self.llm_client = llm_client

    async def predict_trend(
        self,
        topic: str,
        time_range: int = 30,
        prediction_type: str = 'general'
    ) -> PredictionResult:
        """
        预测趋势

        Args:
            topic: 预测主题
            time_range: 预测天数
            prediction_type: 预测类型 (general/technology/sentiment)

        Returns:
            PredictionResult: 预测结果
        """
        try:
            # 1. 收集历史数据
            historical_data = await self._collect_historical_data(topic)

            # 2. 分析影响因素
            factors = await self._analyze_factors(topic)

            # 3. 生成预测
            prediction = self._generate_prediction(
                historical_data, factors, time_range, prediction_type, topic,
                knowledge_basis=historical_data.get('knowledge_basis', {}),
            )

            return prediction

        except Exception as e:
            logger.error(f"趋势预测失败: {e}")
            return PredictionResult(
                topic=topic,
                trend='stable',
                confidence=0.3,
                factors=[],
                timeline=self._generate_default_timeline(time_range),
                prediction_type=prediction_type,
                generated_at=datetime.now().isoformat()
            )

    async def predict_discovered_event(
        self,
        event: Dict[str, Any],
        time_range: int = 30,
        prediction_type: str = "general",
        model_id: Optional[str] = None,
    ) -> PredictionResult:
        """基于候选事件的多篇证据文档和知识点进行交叉预测。"""
        evidence = event.get("evidence_articles") or []
        article_ids = [item.get("id") for item in evidence if item.get("id")]
        points = []
        relation_count = 0
        relation_details = []
        try:
            rows = await self.kg_service.execute(
                """
                MATCH (n:Entity)
                WHERE n.entity_type = 'KnowledgePoint' AND n.article_id IN $article_ids
                RETURN n
                LIMIT 500
                """,
                {"article_ids": article_ids},
            )
            for row in rows if isinstance(rows, list) else []:
                node = row.get("n") if isinstance(row, dict) else None
                if node:
                    points.append(dict(node))
            relation_rows = await self.kg_service.execute(
                """
                MATCH (a:Entity)-[r]->(b:Entity)
                WHERE a.entity_type = 'KnowledgePoint'
                  AND b.entity_type = 'KnowledgePoint'
                  AND a.article_id IN $article_ids
                  AND b.article_id IN $article_ids
                  AND coalesce(r.status, 'approved') = 'approved'
                RETURN a, b, r
                LIMIT 100
                """,
                {"article_ids": article_ids},
            )
            for row in relation_rows if isinstance(relation_rows, list) else []:
                source = row.get("a") if isinstance(row, dict) else None
                target = row.get("b") if isinstance(row, dict) else None
                relation = row.get("r") if isinstance(row, dict) else None
                if not source or not target or not relation:
                    continue
                relation_details.append({
                    "source": source.get("title") or source.get("name", ""),
                    "target": target.get("title") or target.get("name", ""),
                    "type": relation.get("relation_type") or "related_to",
                    "strength": relation.get("strength", 0.5),
                    "evidence": relation.get("evidence", ""),
                })
            relation_count = len(relation_details)
        except Exception as exc:
            logger.warning("候选事件跨文档知识读取失败: %s", exc)

        positive = ("发布", "获批", "突破", "完成", "签约", "启动", "建成", "上线", "增长", "发现", "合作", "入选")
        negative = ("下降", "失败", "风险", "暂停", "争议", "减少", "下滑")
        text = " ".join(
            [event.get("title", "")] +
            [str(item.get("title", "")) + " " + str(item.get("summary", "")) for item in evidence] +
            [str(point.get("content", "")) for point in points]
        )
        positive_hits = sum(text.count(marker) for marker in positive)
        negative_hits = sum(text.count(marker) for marker in negative)
        if positive_hits > negative_hits:
            trend = "up"
        elif negative_hits > positive_hits:
            trend = "down"
        else:
            trend = "stable"

        evidence_count = len(article_ids)
        independent_sources = {
            (urlparse(str(item.get("url") or "")).netloc or str(item.get("id") or "unknown")).lower().removeprefix("www.")
            for item in evidence
        }
        confidence = min(
            0.9,
            0.35
            + min(0.18, evidence_count * 0.05)
            + min(0.18, len(independent_sources) * 0.08)
            + min(0.16, len(points) * 0.02)
            + min(0.12, relation_count * 0.03),
        )
        # These are conditional paths, not calibrated probabilities.  The
        # corpus has no outcome time series, so inventing probability values or
        # a numeric growth curve would make the result look more certain than
        # the evidence allows.
        scenarios = [
            {
                "name": "基准情景",
                "trend": trend,
                "basis": "现有多来源材料中的推进与约束信号",
                "condition": "后续公开信息与现有证据方向一致",
                "invalidated_by": "权威来源出现相反进展、关键主体否认或计划取消",
            },
            {
                "name": "推进情景",
                "trend": "up",
                "basis": "关键主体继续发布实施、投资、获批或落地证据",
                "condition": "至少出现一项新的独立来源可核验进展",
                "invalidated_by": "执行时间表延期、资金或许可条件未满足",
            },
            {
                "name": "约束情景",
                "trend": "down",
                "basis": "材料中出现风险、争议、成本或监管约束",
                "condition": "约束信号被独立来源重复确认",
                "invalidated_by": "约束被官方澄清或关键指标显著改善",
            },
        ]
        basis = {
            "event_id": event.get("id"),
            "evidence_articles": evidence_count,
            "independent_sources": len(independent_sources),
            "knowledge_points": len(points),
            "cross_document_relations": relation_count,
            "multi_document": evidence_count > 1,
            "evidence_titles": [item.get("title") for item in evidence if item.get("title")],
            "support_level": "较强" if confidence >= 0.7 else ("一般" if confidence >= 0.5 else "较弱"),
            "assessment_mode": "conditional_scenario",
            "confidence_note": (
                f"该分数衡量当前证据覆盖度，不是事件发生概率或数值预测准确率。"
                f"本次 {evidence_count} 篇材料来自 {len(independent_sources)} 个独立来源；"
                + ("同一来源的连续材料主要用于时序走向分析，不能视为多来源交叉确认。" if len(independent_sources) == 1 else "不同来源可用于交叉核验，但仍需检查其是否引用同一首发信息。")
            ),
            "monitoring_indicators": [
                "是否出现新的独立来源确认",
                "关键主体是否发布可核验的实施、审批、投资或调整信息",
                "是否出现与现有结论相矛盾的权威来源或数据",
            ],
            "knowledge_point_details": [
                {
                    "title": point.get("title") or point.get("name", ""),
                    "content": point.get("content", ""),
                    "category": point.get("category", "concept"),
                    "evidence": point.get("evidence", ""),
                    "source_url": point.get("source_url"),
                }
                for point in points[:30]
            ],
            "relation_details": relation_details,
        }
        article_factors = [
            {"type": "source_article", "name": item.get("title"), "strength": 0.45, "evidence": item.get("summary", "")}
            for item in evidence[:8]
        ]
        point_factors = [
            {"type": "knowledge_point", "name": point.get("title") or point.get("name"), "strength": point.get("confidence", 0.5), "evidence": point.get("evidence", "")}
            for point in points[:10]
        ]
        factors = article_factors + point_factors
        prediction = PredictionResult(
            topic=event.get("topic") or event.get("title") or "未命名事件",
            trend=trend,
            confidence=confidence,
            factors=factors,
            timeline=self._generate_monitoring_timeline(time_range),
            prediction_type=prediction_type,
            generated_at=datetime.now().isoformat(),
            knowledge_basis=basis,
            scenarios=scenarios,
        )
        from app.services.kg.event_interpretation import EventInterpretationService
        prediction.interpretation = await EventInterpretationService(self.llm_client).interpret(
            event,
            {
                "trend": trend,
                "knowledge_points": len(points),
                "cross_document_relations": relation_count,
                "confidence": confidence,
                "model_id": model_id,
                "knowledge_point_details": basis["knowledge_point_details"],
                "relation_details": relation_details,
            },
        )
        return prediction

    async def _collect_historical_data(self, topic: str) -> Dict[str, Any]:
        """收集历史数据"""
        try:
            # 搜索相关实体
            entities = await self.kg_service.search_entities(topic, limit=10)

            # 获取关联实体
            related_entities = []
            for entity in entities[:5]:
                try:
                    relationships = await self.kg_service.get_relationships(entity.name)
                    related_entities.extend(relationships)
                except Exception:
                    pass

            return {
                'entities': entities,
                'related_entities': related_entities,
                'entity_count': len(entities),
                'knowledge_basis': await self._collect_knowledge_basis(topic),
            }
        except Exception as e:
            logger.warning(f"收集历史数据失败: {e}")
            return {
                'entities': [],
                'related_entities': [],
                'entity_count': 0,
                'knowledge_basis': {},
            }

    async def _collect_knowledge_basis(self, topic: str) -> Dict[str, Any]:
        """收集已发布综合文档与知识质量指标，作为可追溯预测依据。"""
        basis: Dict[str, Any] = {
            'published_syntheses': 0,
            'synthesis_ids': [],
            'evidence_coverage': None,
            'source_coverage': None,
        }
        try:
            syntheses = await self.kg_service.search_knowledge_syntheses(topic, limit=5)
            basis['published_syntheses'] = len(syntheses)
            basis['synthesis_ids'] = [item.get('id') for item in syntheses if item.get('id')]
        except Exception as e:
            logger.warning(f"收集知识综合依据失败: {e}")
        try:
            metrics = await self.kg_service.get_knowledge_quality_metrics()
            points = metrics.get('knowledge_points', {})
            basis['evidence_coverage'] = points.get('evidence_coverage')
            basis['source_coverage'] = points.get('source_coverage')
        except Exception as e:
            logger.warning(f"收集知识质量指标失败: {e}")
        return basis

    async def _analyze_factors(self, topic: str) -> List[Dict[str, Any]]:
        """分析影响因素"""
        factors = []

        try:
            # 获取相关实体作为因素
            entities = await self.kg_service.search_entities(topic, limit=5)
            for entity in entities:
                factors.append({
                    'type': 'entity',
                    'name': entity.name,
                    'entity_type': entity.entity_type,
                    'strength': 0.5
                })
        except Exception as e:
            logger.warning(f"分析影响因素失败: {e}")

        return factors[:5]  # 最多返回5个因素

    def _generate_prediction(
        self,
        historical_data: Dict[str, Any],
        factors: List[Dict[str, Any]],
        time_range: int,
        prediction_type: str,
        topic: str,
        knowledge_basis: Optional[Dict[str, Any]] = None,
    ) -> PredictionResult:
        """生成预测"""

        # 计算基础趋势
        entity_count = historical_data.get('entity_count', 0)
        factor_count = len(factors)

        # 简单的趋势判断
        if entity_count > 10 and factor_count > 3:
            trend = 'up'
            confidence = 0.7
        elif entity_count < 3:
            trend = 'stable'
            confidence = 0.5
        else:
            trend = 'up'
            confidence = 0.6

        basis = knowledge_basis or {}
        evidence_coverage = basis.get('evidence_coverage')
        source_coverage = basis.get('source_coverage')
        if evidence_coverage is not None:
            confidence *= 0.7 + 0.3 * float(evidence_coverage)
        if source_coverage is not None:
            confidence *= 0.8 + 0.2 * float(source_coverage)
        if not basis.get('published_syntheses') and entity_count == 0:
            confidence = min(confidence, 0.35)

        # No observed time series is available for this legacy topic endpoint.
        # Return review checkpoints instead of synthetic numeric forecasts.
        timeline = self._generate_monitoring_timeline(time_range)

        return PredictionResult(
            topic=topic,
            trend=trend,
            confidence=confidence,
            factors=factors,
            timeline=timeline,
            prediction_type=prediction_type,
            generated_at=datetime.now().isoformat(),
            knowledge_basis=basis,
        )

    def _generate_monitoring_timeline(self, days: int) -> List[Dict[str, Any]]:
        """Return evidence-review checkpoints rather than fabricated values."""
        checkpoints = sorted({0, min(7, days), min(14, days), days})
        return [
            {
                "date": (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d"),
                "checkpoint": f"第 {offset} 天" if offset else "当前基线",
                "purpose": "核验新增独立来源、关键主体行动与反证信号",
                "kind": "evidence_review",
            }
            for offset in checkpoints
        ]

    def _generate_default_timeline(self, days: int) -> List[Dict[str, Any]]:
        """Generate default evidence-review checkpoints."""
        return self._generate_monitoring_timeline(days)

    async def predict_sentiment(self, topic: str) -> PredictionResult:
        """预测舆情趋势"""
        return await self.predict_trend(topic, prediction_type='sentiment')

    async def predict_technology(self, topic: str) -> PredictionResult:
        """预测技术趋势"""
        return await self.predict_trend(topic, prediction_type='technology')
