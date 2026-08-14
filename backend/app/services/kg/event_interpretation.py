"""把多来源事件证据转换为可审核、可行动的影响推演。"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from json_repair import repair_json

logger = logging.getLogger("ai-studio.event_interpretation")


class EventInterpretationService:
    """基于来源正文、知识点和关系生成具体的跨领域情景推演。"""

    GENERIC_TITLES = {
        "出现后续公开报道或官方进展",
        "事件进入具体执行或反馈阶段",
        "持续关注后续进展",
        "等待官方进一步消息",
    }
    INDICATOR_MARKERS = (
        "数量", "比例", "金额", "价格", "成本", "利润", "收入", "订单", "产能", "库存",
        "开工率", "覆盖率", "参与率", "缴费率", "收益率", "名单", "文件", "细则", "通报",
        "许可", "预算", "投资", "处罚", "发布日期", "实施日期", "数据",
    )
    VAGUE_PHRASES = ("产生影响", "行为变化", "政策变化", "持续关注", "进一步发展", "带来机遇")

    def __init__(self, llm_client=None, timeout_seconds: int = 95):
        self.llm_client = llm_client
        self.timeout_seconds = timeout_seconds

    async def interpret(
        self,
        event: Dict[str, Any],
        prediction: Dict[str, Any],
    ) -> Dict[str, Any]:
        evidence = event.get("evidence_articles") or []
        model_id = prediction.get("model_id") or None
        if not self.llm_client:
            return {**self._unavailable("深度推演模型未配置，系统未生成模板化结论。"), "analysis_model": model_id or "系统默认模型"}

        source_text = "\n\n".join(
            self._format_source(index, item)
            for index, item in enumerate(evidence)
        )
        knowledge_points = prediction.get("knowledge_point_details") or []
        relations = prediction.get("relation_details") or []
        knowledge_text = "\n".join(
            f"- {item.get('title', '')}: {item.get('content', '')}；原文依据：{item.get('evidence', '')}"
            for item in knowledge_points[:30]
        ) or "无结构化知识点"
        relation_text = "\n".join(
            f"- {item.get('source', '')} --{item.get('type', 'related_to')}--> {item.get('target', '')}；依据：{item.get('evidence', '')}"
            for item in relations[:30]
        ) or "无已审核关系"

        prompt = f"""
你是一名战略情报与政策产业分析师。请对多来源材料做因果推演，而不是复述新闻或预测“还会有后续报道”。

分析规则：
1. 只能把来源中明确出现的事实写成事实；由事实推到未来影响时，必须明确写出推演链条和不确定性。
2. “出现后续报道”“官方进一步进展”“持续受到关注”“进入执行阶段”不能单独作为结论。
3. 这是综合事件推演，不是单纯的正负面或舆情判断。根据材料选择真正相关的经济、政策/监管、产业、技术、市场、竞争、供应链、资本、社会等维度；证据不足的维度不要强行补写。
4. 后续变化必须具体到：谁会采取什么动作、影响谁、通过什么机制发生、预计时间范围、触发条件、失效条件，以及什么信号可验证或推翻。
5. 机会必须说明潜在受益者、进入条件和消失条件；挑战与风险必须说明风险暴露对象、可能性、影响程度、触发条件、预警信号和准备动作。不要写“存在机遇与挑战”之类空话。
6. 不要把内部方向代码 up/down/stable 解释为股价、概率或确定结果。它仅是材料中推进词与约束词的粗略计数，可质疑或修正。
7. 若材料不足以支持某个维度，明确写“证据不足”，不得编造政策名称、金额、企业行动或统计数据。
8. 不要为了增加数量补写空泛条目；宁可输出 2-3 条完整推演，也不要输出没有主体、机制或验证信号的条目。
9. 舆情只是综合报告的一个维度。若材料主要来自新闻和政府网站，只能分析媒体信息环境，并在 data_scope 中说明局限，不能假装代表完整公众情绪。
10. 控制篇幅：impact_assessments 输出 3-5 条，next_developments 输出 2-4 条，opportunities 和 challenges 各输出 1-3 条；每个字符串字段不超过 180 个汉字。
11. 所有字符串中的双引号必须转义，数组和对象之间必须使用英文逗号。输出前自行检查 JSON 可被解析。

输出严格 JSON，不要输出 Markdown：
{{
  "analysis_status": "complete",
  "executive_judgment": "一段可直接给决策者阅读的核心判断，说明最可能发生的实质变化及原因",
  "event_summary": "多来源共同确认了什么，哪些仍只是观点或推断",
  "current_phase": "议题形成/政策酝酿/商业验证/规模扩张/调整分化/风险暴露/证据不足",
  "signal_assessment": {{
    "label": "推进增强/约束增强/信号分化/方向未明",
    "meaning": "用自然语言解释这个方向在本事件中具体代表什么，不使用 up/down",
    "evidence": "支持该判断的跨来源事实及分歧"
  }},
  "impact_assessments": [
    {{
      "dimension": "经济/政策/产业/技术/市场/竞争/供应链/资本/社会",
      "direction": "加速/放缓/扩张/收缩/分化/调整/方向未明",
      "conclusion": "该维度可能发生的具体变化",
      "mechanism": "事实A -> 主体行为变化B -> 结果C",
      "affected_parties": ["具体受影响主体类型"],
      "horizon": "短期（0-6个月）/中期（6-24个月）/长期（2年以上）",
      "likelihood": "高/中/低",
      "evidence_basis": "引用来源中的事实，不得只写多来源关注"
    }}
  ],
  "next_developments": [
    {{
      "path": "基准/加速/延迟/分化/反转",
      "dimension": "经济/政策/产业/技术/市场/竞争/供应链/资本/社会",
      "title": "可验证的具体变化",
      "likelihood": "高/中/低",
      "timeframe": "预计时间范围",
      "mechanism": "为什么会发生的因果链",
      "trigger_condition": "该变化发生所需的具体条件",
      "invalidated_by": "会使该推演失效的具体情况",
      "affected_parties": ["受影响主体"],
      "basis": "对应的来源事实",
      "watch_for": "能够验证或推翻该推断的具体指标"
    }}
  ],
  "opportunities": [
    {{"title": "具体机会", "opportunity_type": "市场/技术/政策/产业链/资本/经营", "beneficiaries": ["潜在受益者"], "rationale": "价值如何产生", "entry_condition": "机会成立的前提", "invalidated_by": "机会消失的条件", "watch_for": "机会形成的可核验信号", "horizon": "时间范围"}}
  ],
  "challenges": [
    {{"title": "具体挑战或风险", "risk_type": "政策/舆情/技术/市场/供应链/合规/执行", "likelihood": "高/中/低", "impact": "高/中/低", "risk_level": "高/中/低", "exposed_parties": ["风险暴露对象"], "rationale": "风险如何传导", "trigger_condition": "风险触发条件", "warning_signal": "预警指标", "response": "可提前采取的准备动作", "horizon": "时间范围"}}
  ],
  "public_opinion": {{
    "current_direction": "当前媒体信息环境中的主要叙事",
    "sentiment": "正面/中性/负面/分化/证据不足",
    "trend": "升温/降温/稳定/分化/证据不足",
    "key_views": ["有来源依据的主要观点"],
    "controversies": ["主要争议点"],
    "turning_triggers": ["可能使叙事转向的事件或信号"],
    "watch_keywords": ["建议监测的关键词"],
    "data_scope": "说明当前来源覆盖及不能代表完整公众情绪的局限"
  }},
  "drivers": ["关键推动因素及其作用"],
  "risks": ["可能使推演失效的反向因素"],
  "watch_indicators": ["可量化或可核验的跟踪指标"],
  "decision_value": {{"category": "政策/产业/投资/经营/风险/综合", "explanation": "用户现在可以据此采取什么准备动作"}}
}}

候选事件：{event.get('title', '')}
内部粗略方向代码：{prediction.get('trend', 'stable')}（仅供参考，不得原样输出）
证据文档数：{len(evidence)}
结构化知识点数：{prediction.get('knowledge_points', 0)}
已审核关系数：{prediction.get('cross_document_relations', 0)}

来源材料：
{source_text[:14000]}

结构化知识点：
{knowledge_text[:6000]}

已审核关系：
{relation_text[:4000]}
"""
        try:
            response = await asyncio.wait_for(
                self.llm_client.non_stream_chat(
                    model_id=model_id,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=4200,
                ),
                timeout=self.timeout_seconds,
            )
            parsed, validation_error = self._parse_with_reason(response)
            if parsed:
                if parsed.get("quality_warnings"):
                    logger.info("事件深度推演已剔除弱项: %s", "；".join(parsed["quality_warnings"]))
                return {**parsed, "generated_by": "llm", "analysis_model": model_id or "系统默认模型"}
            logger.warning(
                "事件深度推演未通过校验: %s；响应长度=%s",
                validation_error,
                len(str(response or "")),
            )
            return {
                **self._unavailable(
                    f"模型输出未通过分析校验：{validation_error}。系统已拒绝展示模板化推断。"
                ),
                "analysis_model": model_id or "系统默认模型",
            }
        except asyncio.TimeoutError:
            logger.warning("事件深度推演超过 %s 秒", self.timeout_seconds)
            return {**self._unavailable(f"深度推演超过 {self.timeout_seconds} 秒，请稍后重试。"), "analysis_model": model_id or "系统默认模型"}
        except Exception as exc:
            logger.warning("事件深度推演生成失败: %s", exc)
            return {**self._unavailable("深度推演调用失败，系统未使用通用模板替代。"), "analysis_model": model_id or "系统默认模型"}

    @staticmethod
    def _format_source(index: int, item: Dict[str, Any]) -> str:
        body = item.get("analysis_text") or item.get("summary") or ""
        return (
            f"来源{index + 1}: {item.get('title', '')}\n"
            f"发布时间: {item.get('published_at') or '未知'}\n"
            f"摘要: {(item.get('summary') or '')[:500]}\n"
            f"正文摘录: {body[:2000]}"
        )

    @staticmethod
    def _unavailable(reason: str) -> Dict[str, Any]:
        return {
            "analysis_status": "unavailable",
            "analysis_error": reason,
            "executive_judgment": "",
            "event_summary": "",
            "current_phase": "待分析",
            "signal_assessment": {
                "label": "未形成有效判断",
                "meaning": "内部方向代码不作为面向用户的结论。",
                "evidence": "",
            },
            "impact_assessments": [],
            "next_developments": [],
            "opportunities": [],
            "challenges": [],
            "public_opinion": {
                "current_direction": "当前证据不足，未形成可靠的媒体舆情判断。",
                "sentiment": "证据不足",
                "trend": "证据不足",
                "key_views": [],
                "controversies": [],
                "turning_triggers": [],
                "watch_keywords": [],
                "data_scope": "未完成深度分析。",
            },
            "drivers": [],
            "risks": [],
            "watch_indicators": [],
            "decision_value": {
                "category": "暂无",
                "explanation": "请重试深度分析；系统不会用固定话术冒充研判结果。",
            },
            "generated_by": "none",
        }

    @classmethod
    def _parse(cls, response: Any) -> Optional[Dict[str, Any]]:
        parsed, _ = cls._parse_with_reason(response)
        return parsed

    @staticmethod
    def _as_list(value: Any) -> List[Any]:
        if isinstance(value, list):
            return value
        if value in (None, ""):
            return []
        return [value]

    @classmethod
    def _parse_with_reason(cls, response: Any) -> tuple[Optional[Dict[str, Any]], str]:
        if not response:
            return None, "模型未返回内容"
        if isinstance(response, dict):
            data = response
        else:
            text = str(response)
            if "{" not in text or "}" not in text:
                return None, "模型未返回 JSON 对象，可能输出被截断"
            text = text[text.find("{"): text.rfind("}") + 1]
            try:
                data = json.loads(text)
            except (TypeError, json.JSONDecodeError) as exc:
                try:
                    repaired = repair_json(text, return_objects=True)
                except Exception:
                    repaired = None
                if not isinstance(repaired, dict):
                    return None, f"JSON 解析失败（{exc.msg}，位置 {exc.pos}），自动修复失败"
                logger.info("事件深度推演 JSON 已自动修复: %s，位置 %s", exc.msg, exc.pos)
                data = repaired

        required = {
            "executive_judgment", "event_summary", "current_phase", "signal_assessment",
            "impact_assessments", "next_developments", "opportunities", "challenges",
            "drivers", "risks", "watch_indicators", "decision_value",
        }
        if not isinstance(data, dict):
            return None, "JSON 顶层不是对象"
        missing = sorted(required.difference(data))
        if missing:
            return None, f"缺少字段：{', '.join(missing)}"

        for key in ("impact_assessments", "next_developments", "opportunities", "challenges", "drivers", "risks", "watch_indicators"):
            data[key] = cls._as_list(data.get(key))
        public_opinion = data.get("public_opinion")
        if not isinstance(public_opinion, dict):
            public_opinion = {}
        data["public_opinion"] = {
            "current_direction": str(public_opinion.get("current_direction") or "当前材料未形成可靠的媒体舆情判断。"),
            "sentiment": str(public_opinion.get("sentiment") or "证据不足"),
            "trend": str(public_opinion.get("trend") or "证据不足"),
            "key_views": cls._as_list(public_opinion.get("key_views")),
            "controversies": cls._as_list(public_opinion.get("controversies")),
            "turning_triggers": cls._as_list(public_opinion.get("turning_triggers")),
            "watch_keywords": cls._as_list(public_opinion.get("watch_keywords")),
            "data_scope": str(public_opinion.get("data_scope") or "当前结论仅反映已抓取来源的信息环境。"),
        }
        if len(str(data.get("executive_judgment", "")).strip()) < 15:
            return None, "核心研判过短"

        quality_warnings: List[str] = []
        valid_next_developments: List[Dict[str, Any]] = []
        for index, item in enumerate(data["next_developments"], start=1):
            if not isinstance(item, dict):
                quality_warnings.append(f"未来变化第 {index} 条格式无效")
                continue
            if str(item.get("title", "")).strip() in cls.GENERIC_TITLES:
                quality_warnings.append(f"未来变化第 {index} 条为通用后续话术")
                continue
            mechanism = str(item.get("mechanism", "")).strip()
            if len(mechanism) < 12 or any(mechanism.endswith(phrase) for phrase in cls.VAGUE_PHRASES):
                quality_warnings.append(f"未来变化第 {index} 条因果机制过短或模糊")
                continue
            item["affected_parties"] = cls._as_list(item.get("affected_parties"))
            if not item.get("timeframe") or not item["affected_parties"] or not item.get("watch_for"):
                quality_warnings.append(f"未来变化第 {index} 条缺少时间、影响对象或验证指标")
                continue
            item.setdefault("path", "基准")
            item.setdefault("trigger_condition", "")
            item.setdefault("invalidated_by", "")
            watch_for = str(item.get("watch_for", ""))
            has_named_indicator = any(marker in watch_for for marker in cls.INDICATOR_MARKERS)
            is_vague_indicator = any(phrase in watch_for for phrase in cls.VAGUE_PHRASES)
            if len(watch_for) < 8 or (is_vague_indicator and not has_named_indicator):
                quality_warnings.append(f"未来变化第 {index} 条验证指标不够具体")
                continue
            valid_next_developments.append(item)
        if len(valid_next_developments) < 2:
            detail = "；".join(quality_warnings[-3:]) or "模型返回条目不足"
            return None, f"未来变化合格内容少于 2 条（{detail}）"
        data["next_developments"] = valid_next_developments

        valid_impacts: List[Dict[str, Any]] = []
        for index, item in enumerate(data["impact_assessments"], start=1):
            if not isinstance(item, dict):
                quality_warnings.append(f"影响分析第 {index} 条格式无效")
                continue
            conclusion = str(item.get("conclusion", ""))
            mechanism = str(item.get("mechanism", ""))
            item["affected_parties"] = cls._as_list(item.get("affected_parties"))
            if len(conclusion.strip()) < 8:
                quality_warnings.append(f"影响分析第 {index} 条结论过短")
                continue
            if len(mechanism) < 12 or any(mechanism.endswith(phrase) for phrase in cls.VAGUE_PHRASES):
                quality_warnings.append(f"影响分析第 {index} 条因果机制过短或模糊")
                continue
            if any(conclusion.strip().endswith(phrase) for phrase in cls.VAGUE_PHRASES):
                quality_warnings.append(f"影响分析第 {index} 条结论仍然模糊")
                continue
            valid_impacts.append(item)
        if len(valid_impacts) < 2:
            return None, "影响分析合格内容少于 2 个维度"
        for item in valid_impacts:
            item.setdefault("direction", "方向未明")
        data["impact_assessments"] = valid_impacts

        valid_opportunities: List[Dict[str, Any]] = []
        for index, item in enumerate(data["opportunities"], start=1):
            if not isinstance(item, dict) or not item.get("beneficiaries") or not item.get("entry_condition"):
                quality_warnings.append(f"机会第 {index} 条缺少受益者或成立条件")
                continue
            item["beneficiaries"] = cls._as_list(item.get("beneficiaries"))
            combined = f"{item.get('title', '')}{item.get('rationale', '')}{item.get('entry_condition', '')}"
            if len(combined) < 24 or all(phrase in combined for phrase in ("产生影响", "行为变化")):
                quality_warnings.append(f"机会第 {index} 条不够具体")
                continue
            item.setdefault("opportunity_type", "综合")
            item.setdefault("invalidated_by", "")
            item.setdefault("watch_for", "")
            valid_opportunities.append(item)
        if not valid_opportunities:
            return None, "没有合格的具体机会"
        data["opportunities"] = valid_opportunities

        valid_challenges: List[Dict[str, Any]] = []
        for index, item in enumerate(data["challenges"], start=1):
            if not isinstance(item, dict) or not item.get("exposed_parties") or not item.get("warning_signal"):
                quality_warnings.append(f"挑战第 {index} 条缺少风险对象或预警指标")
                continue
            item["exposed_parties"] = cls._as_list(item.get("exposed_parties"))
            warning_signal = str(item.get("warning_signal", ""))
            has_named_indicator = any(marker in warning_signal for marker in cls.INDICATOR_MARKERS)
            is_vague_indicator = any(phrase in warning_signal for phrase in cls.VAGUE_PHRASES)
            if len(warning_signal) < 8 or (is_vague_indicator and not has_named_indicator):
                quality_warnings.append(f"挑战第 {index} 条预警指标不够具体")
                continue
            item.setdefault("risk_type", "综合")
            item.setdefault("likelihood", "中")
            item.setdefault("impact", "中")
            item.setdefault("risk_level", "中")
            item.setdefault("trigger_condition", warning_signal)
            item.setdefault("response", "")
            valid_challenges.append(item)
        if not valid_challenges:
            return None, "没有合格的具体挑战"
        data["challenges"] = valid_challenges

        data["analysis_status"] = "complete"
        data["quality_warnings"] = quality_warnings
        return data, ""
