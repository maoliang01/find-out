'use client'

import { useEffect, useState } from 'react'
import { AlertCircle, BellRing, CheckCircle2, Download, FileText, Link2, Loader2, Play, RefreshCw, Trash2, TrendingUp, X } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

interface EventEvidence {
  id: string
  title: string
  summary: string
  url: string
  published_at?: string | null
  scraped_at?: string | null
  source_domain?: string
}

interface DiscoveredEvent {
  id: string
  title: string
  topic: string
  summary?: string
  content?: string
  confidence: number
  signal_type: string
  signal_reasons: string[]
  independent_source_count?: number
  duplicate_count?: number
  match_evidence?: {
    average_similarity: number
    shared_keywords: string[]
    shared_title_terms: string[]
    max_time_gap_days: number
  }
  evidence_articles: EventEvidence[]
}

interface PredictionResult {
  prediction_id: string
  topic: string
  trend: string
  confidence: number
  prediction_type: string
  generated_at: string
  factors: Array<{ type: string; name: string; evidence?: string }>
  scenarios: Array<{ name: string; trend: string; basis: string; condition?: string; invalidated_by?: string }>
  interpretation?: {
    analysis_status?: 'complete' | 'unavailable'
    analysis_error?: string
    quality_warnings?: string[]
    analysis_model?: string
    generated_by?: string
    executive_judgment?: string
    event_summary: string
    current_phase: string
    signal_assessment?: { label: string; meaning: string; evidence: string }
    impact_assessments?: Array<{
      dimension: string
      direction?: string
      conclusion: string
      mechanism: string
      affected_parties: string[]
      horizon: string
      likelihood: string
      evidence_basis: string
    }>
    next_developments: Array<{
      path?: string
      dimension?: string
      title: string
      likelihood: string
      timeframe?: string
      mechanism?: string
      trigger_condition?: string
      invalidated_by?: string
      affected_parties?: string[]
      basis: string
      watch_for: string
    }>
    opportunities?: Array<{ title: string; opportunity_type?: string; beneficiaries: string[]; rationale: string; entry_condition: string; invalidated_by?: string; watch_for?: string; horizon: string }>
    challenges?: Array<{ title: string; risk_type?: string; likelihood?: string; impact?: string; risk_level?: string; exposed_parties: string[]; rationale: string; trigger_condition?: string; warning_signal: string; response?: string; horizon: string }>
    public_opinion?: {
      current_direction: string
      sentiment: string
      trend: string
      key_views: string[]
      controversies: string[]
      turning_triggers: string[]
      watch_keywords: string[]
      data_scope: string
    }
    drivers: string[]
    risks: string[]
    watch_indicators: string[]
    decision_value: { category: string; explanation: string }
  }
  knowledge_basis: {
    event_id?: string
    evidence_articles?: number
    independent_sources?: number
    knowledge_points?: number
    cross_document_relations?: number
    multi_document?: boolean
    evidence_titles?: string[]
    evidence_article_ids?: string[]
    excluded_article_ids?: string[]
    user_reviewed?: boolean
    reviewed_at?: string
    time_range?: number
    support_level?: string
    assessment_mode?: string
    confidence_note?: string
    monitoring_indicators?: string[]
    knowledge_point_details?: Array<{ title: string; content: string; category: string; evidence?: string; source_url?: string | null }>
    relation_details?: Array<{ source: string; target: string; type: string; strength: number; evidence?: string }>
  }
}

interface AnalysisModel {
  id: string
  name: string
  type: string
  model_name: string
  latency?: number
}

interface Synthesis {
  id: string
  topic: string
  title: string
  summary: string
  content: string
  status: string
  iteration: number
  source_document_ids: string[]
  source_claim_ids: string[]
  quality_score: number | null
  created_at: string | null
}

interface PredictionRecord {
  id: string
  topic: string
  trend: string
  confidence: number
  status: string
  actual_trend: string | null
  accuracy_score: number | null
  knowledge_basis?: { support_level?: string }
  created_at: string | null
}

interface Stats {
  total_articles_processed: number
  enhancement_pending_articles?: number
  enhancement_failed_articles?: number
  total_knowledge_points: number
  total_associations: number
  quality_metrics?: {
    knowledge_points?: { evidence_coverage: number; source_coverage: number }
    candidates?: { pending: number }
  }
}

interface PendingDocument {
  id: string
  title: string
  kg_status: string
  enhancement_status?: string
}

interface InsightAlert {
  id: string
  title: string
  severity: 'low' | 'medium' | 'high'
  confidence: number
  evidence_article_ids: string[]
  signal_reasons: string[]
  match_evidence?: { average_similarity?: number; max_time_gap_days?: number }
  first_seen_at?: string | null
}

interface ManualProcessStatus {
  task_id: string
  status: string
  total: number
  processed: number
  queued: number
  failed: number
  skipped: number
  percentage: number
  current_article?: { title?: string; index?: number } | null
  error_count?: number
}

interface AssociationCandidate {
  id: string
  source_id: string
  target_id: string
  candidate_type: string
  strength: number
  evidence: string
  status: string
  updated_at: string
}

interface KnowledgePoint {
  id: string
  title: string
  article_id: string
  content: string
  category: string
}

const trendLabels: Record<string, string> = { up: '推进信号较多', down: '约束信号较多', stable: '信号相对均衡' }

function trendLabel(value: string) {
  return trendLabels[value] || value
}

const categoryLabels: Record<string, string> = {
  concept: '概念',
  fact: '事实',
  argument: '观点',
  method: '方法',
}

const relationLabels: Record<string, string> = {
  same_as: '同一内容',
  related_to: '相关',
  supports: '支持',
  contradicts: '矛盾',
  extends: '延伸',
}

async function readJson(response: Response) {
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || '请求失败')
  return data
}

function escapeReportHtml(value: unknown) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;')
}

function reportList(items: string[] | undefined) {
  if (!items?.length) return '<p class="empty">暂无</p>'
  return `<ul>${items.map((item) => `<li>${escapeReportHtml(item)}</li>`).join('')}</ul>`
}

export default function SelfEnhancementPage() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [events, setEvents] = useState<DiscoveredEvent[]>([])
  const [syntheses, setSyntheses] = useState<Synthesis[]>([])
  const [history, setHistory] = useState<PredictionRecord[]>([])
  const [prediction, setPrediction] = useState<PredictionResult | null>(null)
  const [loadingEvents, setLoadingEvents] = useState(false)
  const [loadingEventId, setLoadingEventId] = useState<string | null>(null)
  const [loadingSynthesis, setLoadingSynthesis] = useState(false)
  const [analysisModels, setAnalysisModels] = useState<AnalysisModel[]>([])
  const [selectedAnalysisModel, setSelectedAnalysisModel] = useState('')
  const [modelLatencies, setModelLatencies] = useState<Record<string, number>>({})
  const [benchmarkingModels, setBenchmarkingModels] = useState(false)
  const [exportingPdf, setExportingPdf] = useState(false)
  const [pendingDocuments, setPendingDocuments] = useState<PendingDocument[]>([])
  const [loadingPendingDocuments, setLoadingPendingDocuments] = useState(false)
  const [manualTaskId, setManualTaskId] = useState<string | null>(null)
  const [manualProcessStatus, setManualProcessStatus] = useState<ManualProcessStatus | null>(null)
  const [startingManualProcess, setStartingManualProcess] = useState(false)
  const [associationCandidates, setAssociationCandidates] = useState<AssociationCandidate[]>([])
  const [candidatesKnowledgePoints, setCandidatesKnowledgePoints] = useState<Record<string, KnowledgePoint>>({})
  const [loadingCandidates, setLoadingCandidates] = useState(false)
  const [reviewingCandidateId, setReviewingCandidateId] = useState<string | null>(null)
  const [insightAlerts, setInsightAlerts] = useState<InsightAlert[]>([])
  const [scanningAlerts, setScanningAlerts] = useState(false)
  const [reviewingAlertId, setReviewingAlertId] = useState<string | null>(null)
  const [selectedEvidenceByEvent, setSelectedEvidenceByEvent] = useState<Record<string, string[]>>({})
  const [timeRangeByEvent, setTimeRangeByEvent] = useState<Record<string, string>>({})

  const loadStats = async () => {
    const response = await fetch('/api/kg/self-enhancement/stats')
    if (response.ok) setStats(await response.json())
  }

  const loadEvents = async () => {
    setLoadingEvents(true)
    try {
      const response = await fetch('/api/kg/prediction/discover-events?limit=20&days=90')
      const data = await readJson(response)
      const discoveredEvents = (data.events || []) as DiscoveredEvent[]
      setEvents(discoveredEvents)
      setSelectedEvidenceByEvent((current) => {
        const next = { ...current }
        for (const event of discoveredEvents) {
          if (!next[event.id]) next[event.id] = event.evidence_articles.map((article) => article.id)
        }
        return next
      })
    } catch (error) {
      console.error(error)
    } finally {
      setLoadingEvents(false)
    }
  }

  const loadSyntheses = async () => {
    const response = await fetch('/api/kg/self-enhancement/syntheses?limit=20')
    if (response.ok) setSyntheses((await response.json()).syntheses || [])
  }

  const loadHistory = async () => {
    const response = await fetch('/api/kg/prediction/history?limit=20')
    if (response.ok) setHistory((await response.json()).predictions || [])
  }

  const loadInsightAlerts = async () => {
    const response = await fetch('/api/kg/insight-alerts?status=open&limit=10')
    if (response.ok) setInsightAlerts((await response.json()).alerts || [])
  }

  const scanInsightAlerts = async () => {
    if (scanningAlerts) return
    setScanningAlerts(true)
    try {
      await readJson(await fetch('/api/kg/insight-alerts/scan', { method: 'POST' }))
      await Promise.all([loadInsightAlerts(), loadEvents()])
    } finally {
      setScanningAlerts(false)
    }
  }

  const reviewInsightAlert = async (alertId: string, status: 'acknowledged' | 'dismissed') => {
    setReviewingAlertId(alertId)
    try {
      await readJson(await fetch(`/api/kg/insight-alerts/${alertId}/review`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }),
      }))
      setInsightAlerts((current) => current.filter((alert) => alert.id !== alertId))
    } finally {
      setReviewingAlertId(null)
    }
  }

  const loadCandidates = async () => {
    setLoadingCandidates(true)
    try {
      const response = await fetch('/api/kg/self-enhancement/association-candidates?review_status=pending&limit=200')
      if (!response.ok) return
      const data = await response.json() as { candidates: AssociationCandidate[] }
      setAssociationCandidates(data.candidates || [])
      // 加载候选关联涉及的知识点
      if (data.candidates?.length) {
        const kpIds = [...new Set(data.candidates.flatMap(c => [c.source_id, c.target_id]))]
        const kpResponse = await fetch(`/api/kg/self-enhancement/knowledge-points?limit=200`)
        if (kpResponse.ok) {
          const kpData = await kpResponse.json() as { knowledge_points: KnowledgePoint[] }
          const kpMap: Record<string, KnowledgePoint> = {}
          kpData.knowledge_points?.forEach(kp => { kpMap[kp.id] = kp })
          setCandidatesKnowledgePoints(kpMap)
        }
      }
    } catch (error) {
      console.error('加载候选关联失败:', error)
    } finally {
      setLoadingCandidates(false)
    }
  }

  const reviewCandidate = async (candidate: AssociationCandidate, decision: 'approved' | 'rejected', relationType?: string) => {
    setReviewingCandidateId(candidate.id)
    try {
      const targetKp = candidatesKnowledgePoints[candidate.target_id]
      const response = await fetch(`/api/kg/self-enhancement/associations/${candidate.id}/review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision, relation_type: relationType || (candidate.candidate_type === 'conflict_candidate' ? 'contradicts' : 'related_to') }),
      })
      if (response.ok) {
        setAssociationCandidates(prev => prev.filter(c => c.id !== candidate.id))
        if (decision === 'approved') await loadStats()
      }
    } catch (error) {
      console.error('审核失败:', error)
    } finally {
      setReviewingCandidateId(null)
    }
  }

  const loadPendingDocuments = async () => {
    setLoadingPendingDocuments(true)
    try {
      const response = await fetch('/api/kg/self-enhancement/auto-detect-pending', { method: 'POST' })
      const data = await readJson(response)
      setPendingDocuments(data.pending_articles || [])
    } catch (error) {
      console.error('加载待处理文档失败:', error)
    } finally {
      setLoadingPendingDocuments(false)
    }
  }

  const benchmarkAnalysisModels = async (candidates = analysisModels) => {
    if (!candidates.length || benchmarkingModels) return
    setBenchmarkingModels(true)
    try {
      const results = await Promise.all(candidates.map(async (model) => {
        const startedAt = performance.now()
        try {
          const response = await fetch(`/api/models/${encodeURIComponent(model.id)}/test`, { method: 'POST' })
          const result = await response.json()
          if (!response.ok || !result.success) return null
          return { id: model.id, latency: Number(result.latency) || Math.round(performance.now() - startedAt) }
        } catch {
          return null
        }
      }))
      const successful = results.filter((item): item is { id: string; latency: number } => item !== null)
      if (!successful.length) return
      const latencies = Object.fromEntries(successful.map((item) => [item.id, item.latency]))
      const fastest = [...successful].sort((left, right) => left.latency - right.latency)[0]
      setModelLatencies(latencies)
      setSelectedAnalysisModel(fastest.id)
      localStorage.setItem('kg-analysis-model', fastest.id)
      localStorage.setItem('kg-analysis-model-latencies', JSON.stringify(latencies))
    } finally {
      setBenchmarkingModels(false)
    }
  }

  const loadAnalysisModels = async () => {
    try {
      const response = await fetch('/api/models')
      const data = await readJson(response)
      const candidates = (Array.isArray(data) ? data : []).filter((model: AnalysisModel) => model.type !== 'embedding')
      setAnalysisModels(candidates)
      const cachedLatencies = JSON.parse(localStorage.getItem('kg-analysis-model-latencies') || '{}') as Record<string, number>
      setModelLatencies(cachedLatencies)
      const cachedSelection = localStorage.getItem('kg-analysis-model') || ''
      const validCachedSelection = candidates.some((model: AnalysisModel) => model.id === cachedSelection)
      if (validCachedSelection) {
        setSelectedAnalysisModel(cachedSelection)
      } else if (candidates.length) {
        void benchmarkAnalysisModels(candidates)
      }
    } catch (error) {
      console.error('加载分析模型失败:', error)
    }
  }

  const reload = async () => {
    await Promise.all([loadStats(), loadEvents(), loadPendingDocuments(), loadInsightAlerts()])
  }

  useEffect(() => {
    reload()
    loadAnalysisModels()
  }, [])

  useEffect(() => {
    if (!manualTaskId) return
    let stopped = false
    let timer: number | undefined
    const poll = async () => {
      try {
        const response = await fetch(`/api/kg/self-enhancement/batch-status/${manualTaskId}`)
        if (!response.ok) return
        const data = await response.json() as ManualProcessStatus
        if (stopped) return
        setManualProcessStatus(data)
        if (['completed', 'failed'].includes(data.status)) {
          if (timer) window.clearInterval(timer)
          await Promise.all([loadStats(), loadPendingDocuments(), loadCandidates()])
        }
      } catch (error) {
        console.error('查询文档处理进度失败:', error)
      }
    }
    timer = window.setInterval(poll, 1500)
    void poll()
    return () => {
      stopped = true
      if (timer) window.clearInterval(timer)
    }
  }, [manualTaskId])

  const processPendingDocuments = async () => {
    if (startingManualProcess || !pendingDocuments.length) return
    setStartingManualProcess(true)
    try {
      const response = await fetch('/api/kg/self-enhancement/batch-process', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ article_ids: pendingDocuments.map((article) => article.id) }),
      })
      const data = await readJson(response)
      if (!data.task_id) {
        setManualProcessStatus({
          task_id: '', status: 'completed', total: data.total || 0, processed: 0,
          queued: 0, failed: 0, skipped: data.skipped || 0, percentage: 100,
        })
        await Promise.all([loadStats(), loadPendingDocuments(), loadCandidates()])
        return
      }
      setManualTaskId(data.task_id)
      setManualProcessStatus({
        task_id: data.task_id, status: data.status || 'started', total: data.total || 0,
        processed: 0, queued: 0, failed: 0, skipped: data.skipped || 0, percentage: 0,
      })
    } catch (error) {
      alert(error instanceof Error ? error.message : '手动处理文档失败')
    } finally {
      setStartingManualProcess(false)
    }
  }

  const predictEvent = async (event: DiscoveredEvent) => {
    const articleIds = selectedEvidenceByEvent[event.id] || event.evidence_articles.map((article) => article.id)
    if (articleIds.length < 2) {
      alert('交叉推演至少需要保留两个独立来源，请重新审核材料。')
      return
    }
    const timeRange = Number(timeRangeByEvent[event.id] || '30')
    setLoadingEventId(event.id)
    try {
      const requestPrediction = async (modelId?: string) => {
        const controller = new AbortController()
        const timeout = window.setTimeout(() => controller.abort(), 175000)
        try {
          const response = await fetch('/api/kg/prediction/discovered-event', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ event_id: event.id, article_ids: articleIds, time_range: timeRange, prediction_type: 'general', model_id: modelId || undefined }),
            signal: controller.signal,
          })
          return await readJson(response) as PredictionResult
        } finally {
          window.clearTimeout(timeout)
        }
      }

      const preferredModel = selectedAnalysisModel || undefined
      let result = await requestPrediction(preferredModel)
      const retryableFailure = result.interpretation?.analysis_status === 'unavailable'
        && /超过|JSON 解析失败|自动修复失败/.test(result.interpretation.analysis_error || '')
      if (retryableFailure && analysisModels.length > 1) {
        const currentIndex = Math.max(analysisModels.findIndex((model) => model.id === preferredModel), 0)
        const fallbackModel = analysisModels[(currentIndex + 1) % analysisModels.length]
        if (fallbackModel.id !== preferredModel) {
          const fallbackResult = await requestPrediction(fallbackModel.id)
          result = fallbackResult
          if (fallbackResult.interpretation?.analysis_status === 'complete') {
            setSelectedAnalysisModel(fallbackModel.id)
            localStorage.setItem('kg-analysis-model', fallbackModel.id)
          }
        }
      }
      setPrediction(result)
      await loadHistory()
    } catch (error) {
      alert(error instanceof DOMException && error.name === 'AbortError' ? '深度分析请求超时，请稍后重试。系统不会用固定模板替代分析结果。' : error instanceof Error ? error.message : '交叉分析失败')
    } finally {
      setLoadingEventId(null)
    }
  }

  const exportPredictionPdf = async () => {
    const interpretation = prediction?.interpretation
    if (!prediction || !interpretation || interpretation.analysis_status !== 'complete' || exportingPdf) return
    setExportingPdf(true)
    const report = document.createElement('div')
    try {
      const [{ jsPDF }, { default: html2canvas }] = await Promise.all([import('jspdf'), import('html2canvas')])
      const modelName = analysisModels.find((model) => model.id === interpretation.analysis_model)?.name || interpretation.analysis_model || '系统默认模型'
      const impacts = (interpretation.impact_assessments || []).map((item) => `
        <article><div class="meta">${escapeReportHtml(item.dimension)} · ${escapeReportHtml(item.direction || '方向待判断')} · ${escapeReportHtml(item.horizon)} · 可能性${escapeReportHtml(item.likelihood)}</div>
        <h3>${escapeReportHtml(item.conclusion)}</h3><p><strong>推演机制：</strong>${escapeReportHtml(item.mechanism)}</p>
        <p><strong>影响对象：</strong>${escapeReportHtml(item.affected_parties?.join('、'))}</p><p><strong>事实依据：</strong>${escapeReportHtml(item.evidence_basis)}</p></article>`).join('')
      const developments = (interpretation.next_developments || []).map((item) => `
        <article><div class="meta">${escapeReportHtml(item.path || '基准')}路径 · ${escapeReportHtml(item.dimension || '综合')} · ${escapeReportHtml(item.timeframe)} · 可能性${escapeReportHtml(item.likelihood)}</div>
        <h3>${escapeReportHtml(item.title)}</h3><p><strong>发生机制：</strong>${escapeReportHtml(item.mechanism)}</p>
        ${item.trigger_condition ? `<p><strong>触发条件：</strong>${escapeReportHtml(item.trigger_condition)}</p>` : ''}${item.invalidated_by ? `<p><strong>失效条件：</strong>${escapeReportHtml(item.invalidated_by)}</p>` : ''}
        <p><strong>影响对象：</strong>${escapeReportHtml(item.affected_parties?.join('、'))}</p><p><strong>判断依据：</strong>${escapeReportHtml(item.basis)}</p>
        <p><strong>验证或推翻：</strong>${escapeReportHtml(item.watch_for)}</p></article>`).join('')
      const opportunities = (interpretation.opportunities || []).map((item) => `
        <article><div class="meta">${escapeReportHtml(item.opportunity_type || '综合')} · ${escapeReportHtml(item.horizon)}</div><h3>${escapeReportHtml(item.title)}</h3><p><strong>潜在受益者：</strong>${escapeReportHtml(item.beneficiaries.join('、'))}</p>
        <p>${escapeReportHtml(item.rationale)}</p><p><strong>成立条件：</strong>${escapeReportHtml(item.entry_condition)}</p>${item.invalidated_by ? `<p><strong>消失条件：</strong>${escapeReportHtml(item.invalidated_by)}</p>` : ''}${item.watch_for ? `<p><strong>形成信号：</strong>${escapeReportHtml(item.watch_for)}</p>` : ''}</article>`).join('')
      const challenges = (interpretation.challenges || []).map((item) => `
        <article><div class="meta">${escapeReportHtml(item.risk_type || '综合')} · 风险等级${escapeReportHtml(item.risk_level || '待判断')} · 可能性${escapeReportHtml(item.likelihood || '待判断')} · 影响${escapeReportHtml(item.impact || '待判断')} · ${escapeReportHtml(item.horizon)}</div><h3>${escapeReportHtml(item.title)}</h3><p><strong>风险对象：</strong>${escapeReportHtml(item.exposed_parties.join('、'))}</p>
        <p>${escapeReportHtml(item.rationale)}</p>${item.trigger_condition ? `<p><strong>触发条件：</strong>${escapeReportHtml(item.trigger_condition)}</p>` : ''}<p><strong>预警信号：</strong>${escapeReportHtml(item.warning_signal)}</p>${item.response ? `<p><strong>准备动作：</strong>${escapeReportHtml(item.response)}</p>` : ''}</article>`).join('')
      const publicOpinion = interpretation.public_opinion
      const publicOpinionHtml = publicOpinion ? `<article><div class="meta">情绪：${escapeReportHtml(publicOpinion.sentiment)} · 趋势：${escapeReportHtml(publicOpinion.trend)}</div><h3>${escapeReportHtml(publicOpinion.current_direction)}</h3><p><strong>主要观点：</strong>${escapeReportHtml(publicOpinion.key_views?.join('；') || '暂无')}</p><p><strong>争议焦点：</strong>${escapeReportHtml(publicOpinion.controversies?.join('；') || '暂无')}</p><p><strong>转向触发：</strong>${escapeReportHtml(publicOpinion.turning_triggers?.join('；') || '暂无')}</p><p><strong>监测关键词：</strong>${escapeReportHtml(publicOpinion.watch_keywords?.join('、') || '暂无')}</p><p class="meta">数据范围：${escapeReportHtml(publicOpinion.data_scope)}</p></article>` : '<p class="empty">当前材料未形成可靠的媒体舆情判断</p>'

      report.setAttribute('aria-hidden', 'true')
      report.style.cssText = 'position:absolute;left:0;top:0;z-index:2147483647;box-sizing:border-box;width:860px;background:#fff;color:#172033;font-family:"Microsoft YaHei","PingFang SC",Arial,sans-serif;padding:38px;line-height:1.7;letter-spacing:0;'
      report.innerHTML = `
        <style>
          h1{font-size:26px;margin:0 0 8px;color:#111827}h2{font-size:18px;margin:24px 0 10px;padding-bottom:6px;border-bottom:2px solid #1d4ed8;color:#172554}
          h3{font-size:15px;margin:6px 0;color:#111827}p{font-size:13px;margin:5px 0}.meta{font-size:11px;color:#64748b}article{break-inside:avoid;border-left:3px solid #cbd5e1;padding:8px 12px;margin:10px 0;background:#f8fafc}
          ul{margin:6px 0;padding-left:22px}li{font-size:13px;margin:4px 0}.summary{border-left:4px solid #2563eb;background:#eff6ff;padding:12px 16px;margin:16px 0}.empty{color:#94a3b8}.footer{margin-top:28px;padding-top:10px;border-top:1px solid #cbd5e1;font-size:10px;color:#64748b}
        </style>
        <h1>多源事件综合推演报告</h1><p>${escapeReportHtml(prediction.topic)}</p>
        <p class="meta">生成时间：${escapeReportHtml(new Date(prediction.generated_at || Date.now()).toLocaleString('zh-CN'))}　分析模型：${escapeReportHtml(modelName)}</p>
        <p class="meta">证据支持度：${escapeReportHtml(prediction.knowledge_basis.support_level || '待评估')}（${Math.round(prediction.confidence * 100)}分）　材料：${prediction.knowledge_basis.evidence_articles || 0}篇　独立来源：${prediction.knowledge_basis.independent_sources || 0}个　知识点：${prediction.knowledge_basis.knowledge_points || 0}个　已审核关系：${prediction.knowledge_basis.cross_document_relations || 0}条</p>
        <div class="summary"><h3>核心研判</h3><p>${escapeReportHtml(interpretation.executive_judgment)}</p><p>${escapeReportHtml(interpretation.event_summary)}</p></div>
        <h2>信号含义</h2><p><strong>${escapeReportHtml(interpretation.signal_assessment?.label)}</strong>　${escapeReportHtml(interpretation.signal_assessment?.meaning)}</p><p>${escapeReportHtml(interpretation.signal_assessment?.evidence)}</p>
        <h2>下一步事件推演</h2>${developments || '<p class="empty">暂无</p>'}
        <h2>行业与跨领域走向</h2>${impacts || '<p class="empty">暂无</p>'}
        <h2>潜在机遇</h2>${opportunities || '<p class="empty">暂无</p>'}
        <h2>挑战与风险预测</h2>${challenges || '<p class="empty">暂无</p>'}
        <h2>媒体舆情走向</h2>${publicOpinionHtml}
        <h2>推动因素</h2>${reportList(interpretation.drivers)}<h2>推演失效风险</h2>${reportList(interpretation.risks)}
        <h2>下一步跟踪指标</h2>${reportList(interpretation.watch_indicators)}
        <h2>来源文章</h2>${reportList(prediction.knowledge_basis.evidence_titles)}
        <div class="footer">本报告区分来源事实与模型推断，仅用于线索研判和持续跟踪，不作为确定性决策依据。</div>`
      document.body.appendChild(report)
      const canvas = await html2canvas(report, {
        scale: 1.5,
        useCORS: true,
        backgroundColor: '#ffffff',
        onclone: (clonedDocument) => {
          clonedDocument.querySelectorAll('head link[rel="stylesheet"], head style').forEach((node) => node.remove())
          clonedDocument.documentElement.style.background = '#ffffff'
          clonedDocument.body.style.background = '#ffffff'
        },
      })
      const pdf = new jsPDF({ orientation: 'portrait', unit: 'mm', format: 'a4', compress: true })
      const pageWidthMm = 182
      const pageHeightMm = 273
      const pageHeightPx = Math.floor(canvas.width * pageHeightMm / pageWidthMm)
      for (let offsetY = 0, pageIndex = 0; offsetY < canvas.height; offsetY += pageHeightPx, pageIndex += 1) {
        if (pageIndex > 0) pdf.addPage()
        const sliceHeight = Math.min(pageHeightPx, canvas.height - offsetY)
        const pageCanvas = document.createElement('canvas')
        pageCanvas.width = canvas.width
        pageCanvas.height = sliceHeight
        const context = pageCanvas.getContext('2d')
        if (!context) throw new Error('无法创建 PDF 页面画布')
        context.fillStyle = '#ffffff'
        context.fillRect(0, 0, pageCanvas.width, pageCanvas.height)
        context.drawImage(canvas, 0, offsetY, canvas.width, sliceHeight, 0, 0, canvas.width, sliceHeight)
        const renderedHeightMm = sliceHeight * pageWidthMm / canvas.width
        pdf.addImage(pageCanvas.toDataURL('image/jpeg', 0.92), 'JPEG', 14, 12, pageWidthMm, renderedHeightMm, undefined, 'FAST')
      }
      const safeName = prediction.topic.replace(/[\\/:*?"<>|]/g, '_').slice(0, 48) || '多源事件综合推演报告'
      pdf.save(`${safeName}.pdf`)
    } catch (error) {
      console.error('导出 PDF 失败:', error)
      alert('PDF 导出失败，请稍后重试。')
    } finally {
      report.remove()
      setExportingPdf(false)
    }
  }

  const createAutoSynthesis = async () => {
    setLoadingSynthesis(true)
    try {
      const response = await fetch('/api/kg/self-enhancement/syntheses/auto', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ limit: 5 }),
      })
      await readJson(response)
      await loadSyntheses()
    } catch (error) {
      alert(error instanceof Error ? error.message : '知识综合失败')
    } finally {
      setLoadingSynthesis(false)
    }
  }

  const reviewSynthesis = async (id: string, decision: 'approved' | 'rejected') => {
    const response = await fetch(`/api/kg/self-enhancement/syntheses/${id}/review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision }),
    })
    if (!response.ok) {
      const data = await response.json()
      alert(data.detail || '审核失败')
      return
    }
    await loadSyntheses()
  }

  const submitFeedback = async (id: string, actualTrend: 'up' | 'down' | 'stable') => {
    const response = await fetch(`/api/kg/prediction/history/${id}/feedback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ actual_trend: actualTrend }),
    })
    if (response.ok) await loadHistory()
  }

  const multiDocumentEvents = events.filter((event) => event.evidence_articles.length >= 2)
  const evidenceCoverage = stats?.quality_metrics?.knowledge_points?.evidence_coverage || 0

  return (
    <main className="container mx-auto max-w-7xl p-6">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <TrendingUp className="h-8 w-8" />
            <h1 className="text-3xl font-bold">知识洞察中心</h1>
          </div>
          <p className="mt-2 text-sm text-gray-600">
            系统从持续进入的文章中发现事件，交叉比较多篇来源，生成可审核的趋势、舆情、科技和商机洞察。
          </p>
        </div>
        <Button variant="outline" onClick={reload} title="重新加载数据">
          <RefreshCw className="mr-2 h-4 w-4" />刷新
        </Button>
      </div>

      <Card className="mb-6 border-blue-200 bg-blue-50/40">
        <CardContent className="grid gap-4 p-4 text-sm md:grid-cols-4">
          <div><strong>1. 数据进入</strong><p className="mt-1 text-gray-600">爬虫或外部导入文章，系统自动去重并保留来源。</p></div>
          <div><strong>2. 事件发现</strong><p className="mt-1 text-gray-600">从近期文章中发现至少两个来源共同指向的变化。</p></div>
          <div><strong>3. 交叉分析</strong><p className="mt-1 text-gray-600">比较文章、知识点和关系，输出多种可能情景。</p></div>
          <div><strong>4. 审核反馈</strong><p className="mt-1 text-gray-600">发布有价值的综合知识，并反馈预测是否命中。</p></div>
        </CardContent>
      </Card>

      <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-4">
        <Card><CardHeader className="pb-2"><CardTitle className="text-sm text-gray-500">自增强已完成</CardTitle></CardHeader><CardContent><div className="text-3xl font-bold">{stats?.total_articles_processed || 0}</div><p className="mt-1 text-xs text-gray-500">待处理 {stats?.enhancement_pending_articles || 0} · 失败 {stats?.enhancement_failed_articles || 0}</p></CardContent></Card>
        <Card><CardHeader className="pb-2"><CardTitle className="text-sm text-gray-500">知识点</CardTitle></CardHeader><CardContent><div className="text-3xl font-bold">{stats?.total_knowledge_points || 0}</div><p className="mt-1 text-xs text-gray-500">作为分析证据，不是最终洞察</p></CardContent></Card>
        <Card><CardHeader className="pb-2"><CardTitle className="text-sm text-gray-500">已审核关系</CardTitle></CardHeader><CardContent><div className="text-3xl font-bold">{stats?.total_associations || 0}</div><p className="mt-1 text-xs text-gray-500">已确认的知识点之间关系</p></CardContent></Card>
        <Card><CardHeader className="pb-2"><CardTitle className="text-sm text-gray-500">证据覆盖率</CardTitle></CardHeader><CardContent><div className="text-3xl font-bold">{Math.round(evidenceCoverage * 100)}%</div><p className="mt-1 text-xs text-gray-500">知识点带原文证据的比例</p></CardContent></Card>
      </div>

      {insightAlerts.length > 0 && <section className="mb-6 rounded-lg border border-amber-200 bg-amber-50/60 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2"><BellRing className="h-4 w-4 text-amber-700" /><h2 className="font-semibold text-amber-950">待复核预警</h2><Badge variant="outline" className="border-amber-300 bg-white">{insightAlerts.length}</Badge></div>
          <Button size="sm" variant="outline" onClick={scanInsightAlerts} disabled={scanningAlerts}>{scanningAlerts ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="mr-1 h-3.5 w-3.5" />}扫描新信号</Button>
        </div>
        <div className="mt-3 space-y-2">
          {insightAlerts.map((alert) => <div key={alert.id} className="flex flex-wrap items-center justify-between gap-3 border-t border-amber-200 pt-3 first:border-t-0 first:pt-0">
            <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><span className="font-medium">{alert.title}</span><Badge variant="outline">{alert.severity === 'high' ? '高优先级' : alert.severity === 'medium' ? '中优先级' : '低优先级'}</Badge></div><p className="mt-1 text-xs text-amber-900/80">{alert.evidence_article_ids.length} 个独立来源 · 证据支持度 {Math.round(alert.confidence * 100)}%{alert.match_evidence?.max_time_gap_days !== undefined ? ` · 最大时间间隔 ${alert.match_evidence.max_time_gap_days} 天` : ''}</p></div>
            <div className="flex gap-2"><Button size="sm" variant="outline" disabled={reviewingAlertId === alert.id} onClick={() => reviewInsightAlert(alert.id, 'acknowledged')}>确认跟踪</Button><Button size="sm" variant="ghost" disabled={reviewingAlertId === alert.id} onClick={() => reviewInsightAlert(alert.id, 'dismissed')}>忽略</Button></div>
          </div>)}
        </div>
      </section>}

      <Card className="mb-6 border-amber-200 bg-amber-50/40">
        <CardHeader className="flex flex-row items-start justify-between gap-4 pb-3">
          <div>
            <CardTitle className="text-base">手动处理文档</CardTitle>
            <p className="mt-1 text-sm text-gray-600">
              将尚未完成知识增强的文档批量加入处理队列，已完成文档会自动跳过，不会重复生成知识点。
            </p>
          </div>
          <Button onClick={processPendingDocuments} disabled={startingManualProcess || loadingPendingDocuments || !pendingDocuments.length}>
            {startingManualProcess ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
            {startingManualProcess ? '正在提交' : '处理待处理文档'}
          </Button>
        </CardHeader>
        <CardContent className="space-y-3 pt-0">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <Badge variant={pendingDocuments.length ? 'secondary' : 'outline'}>
              {loadingPendingDocuments ? '正在检查' : `待处理 ${pendingDocuments.length} 篇`}
            </Badge>
            {manualProcessStatus && <span className="text-gray-600">
              本次已入队 {manualProcessStatus.queued} 篇，跳过 {manualProcessStatus.skipped} 篇，失败 {manualProcessStatus.failed} 篇
            </span>}
          </div>
          {manualProcessStatus && manualProcessStatus.status === 'running' && (
            <div>
              <div className="mb-1 flex justify-between text-xs text-gray-600">
                <span>{manualProcessStatus.current_article?.title || '正在加入处理队列'}</span>
                <span>{manualProcessStatus.percentage}%</span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-amber-100">
                <div className="h-full rounded-full bg-amber-500 transition-all" style={{ width: `${manualProcessStatus.percentage}%` }} />
              </div>
            </div>
          )}
          {manualProcessStatus?.status === 'completed' && <p className="text-xs text-emerald-700">文档已加入知识增强队列，后台会继续处理；处理完成后“已处理文档”会自动增长。</p>}
          {!loadingPendingDocuments && !pendingDocuments.length && !manualProcessStatus && <p className="text-xs text-emerald-700">当前没有待处理文档。</p>}
        </CardContent>
      </Card>

      <Tabs defaultValue="events">
        <TabsList className="mb-4">
          <TabsTrigger value="events">事件研判与综合推演</TabsTrigger>
        </TabsList>

        <TabsContent value="events" className="space-y-4">
          <Card>
            <CardHeader className="flex flex-row items-start justify-between gap-4">
              <div>
                <CardTitle>系统发现的候选事件</CardTitle>
                <p className="mt-1 text-sm text-gray-500">先审核多来源材料，再由您决定是否生成下一步事件、行业走向、主体行动、机遇、挑战、风险和媒体舆情的综合推演。</p>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <span className="text-xs font-medium text-gray-600">分析模型</span>
                  <Select value={selectedAnalysisModel || null} onValueChange={(value) => { if (value) { setSelectedAnalysisModel(value); localStorage.setItem('kg-analysis-model', value) } }}>
                    <SelectTrigger size="sm" className="w-64"><SelectValue placeholder={benchmarkingModels ? '正在测试模型速度...' : '选择分析模型'} /></SelectTrigger>
                    <SelectContent>{analysisModels.map((model) => <SelectItem key={model.id} value={model.id}>{model.name}{modelLatencies[model.id] ? ` · ${modelLatencies[model.id]}ms` : ''}</SelectItem>)}</SelectContent>
                  </Select>
                  <Button size="sm" variant="outline" onClick={() => benchmarkAnalysisModels()} disabled={benchmarkingModels || !analysisModels.length}>
                    {benchmarkingModels ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="mr-1 h-3.5 w-3.5" />}{benchmarkingModels ? '测速中' : '测速并选最快'}
                  </Button>
                </div>
              </div>
              <Button variant="outline" onClick={loadEvents} disabled={loadingEvents}>
                <RefreshCw className={`mr-2 h-4 w-4 ${loadingEvents ? 'animate-spin' : ''}`} />刷新发现
              </Button>
            </CardHeader>
            <CardContent className="space-y-3">
              {!multiDocumentEvents.length ? (
                <div className="rounded border border-dashed p-8 text-center text-sm text-gray-500">
                  暂无多来源候选事件。当前只有单源信号，证据不足时系统不会用知识综合内容代替交叉发现。
                </div>
              ) : multiDocumentEvents.map((event) => (
                <div key={event.id} className="rounded-lg border p-4">
                  <div className="flex flex-wrap items-start justify-between gap-4">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">{event.title}</h3><Badge variant={event.signal_type === 'cross_document' ? 'default' : 'secondary'}>{event.signal_type === 'cross_document' ? '多来源交叉确认' : '单源时序分析'}</Badge></div>
                      <p className="mt-1 text-sm text-gray-500">证据支持度 {Math.round(event.confidence * 100)}分 · 独立来源 {event.independent_source_count ?? event.evidence_articles.length} 个{event.duplicate_count ? ` · 已折叠重复内容 ${event.duplicate_count} 篇` : ''}</p>
                      <div className="mt-2 flex flex-wrap gap-2">{event.signal_reasons.map((reason) => <Badge key={reason} variant="outline">{reason}</Badge>)}</div>
                      {event.match_evidence && <p className="mt-2 text-xs leading-5 text-gray-500">匹配依据：相似度 {Math.round(event.match_evidence.average_similarity * 100)}% · 最大时间间隔 {event.match_evidence.max_time_gap_days} 天{event.match_evidence.shared_keywords.length ? ` · 共同关键词 ${event.match_evidence.shared_keywords.join('、')}` : ''}</p>}
                    </div>
                    <div className="flex flex-col items-end gap-2">
                      <Select value={timeRangeByEvent[event.id] || '30'} onValueChange={(value) => value && setTimeRangeByEvent((current) => ({ ...current, [event.id]: value }))}>
                        <SelectTrigger size="sm" className="w-36"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="7">推演未来 7 天</SelectItem>
                          <SelectItem value="30">推演未来 30 天</SelectItem>
                          <SelectItem value="90">推演未来 90 天</SelectItem>
                          <SelectItem value="180">推演未来 180 天</SelectItem>
                        </SelectContent>
                      </Select>
                      <Button onClick={() => predictEvent(event)} disabled={loadingEventId !== null || benchmarkingModels || !selectedAnalysisModel || (selectedEvidenceByEvent[event.id]?.length || 0) < 2}>
                        {loadingEventId === event.id ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <TrendingUp className="mr-2 h-4 w-4" />}
                        审核材料并生成推演
                      </Button>
                    </div>
                  </div>
                  <details open className="mt-3 rounded bg-gray-50 p-3 text-sm">
                    <summary className="cursor-pointer font-medium">审核参与推演的材料（已选 {selectedEvidenceByEvent[event.id]?.length || 0}/{event.evidence_articles.length}）</summary>
                    <div className="mt-3 space-y-3">{event.evidence_articles.map((article) => {
                      const checked = (selectedEvidenceByEvent[event.id] || []).includes(article.id)
                      return <label key={article.id} className="flex cursor-pointer items-start gap-3 border-b pb-3 last:border-0">
                        <Checkbox
                          checked={checked}
                          onCheckedChange={(nextChecked) => setSelectedEvidenceByEvent((current) => {
                            const selected = new Set(current[event.id] || [])
                            if (nextChecked === true) selected.add(article.id)
                            else selected.delete(article.id)
                            return { ...current, [event.id]: Array.from(selected) }
                          })}
                          aria-label={`选择材料：${article.title}`}
                        />
                        <span className="min-w-0 flex-1"><span className="font-medium">{article.title}</span><span className="mt-1 block text-xs text-gray-500">{article.source_domain || '未知来源'}{article.published_at ? ` · ${article.published_at}` : ''}</span><span className="mt-1 block text-xs leading-5 text-gray-600">{article.summary || '无摘要'}</span></span>
                      </label>
                    })}</div>
                  </details>
                </div>
              ))}
            </CardContent>
          </Card>

          {prediction && (
            <Card className="border-green-200">
              <CardHeader>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div><CardTitle>多源事件综合推演</CardTitle><p className="mt-1 text-sm text-gray-500">{prediction.topic}</p></div>
                  <div className="flex items-center gap-2">
                    <Button size="sm" variant="outline" onClick={exportPredictionPdf} disabled={prediction.interpretation?.analysis_status !== 'complete' || exportingPdf}>
                      {exportingPdf ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Download className="mr-1 h-4 w-4" />}{exportingPdf ? '正在生成' : '导出 PDF'}
                    </Button>
                    <Badge variant={prediction.interpretation?.analysis_status === 'unavailable' ? 'destructive' : 'default'}>
                      {prediction.interpretation?.analysis_status === 'unavailable' ? '深度分析未完成' : '深度分析完成'}
                    </Badge>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-5">
                <div className="rounded border border-slate-200 bg-slate-50 p-3 text-sm text-gray-700">
                  推演会区分来源事实与模型推断。内部方向代码仅用于候选事件初筛，不代表股价、概率或必然结果，也不再作为面向用户的结论。
                </div>
                <div className="flex flex-wrap gap-2">
                  <Badge variant="outline">证据支持度：{prediction.knowledge_basis.support_level || '待评估'}（{Math.round(prediction.confidence * 100)}分）</Badge>
                  <Badge variant="outline">来源：{prediction.knowledge_basis.evidence_articles || 0} 篇</Badge>
                  <Badge variant="outline">独立来源：{prediction.knowledge_basis.independent_sources || 0} 个</Badge>
                  <Badge variant="outline">知识点：{prediction.knowledge_basis.knowledge_points || 0} 个</Badge>
                  <Badge variant="outline">已审核关系：{prediction.knowledge_basis.cross_document_relations || 0} 条</Badge>
                  {prediction.interpretation?.analysis_model && <Badge variant="outline">模型：{analysisModels.find((model) => model.id === prediction.interpretation?.analysis_model)?.name || prediction.interpretation.analysis_model}</Badge>}
                </div>
                {prediction.knowledge_basis.confidence_note && <div className="flex gap-2 bg-slate-50 p-3 text-sm text-slate-700"><AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /><span>{prediction.knowledge_basis.confidence_note}</span></div>}
                {!!prediction.interpretation?.quality_warnings?.length && <div className="flex gap-2 bg-amber-50 p-3 text-sm text-amber-800"><AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /><span>系统已保留合格推演，并自动剔除 {prediction.interpretation.quality_warnings.length} 条不够具体的模型条目。</span></div>}

                {prediction.interpretation?.analysis_status === 'unavailable' ? (
                  <div className="border-l-4 border-red-500 bg-red-50 p-4">
                    <div className="flex gap-3"><AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-red-600" /><div><h4 className="font-medium text-red-900">本次没有形成可用推演</h4><p className="mt-1 text-sm text-red-800">{prediction.interpretation.analysis_error || '模型未返回足够具体的分析，系统已拒绝展示通用模板。'}</p></div></div>
                    <Button className="mt-3" size="sm" variant="outline" onClick={() => { const target = events.find((item) => item.id === prediction.knowledge_basis.event_id); if (target) predictEvent(target) }}>重新深度分析</Button>
                  </div>
                ) : (
                  <>
                    {(prediction.knowledge_basis.knowledge_points || 0) === 0 && <div className="flex gap-2 bg-amber-50 p-3 text-sm text-amber-800"><AlertCircle className="h-4 w-4 shrink-0" />当前没有结构化知识点或正式关系，推演主要依据多篇来源正文，结论需要结合下方验证指标持续校验。</div>}

                    <section className="border-l-4 border-blue-600 bg-blue-50/50 p-4">
                      <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="font-semibold">核心研判</h4><Badge variant="outline">{prediction.interpretation?.current_phase || '阶段待判断'}</Badge></div>
                      <p className="mt-2 text-base font-medium leading-7 text-gray-900">{prediction.interpretation?.executive_judgment}</p>
                      <p className="mt-2 text-sm leading-6 text-gray-600">{prediction.interpretation?.event_summary}</p>
                    </section>

                    {prediction.interpretation?.signal_assessment && <section className="border-b pb-5"><div className="flex flex-wrap items-center gap-2"><h4 className="font-semibold">信号含义</h4><Badge>{prediction.interpretation.signal_assessment.label}</Badge></div><p className="mt-2 text-sm leading-6 text-gray-700">{prediction.interpretation.signal_assessment.meaning}</p><p className="mt-1 text-xs leading-5 text-gray-500">证据与分歧：{prediction.interpretation.signal_assessment.evidence}</p></section>}

                    <section>
                      <h4 className="mb-3 font-semibold">下一步事件推演</h4>
                      <div className="space-y-3">{(prediction.interpretation?.next_developments || []).map((item, index) => <article key={`${item.title}-${index}`} className="border-l-2 border-slate-300 py-1 pl-4"><div className="flex flex-wrap items-center gap-2"><Badge>{item.path || '基准'}路径</Badge><Badge variant="outline">{item.dimension || '综合'}</Badge><strong className="text-sm">{item.title}</strong><span className="text-xs text-gray-500">{item.timeframe || '时间待验证'} · 可能性{item.likelihood}</span></div>{item.mechanism && <p className="mt-2 text-sm leading-6 text-gray-700">发生机制：{item.mechanism}</p>}{item.trigger_condition && <p className="mt-1 text-xs text-emerald-700">触发条件：{item.trigger_condition}</p>}{item.invalidated_by && <p className="mt-1 text-xs text-red-700">失效条件：{item.invalidated_by}</p>}<p className="mt-1 text-xs text-gray-600">受影响对象：{item.affected_parties?.join('、') || '待识别'}</p><p className="mt-1 text-xs text-gray-500">依据：{item.basis}</p><p className="mt-1 text-xs text-blue-700">验证或推翻：{item.watch_for}</p></article>)}</div>
                    </section>

                    <section>
                      <h4 className="mb-3 font-semibold">行业与跨领域走向</h4>
                      <div className="grid gap-3 md:grid-cols-2">{(prediction.interpretation?.impact_assessments || []).map((item, index) => <article key={`${item.dimension}-${index}`} className="rounded border p-4"><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex gap-2"><Badge variant="outline">{item.dimension}</Badge>{item.direction && <Badge variant="secondary">{item.direction}</Badge>}</div><span className="text-xs text-gray-500">{item.horizon} · 可能性{item.likelihood}</span></div><h5 className="mt-3 font-medium leading-6">{item.conclusion}</h5><p className="mt-2 text-sm leading-6 text-blue-800">推演链：{item.mechanism}</p><p className="mt-2 text-xs text-gray-600">影响对象：{item.affected_parties.join('、')}</p><p className="mt-1 text-xs text-gray-500">事实依据：{item.evidence_basis}</p></article>)}</div>
                    </section>

                    <section className="grid gap-5 border-t pt-5 md:grid-cols-2">
                      <div><h4 className="font-semibold text-emerald-800">潜在机遇</h4><div className="mt-3 space-y-3">{(prediction.interpretation?.opportunities || []).map((item, index) => <article key={`${item.title}-${index}`} className="border-l-2 border-emerald-500 pl-3"><div className="flex flex-wrap gap-2"><h5 className="text-sm font-medium">{item.title}</h5>{item.opportunity_type && <Badge variant="outline">{item.opportunity_type}</Badge>}</div><p className="mt-1 text-xs leading-5 text-gray-600">受益者：{item.beneficiaries.join('、')}；{item.rationale}</p><p className="mt-1 text-xs text-emerald-700">成立条件：{item.entry_condition} · {item.horizon}</p>{item.watch_for && <p className="mt-1 text-xs text-blue-700">形成信号：{item.watch_for}</p>}{item.invalidated_by && <p className="mt-1 text-xs text-gray-500">消失条件：{item.invalidated_by}</p>}</article>)}</div></div>
                      <div><h4 className="font-semibold text-red-800">挑战与风险预测</h4><div className="mt-3 space-y-3">{(prediction.interpretation?.challenges || []).map((item, index) => <article key={`${item.title}-${index}`} className="border-l-2 border-red-500 pl-3"><div className="flex flex-wrap items-center gap-2"><h5 className="text-sm font-medium">{item.title}</h5><Badge variant="outline">{item.risk_type || '综合'}</Badge><Badge variant={item.risk_level === '高' ? 'destructive' : 'secondary'}>{item.risk_level || '待判断'}风险</Badge></div><p className="mt-1 text-xs leading-5 text-gray-600">风险对象：{item.exposed_parties.join('、')}；{item.rationale}</p>{item.trigger_condition && <p className="mt-1 text-xs text-gray-700">触发条件：{item.trigger_condition}</p>}<p className="mt-1 text-xs text-red-700">预警信号：{item.warning_signal} · {item.horizon}</p>{item.response && <p className="mt-1 text-xs text-blue-700">准备动作：{item.response}</p>}</article>)}</div></div>
                    </section>

                    {prediction.interpretation?.public_opinion && <section className="border-t pt-5"><div className="flex flex-wrap items-center gap-2"><h4 className="font-semibold">媒体舆情走向</h4><Badge variant="outline">{prediction.interpretation.public_opinion.sentiment}</Badge><Badge variant="secondary">{prediction.interpretation.public_opinion.trend}</Badge></div><p className="mt-2 text-sm leading-6 text-gray-700">{prediction.interpretation.public_opinion.current_direction}</p><div className="mt-3 grid gap-3 text-xs md:grid-cols-2"><div><strong>主要观点</strong><p className="mt-1 text-gray-600">{prediction.interpretation.public_opinion.key_views.join('；') || '暂无'}</p></div><div><strong>争议焦点</strong><p className="mt-1 text-gray-600">{prediction.interpretation.public_opinion.controversies.join('；') || '暂无'}</p></div><div><strong>转向触发</strong><p className="mt-1 text-gray-600">{prediction.interpretation.public_opinion.turning_triggers.join('；') || '暂无'}</p></div><div><strong>监测关键词</strong><p className="mt-1 text-gray-600">{prediction.interpretation.public_opinion.watch_keywords.join('、') || '暂无'}</p></div></div><p className="mt-3 text-xs text-gray-500">数据范围：{prediction.interpretation.public_opinion.data_scope}</p></section>}

                    <section className="grid gap-5 border-t pt-5 text-sm md:grid-cols-2"><div><h4 className="mb-2 font-semibold">推动因素</h4><ul className="list-disc space-y-1 pl-5 text-gray-600">{(prediction.interpretation?.drivers || []).map((item) => <li key={item}>{item}</li>)}</ul></div><div><h4 className="mb-2 font-semibold">推演失效风险</h4><ul className="list-disc space-y-1 pl-5 text-gray-600">{(prediction.interpretation?.risks || []).map((item) => <li key={item}>{item}</li>)}</ul></div></section>

                    <section className="border-t pt-5"><h4 className="font-semibold">下一步跟踪指标</h4><div className="mt-2 flex flex-wrap gap-2">{(prediction.interpretation?.watch_indicators || []).map((item) => <Badge key={item} variant="outline" className="whitespace-normal text-left font-normal">{item}</Badge>)}</div>{prediction.interpretation?.decision_value && <div className="mt-4 bg-slate-50 p-3 text-sm"><strong>决策用途：{prediction.interpretation.decision_value.category}</strong><p className="mt-1 text-gray-600">{prediction.interpretation.decision_value.explanation}</p></div>}</section>

                    <details className="border-t pt-4"><summary className="cursor-pointer text-sm font-medium">查看参与判断的知识证据</summary><div className="mt-3 space-y-3 text-sm text-gray-600"><div><p className="font-medium text-gray-800">来源文章</p>{prediction.knowledge_basis.evidence_titles?.map((title) => <p key={title} className="mt-1">· {title}</p>)}</div><div><p className="font-medium text-gray-800">结构化知识点</p>{prediction.knowledge_basis.knowledge_point_details?.map((point, index) => <div key={`${point.title}-${index}`} className="mt-2 border-l-2 pl-3"><div className="font-medium text-gray-800">{point.title} <span className="text-xs text-gray-500">（{categoryLabels[point.category] || point.category}）</span></div><p className="mt-1">{point.content}</p>{point.evidence && <p className="mt-1 text-xs text-gray-500">原文依据：{point.evidence}</p>}</div>)}</div>{(prediction.knowledge_basis.relation_details || []).length > 0 && <div><p className="font-medium text-gray-800">已审核关系</p>{prediction.knowledge_basis.relation_details?.map((relation, index) => <div key={`${relation.source}-${relation.target}-${index}`} className="mt-2 border-l-2 pl-3"><p className="font-medium text-gray-800">{relation.source} → {relation.target} <span className="text-xs text-gray-500">（{relationLabels[relation.type] || relation.type}）</span></p>{relation.evidence && <p className="mt-1 text-xs text-gray-500">关系依据：{relation.evidence}</p>}</div>)}</div>}</div></details>
                  </>
                )}
              </CardContent>
            </Card>
          )}
        </TabsContent>

        <TabsContent value="synthesis" className="space-y-4">
          <Card>
            <CardHeader><CardTitle>知识综合</CardTitle><p className="text-sm text-gray-500">系统自动选择候选事件的来源文档和知识点，生成可复用的知识草稿。发布前必须人工审核。</p></CardHeader>
            <CardContent><Button onClick={createAutoSynthesis} disabled={loadingSynthesis}>{loadingSynthesis ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <FileText className="mr-2 h-4 w-4" />}自动发现并生成草稿</Button></CardContent>
          </Card>
          {syntheses.map((synthesis) => <Card key={synthesis.id}><CardHeader><div className="flex flex-wrap items-center justify-between gap-3"><div><CardTitle>{synthesis.title}</CardTitle><p className="mt-1 text-sm text-gray-500">来源 {synthesis.source_document_ids.length} 篇 · 知识声明 {synthesis.source_claim_ids.length} 条</p></div><Badge variant={synthesis.status === 'published' ? 'default' : 'secondary'}>{synthesis.status}</Badge></div></CardHeader><CardContent><p className="text-sm text-gray-600">{synthesis.summary}</p>{(synthesis.status === 'draft' || synthesis.status === 'review') && <div className="mt-3 flex gap-2"><Button onClick={() => reviewSynthesis(synthesis.id, 'approved')}><CheckCircle2 className="mr-2 h-4 w-4" />审核发布</Button><Button variant="outline" onClick={() => reviewSynthesis(synthesis.id, 'rejected')}>驳回</Button></div>}<details className="mt-3"><summary className="cursor-pointer text-sm font-medium">查看综合内容</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap rounded bg-gray-50 p-3 text-xs">{synthesis.content}</pre></details></CardContent></Card>)}
          {syntheses.length === 0 && <div className="rounded border border-dashed p-8 text-center text-sm text-gray-500">还没有综合草稿，点击上方按钮开始。</div>}
        </TabsContent>

        <TabsContent value="candidates" className="space-y-4">
          <Card>
            <CardHeader className="flex flex-row items-start justify-between gap-4">
              <div>
                <CardTitle>跨文档关联审核</CardTitle>
                <p className="mt-1 text-sm text-gray-500">系统通过知识点之间的标题相似度、内容相似度和关键词重叠度，发现跨文档候选关联。可以批准成立关联，或驳回误匹配。</p>
              </div>
              <Button variant="outline" onClick={loadCandidates} disabled={loadingCandidates}>
                <RefreshCw className={`mr-2 h-4 w-4 ${loadingCandidates ? 'animate-spin' : ''}`} />刷新
              </Button>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-sm text-gray-500">待审核 {associationCandidates.length} 条候选关联</p>
              {associationCandidates.length === 0 && !loadingCandidates && (
                <div className="rounded border border-dashed p-8 text-center text-sm text-gray-500">当前没有待审核的候选关联。</div>
              )}
              {associationCandidates.map((candidate) => {
                const sourceKp = candidatesKnowledgePoints[candidate.source_id]
                const targetKp = candidatesKnowledgePoints[candidate.target_id]
                const isConflict = candidate.candidate_type === 'conflict_candidate'
                const isDuplicate = candidate.candidate_type === 'duplicate_candidate'
                return (
                  <Card key={candidate.id} className={`border-l-4 ${isConflict ? 'border-l-red-400' : isDuplicate ? 'border-l-amber-400' : 'border-l-blue-400'}`}>
                    <CardContent className="pt-4">
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2 mb-2">
                            <Badge variant={isConflict ? 'destructive' : isDuplicate ? 'secondary' : 'outline'}>
                              {isConflict ? '矛盾候选' : isDuplicate ? '重复候选' : '相关候选'}
                            </Badge>
                            <Badge variant="outline">强度 {Math.round(candidate.strength * 100)}%</Badge>
                            <Badge variant="outline">{candidate.candidate_type.replace('_candidate', '')}</Badge>
                          </div>
                          <div className="flex items-center gap-2 text-sm">
                            <Link2 className="h-4 w-4 shrink-0 text-gray-400" />
                            <span className="font-medium">{sourceKp?.title || candidate.source_id.slice(0, 16)}</span>
                            <span className="text-gray-400">→</span>
                            <span className="font-medium">{targetKp?.title || candidate.target_id.slice(0, 16)}</span>
                          </div>
                          {candidate.evidence && (
                            <p className="mt-2 text-xs text-gray-500">{candidate.evidence}</p>
                          )}
                          {sourceKp && targetKp && (
                            <div className="mt-2 grid gap-2 text-xs text-gray-600 md:grid-cols-2">
                              <div className="rounded bg-gray-50 p-2">
                                <p className="font-medium text-gray-700">{sourceKp.title}</p>
                                <p className="mt-1 truncate">{sourceKp.content}</p>
                              </div>
                              <div className="rounded bg-gray-50 p-2">
                                <p className="font-medium text-gray-700">{targetKp.title}</p>
                                <p className="mt-1 truncate">{targetKp.content}</p>
                              </div>
                            </div>
                          )}
                        </div>
                        <div className="flex flex-col gap-2">
                          <Button
                            size="sm"
                            onClick={() => reviewCandidate(candidate, 'approved')}
                            disabled={reviewingCandidateId === candidate.id}
                          >
                            {reviewingCandidateId === candidate.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
                            <span className="ml-1">批准</span>
                          </Button>
                          <Button
                            size="sm"
                            variant="destructive"
                            onClick={() => reviewCandidate(candidate, 'rejected')}
                            disabled={reviewingCandidateId === candidate.id}
                          >
                            {reviewingCandidateId === candidate.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                            <span className="ml-1">驳回</span>
                          </Button>
                        </div>
                      </div>
                    </CardContent>
                  </Card>
                )
              })}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="feedback" className="space-y-4">
          <div className="rounded border border-slate-200 bg-slate-50 p-3 text-sm text-gray-700">反馈的用途：未来确认事件实际方向后回填“实际上升、实际稳定或实际下降”。系统据此计算方向一致度，帮助判断过去的规则是否可靠，不会把这次反馈直接当成新的事实。</div>
          <Card><CardHeader><CardTitle>预测反馈</CardTitle><p className="text-sm text-gray-500">预测结果不会自动变成事实。未来观察到实际结果后，请标记真实趋势，系统会统计命中率。</p></CardHeader><CardContent className="space-y-3">{history.map((item) => <div key={item.id} className="flex flex-wrap items-center justify-between gap-3 border-b pb-3 last:border-0"><div><p className="font-medium">{item.topic}</p><p className="text-xs text-gray-500">预测：{trendLabel(item.trend)} · 置信度 {Math.round(item.confidence * 100)}% {item.status === 'evaluated' ? `· 命中率 ${Math.round((item.accuracy_score || 0) * 100)}%` : ''}</p></div>{item.status !== 'evaluated' && <div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => submitFeedback(item.id, 'up')}>上升</Button><Button size="sm" variant="outline" onClick={() => submitFeedback(item.id, 'stable')}>稳定</Button><Button size="sm" variant="outline" onClick={() => submitFeedback(item.id, 'down')}>下降</Button></div>}</div>)}{history.length === 0 && <p className="py-8 text-center text-sm text-gray-500">完成一次交叉分析后，这里会出现预测记录。</p>}</CardContent></Card>
        </TabsContent>
      </Tabs>
    </main>
  )
}
