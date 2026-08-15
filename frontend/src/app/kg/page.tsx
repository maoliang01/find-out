"use client";

import { useState, useEffect, useRef, useCallback, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import * as d3 from "d3";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Network,
  Search,
  Play,
  RefreshCw,
  Loader2,
  AlertCircle,
  CheckCircle2,
  Filter,
  Trash2,
  Star,
  X,
  Route,
  BookOpen,
  Users,
  GitMerge,
  ScanSearch,
  ShieldCheck,
  BrainCircuit,
  Undo2,
  Clock3,
  Link2,
  Boxes,
  GitBranch,
  TrendingUp,
} from "lucide-react";
import { toast } from "sonner";
import EntitySourcePopover from "@/components/kg/EntitySourcePopover";

interface GraphNode {
  id: string;
  label: string;
  type: string;
  data: Record<string, any>;
}

interface GraphEdge {
  source: string;
  target: string;
  type: string;
  data: Record<string, any>;
}

interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

interface Stats {
  articles: number;          // Neo4j 中 Article 数
  articles_in_db?: number;   // SQLite 中 success 文章数
  drift_detected?: boolean;  // 数量不一致
  entities: number;
  orphan_entities?: number;
  articles_without_entities?: number;
  article_entity_links: number;
  entity_relations: number;
  entities_by_type?: Record<string, number>;
  entities_by_subtype?: Record<string, number>;
}

interface SyncStatus {
  by_status: {
    pending: number;
    processing: number;
    success: number;
    failed: number;
    partial: number;
    skipped: number;
  };
  total_in_db: number;
  total_in_kg: number;
  orphan_entities?: number;
  articles_without_entities?: number;
  drift_detected: boolean;
  failed_articles: Array<{ id: string; title: string; error?: string }>;
  partial_articles: Array<{ id: string; title: string; warning?: string }>;
  sync_state: {
    in_progress: boolean;
    active_count: number;
    total_processed: number;
    total_failed: number;
    batch_total?: number;
    batch_processed?: number;
    batch_failed?: number;
    batch_started_at?: string | null;
    started_at: string | null;
    last_finished_at: string | null;
  };
}

interface EntityProfile {
  entity: Record<string, unknown> & { name?: string; entity_type?: string; description?: string };
  article_count: number;
  articles: Array<{ id: string; title: string; url?: string }>;
  neighbors: Array<{
    name: string;
    entity_type?: string;
    rel_type: string;
    confidence?: number;
    support_count: number;
    source_articles: string[];
    provenance_status?: string;
  }>;
  evidence: Array<{
    claim_id: string;
    source: string;
    target: string;
    rel_type: string;
    evidence?: string;
    confidence?: number;
    article_id?: string;
    status?: string;
  }>;
}

interface KnowledgePath {
  length: number;
  nodes: Array<{ name: string; entity_type?: string }>;
  relationships: Array<{
    source: string;
    target: string;
    rel_type: string;
    confidence?: number;
    support_count: number;
    source_articles: string[];
    evidence_samples: string[];
    provenance_status?: string;
  }>;
}

interface EntityCommunity {
  id: string;
  label: string;
  size: number;
  article_count: number;
  internal_edges: number;
  density: number;
  members: Array<{ name: string; entity_type?: string; subtype?: string }>;
  entity_types: Record<string, number>;
  summary: string;
  core_entities: Array<{ name: string; score: number }>;
  bridge_entities: Array<{ name: string; external_connections: number }>;
}

interface CrossDocumentCandidate {
  source: string;
  source_type?: string;
  target: string;
  target_type?: string;
  shared_articles: string[];
  support_count: number;
  score: number;
}

interface AliasCandidate {
  left: { name: string; entity_type?: string };
  right: { name: string; entity_type?: string };
  score: number;
  shared_articles: string[];
  reasons: string[];
}

interface LegacyRelation {
  source: string;
  target: string;
  rel_type: string;
  confidence?: number;
}

interface MiningReview {
  id: string;
  review_type: "alias" | "cross_document" | "legacy_relation" | "inference" | "link_prediction" | "causal";
  source: string;
  target: string;
  rel_type?: string;
  decision: string;
  status?: "active" | "undone";
  reviewed_at?: string;
  undone_at?: string;
}

interface InferenceCandidate {
  source: string;
  target: string;
  rel_type: string;
  path: string[];
  hops: number;
  confidence: number;
  source_articles: string[];
  rule: string;
}

interface LinkPredictionCandidate {
  source: string;
  source_type?: string;
  target: string;
  target_type?: string;
  score: number;
  common_neighbors: string[];
  common_neighbor_count: number;
  jaccard: number;
  adamic_adar: number;
  reasons: string[];
}

interface TimelineEvent {
  name: string;
  subtype?: string;
  description?: string;
  observed_at?: string;
  date_markers: string[];
  articles: Array<{ id: string; title: string; url?: string; published_at?: string }>;
  temporal_relations: Array<{ target?: string; rel_type?: string; confidence?: number }>;
}

interface SimilarEntity {
  name: string;
  entity_type?: string;
  subtype?: string;
  score: number;
}

interface EntityRankingItem {
  name: string;
  entity_type: string;
  subtype?: string;
  occurrence_count: number;
  source_articles: string[];
}

interface CausalCandidate {
  source: string;
  target: string;
  rel_type: "causes" | "enables";
  confidence: number;
  support_count: number;
  source_articles: string[];
  evidence_samples: string[];
  markers: string[];
  discovery_sources?: string[];
}

interface CausalChain {
  nodes: Array<{ name: string; entity_type?: string; subtype?: string }>;
  relations: Array<{
    rel_type: "causes" | "enables";
    confidence?: number;
    source_articles: string[];
    evidence_samples: string[];
  }>;
  hops: number;
  confidence: number;
}

interface EmbeddingStatus {
  entity_count: number;
  embedded_count: number;
  coverage: number;
  dimensions: number;
  current_version?: string;
  stale: boolean;
  edge_count: number;
}

interface EmbeddingQuality {
  k: number;
  evaluated_entities: number;
  coverage: number;
  precision_at_k: number;
  recall_at_k: number;
  mean_neighbor_similarity: number;
}

const provenanceLabels: Record<string, string> = {
  evidence_backed: "正式证据",
  recovered_evidence: "恢复证据",
  reviewed_candidate: "审核共现",
  inferred_reviewed: "审核推理",
  prediction_reviewed: "审核预测",
  causal_reviewed: "审核因果",
  legacy_reviewed: "历史已审",
  legacy: "历史无证据",
};

function KnowledgeGraphPageContent() {
  const [graphData, setGraphData] = useState<GraphData>({ nodes: [], edges: [] });
  const [stats, setStats] = useState<Stats | null>(null);
  const [syncStatus, setSyncStatus] = useState<SyncStatus | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [health, setHealth] = useState<{ status: string; neo4j: { connected: boolean } } | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [entityTypeFilter, setEntityTypeFilter] = useState<string | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);
  const [highlightQuery, setHighlightQuery] = useState<string | null>(null);
  const [highlightedNodeIds, setHighlightedNodeIds] = useState<Set<string>>(new Set());
  const [popoverEntity, setPopoverEntity] = useState<string | null>(null);
  const [exploreOpen, setExploreOpen] = useState(false);
  const [exploreTab, setExploreTab] = useState("community");
  const [profileQuery, setProfileQuery] = useState("");
  const [entityProfile, setEntityProfile] = useState<EntityProfile | null>(null);
  const [pathSource, setPathSource] = useState("");
  const [pathTarget, setPathTarget] = useState("");
  const [pathDepth, setPathDepth] = useState(4);
  const [paths, setPaths] = useState<KnowledgePath[]>([]);
  const [communities, setCommunities] = useState<EntityCommunity[]>([]);
  const [crossDocumentCandidates, setCrossDocumentCandidates] = useState<CrossDocumentCandidate[]>([]);
  const [aliasCandidates, setAliasCandidates] = useState<AliasCandidate[]>([]);
  const [legacyRelations, setLegacyRelations] = useState<LegacyRelation[]>([]);
  const [miningReviews, setMiningReviews] = useState<MiningReview[]>([]);
  const [inferences, setInferences] = useState<InferenceCandidate[]>([]);
  const [linkPredictions, setLinkPredictions] = useState<LinkPredictionCandidate[]>([]);
  const [timelineEvents, setTimelineEvents] = useState<TimelineEvent[]>([]);
  const [timelineQuery, setTimelineQuery] = useState("");
  const [similarQuery, setSimilarQuery] = useState("");
  const [similarEntities, setSimilarEntities] = useState<SimilarEntity[]>([]);
  const [embeddingVersion, setEmbeddingVersion] = useState<string | null>(null);
  const [embeddingStatus, setEmbeddingStatus] = useState<EmbeddingStatus | null>(null);
  const [embeddingQuality, setEmbeddingQuality] = useState<EmbeddingQuality | null>(null);
  const [causalCandidates, setCausalCandidates] = useState<CausalCandidate[]>([]);
  const [causalSource, setCausalSource] = useState("");
  const [causalTarget, setCausalTarget] = useState("");
  const [causalChains, setCausalChains] = useState<CausalChain[]>([]);
  const [entityRanking, setEntityRanking] = useState<EntityRankingItem[]>([]);
  const [exploreLoading, setExploreLoading] = useState(false);

  const svgRef = useRef<SVGSVGElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const simulationRef = useRef<any>(null);
  const positionedNodesRef = useRef<Array<GraphNode & d3.SimulationNodeDatum>>([]);

  // 获取 URL 参数
  const searchParams = useSearchParams();
  const highlight = searchParams.get("highlight");

  // 加载图谱数据
  const loadGraphData = useCallback(async (limit = 500) => {
    setIsLoading(true);
    try {
      const response = await fetch(`/api/kg/graph?limit=${limit}`);
      const data = await response.json();
      if (data.status === "success") {
        setGraphData(data.data);
      }
    } catch (error) {
      console.error("加载图谱失败:", error);
    } finally {
      setIsLoading(false);
    }
  }, []);

  // 加载统计数据
  const loadStats = useCallback(async () => {
    try {
      const response = await fetch("/api/kg/stats");
      const data = await response.json();
      if (data.status === "success") {
        setStats(data.stats);
      }
    } catch (error) {
      console.error("加载统计失败:", error);
    }
  }, []);

  // 加载同步状态(各 kg_status 计数 + 后台任务进度)
  const loadSyncStatus = useCallback(async () => {
    try {
      const response = await fetch("/api/kg/sync-status");
      const data = await response.json();
      if (data.status === "success") {
        setSyncStatus(data);
      }
    } catch (error) {
      console.error("加载同步状态失败:", error);
    }
  }, []);

  // 加载健康状态
  const checkHealth = useCallback(async () => {
    try {
      const response = await fetch("/api/kg/health");
      const data = await response.json();
      setHealth(data);
    } catch (error) {
      console.error("健康检查失败:", error);
      setHealth({ status: "unhealthy", neo4j: { connected: false } });
    }
  }, []);

  // 处理高亮查询 - 根据文章标题查找相关节点
  const handleHighlight = useCallback(async (query: string) => {
    setHighlightQuery(query);
    if (!query || graphData.nodes.length === 0) return;

    // 直接在前端匹配文章标题
    const normalizedQuery = query.toLowerCase();
    const articleNodes = graphData.nodes.filter(
      (n) => n.type === "Article" && n.label.toLowerCase().includes(normalizedQuery)
    );

    if (articleNodes.length > 0) {
      // 找到文章节点后，查找所有关联的实体
      const articleIds = new Set(articleNodes.map((n) => n.id));
      const relatedEdges = graphData.edges.filter(
        (e) => articleIds.has(e.source) || articleIds.has(e.target)
      );
      const relatedNodeIds = new Set<string>();
      relatedEdges.forEach((e) => {
        relatedNodeIds.add(e.source);
        relatedNodeIds.add(e.target);
      });
      setHighlightedNodeIds(relatedNodeIds);
    }
  }, [graphData]);

  // 初始化
  useEffect(() => {
    checkHealth();
    loadStats();
    loadSyncStatus();
    loadGraphData();
  }, [checkHealth, loadStats, loadSyncStatus, loadGraphData]);

  // 同步状态轮询:每 5s 拉一次,后台抽取进度 + stats 同步显示
  // 后台有抽取任务时 → 抽取完毕(in_progress: true → false)自动刷新图谱
  const prevInProgressRef = useRef(false);
  useEffect(() => {
    const id = setInterval(() => {
      loadSyncStatus();
      loadStats();
    }, 5000);
    return () => clearInterval(id);
  }, [loadSyncStatus, loadStats]);

  // 抽取完成时自动刷新图谱(从 in_progress=true → false)
  useEffect(() => {
    if (!syncStatus) return;
    const wasInProgress = prevInProgressRef.current;
    const isInProgress = syncStatus.sync_state.in_progress;
    if (wasInProgress && !isInProgress) {
      loadGraphData();
    }
    prevInProgressRef.current = isInProgress;
  }, [syncStatus, loadGraphData]);

  // 处理 URL 参数中的 highlight
  useEffect(() => {
    if (highlight && graphData.nodes.length > 0) {
      handleHighlight(decodeURIComponent(highlight));
    }
  }, [highlight, graphData.nodes.length, handleHighlight]);

  // 清除高亮
  const clearHighlight = () => {
    setHighlightQuery(null);
    setHighlightedNodeIds(new Set());
  };

  // 搜索实体
  const handleSearch = async () => {
    if (!searchQuery.trim()) return;

    try {
      const response = await fetch(
        `/api/kg/search?query=${encodeURIComponent(searchQuery)}&limit=20`
      );
      const data = await response.json();
      if (data.status === "success") {
        setSearchResults(data.entities);
        // 将搜索结果作为高亮(D3 节点 id 与 Neo4j Entity.name 一致)
        const ids = new Set<string>(
          (data.entities as Array<{ name: string }>).map((e) => e.name)
        );
        setHighlightedNodeIds((prev) => {
          const next = new Set<string>(prev);
          ids.forEach((id) => next.add(id));
          return next;
        });
      }
    } catch (error) {
      console.error("搜索失败:", error);
    }
  };

  const loadEntityProfile = async (name = profileQuery) => {
    const entityName = name.trim();
    if (!entityName) return;
    setExploreLoading(true);
    try {
      const [profileResponse, similarResponse] = await Promise.all([
        fetch(`/api/kg/explore/entity-profile/${encodeURIComponent(entityName)}`),
        fetch(`/api/kg/mining/similar/${encodeURIComponent(entityName)}?limit=12&min_score=0&same_type=true`),
      ]);
      const profileData = await profileResponse.json();
      if (!profileResponse.ok) throw new Error(profileData.detail || "实体档案查询失败");
      setEntityProfile(profileData);
      setProfileQuery(entityName);
      setSimilarQuery(entityName);
      if (similarResponse.ok) {
        const similarData = await similarResponse.json();
        setSimilarEntities(similarData.results || []);
        setEmbeddingVersion(similarData.version || null);
      } else {
        setSimilarEntities([]);
      }
    } catch (error) {
      setEntityProfile(null);
      toast.error(error instanceof Error ? error.message : "实体档案查询失败");
    } finally {
      setExploreLoading(false);
    }
  };

  const findKnowledgePaths = async () => {
    if (!pathSource.trim() || !pathTarget.trim()) {
      toast.error("请输入起点和终点实体");
      return;
    }
    setExploreLoading(true);
    try {
      const response = await fetch("/api/kg/explore/path", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source: pathSource.trim(),
          target: pathTarget.trim(),
          max_depth: pathDepth,
          limit: 10,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "路径查询失败");
      setPaths(data.paths || []);
      if (!data.paths?.length) toast.info("指定深度内未发现连接路径");
    } catch (error) {
      setPaths([]);
      toast.error(error instanceof Error ? error.message : "路径查询失败");
    } finally {
      setExploreLoading(false);
    }
  };

  const loadMiningResults = async (mode: "community" | "cross-document" | "aliases" | "governance" | "inference" | "prediction" | "timeline" | "causal") => {
    setExploreLoading(true);
    try {
      if (mode === "governance") {
        const [legacyResponse, reviewResponse] = await Promise.all([
          fetch("/api/kg/mining/legacy-relations?limit=100"),
          fetch("/api/kg/mining/reviews?limit=100"),
        ]);
        const [legacyData, reviewData] = await Promise.all([
          legacyResponse.json(), reviewResponse.json(),
        ]);
        if (!legacyResponse.ok || !reviewResponse.ok) {
          throw new Error(legacyData.detail || reviewData.detail || "治理数据加载失败");
        }
        setLegacyRelations(legacyData.relations || []);
        setMiningReviews(reviewData.reviews || []);
        return;
      }
      const endpoints = {
        community: "/api/kg/mining/communities?min_size=3&limit=30",
        "cross-document": "/api/kg/mining/cross-document?min_shared_articles=2&limit=50",
        aliases: "/api/kg/mining/aliases?min_shared_articles=2&limit=50",
        inference: "/api/kg/mining/inferences?max_hops=3&limit=100",
        prediction: "/api/kg/mining/link-predictions?min_common_neighbors=2&min_score=0.2&limit=100",
        timeline: `/api/kg/mining/timeline?limit=500${timelineQuery.trim() ? `&query=${encodeURIComponent(timelineQuery.trim())}` : ""}`,
        causal: "/api/kg/mining/causal-candidates?limit=100",
      } as const;
      const endpoint = endpoints[mode];
      const response = await fetch(endpoint);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "知识挖掘失败");
      if (mode === "community") setCommunities(data.communities || []);
      if (mode === "cross-document") setCrossDocumentCandidates(data.candidates || []);
      if (mode === "aliases") setAliasCandidates(data.candidates || []);
      if (mode === "inference") setInferences(data.inferences || []);
      if (mode === "prediction") setLinkPredictions(data.predictions || []);
      if (mode === "timeline") setTimelineEvents(data.events || []);
      if (mode === "causal") setCausalCandidates(data.candidates || []);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "知识挖掘失败");
    } finally {
      setExploreLoading(false);
    }
  };

  const loadEntityRanking = async () => {
    setExploreLoading(true);
    try {
      const response = await fetch("/api/kg/explore/entity-ranking?limit=50");
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "实体排行加载失败");
      setEntityRanking(data.entities || []);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "实体排行加载失败");
    } finally {
      setExploreLoading(false);
    }
  };

  const loadDiscoveryOverview = async () => {
    setExploreLoading(true);
    try {
      const [communityResponse, rankingResponse] = await Promise.all([
        fetch("/api/kg/mining/communities?min_size=3&limit=30"),
        fetch("/api/kg/explore/entity-ranking?limit=20"),
      ]);
      const [communityData, rankingData] = await Promise.all([
        communityResponse.json(),
        rankingResponse.json(),
      ]);
      if (!communityResponse.ok || !rankingResponse.ok) {
        throw new Error(communityData.detail || rankingData.detail || "发现概览加载失败");
      }
      setCommunities(communityData.communities || []);
      setEntityRanking(rankingData.entities || []);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "发现概览加载失败");
    } finally {
      setExploreLoading(false);
    }
  };

  const changeExploreTab = (value: string) => {
    setExploreTab(value);
    if (value === "community" && communities.length === 0 && entityRanking.length === 0) loadDiscoveryOverview();
    if (value === "cross-document" && crossDocumentCandidates.length === 0) loadMiningResults("cross-document");
    if (value === "aliases" && aliasCandidates.length === 0) loadMiningResults("aliases");
    if (value === "governance" && legacyRelations.length === 0) loadMiningResults("governance");
    if (value === "inference" && inferences.length === 0) loadMiningResults("inference");
    if (value === "prediction" && linkPredictions.length === 0) loadMiningResults("prediction");
    if (value === "timeline" && timelineEvents.length === 0) loadMiningResults("timeline");
    if (value === "causal" && causalCandidates.length === 0) loadMiningResults("causal");
    if (value === "similar" && !embeddingStatus) loadEmbeddingDiagnostics();
    if (value === "ranking" && entityRanking.length === 0) loadEntityRanking();
  };

  const undoMiningReview = async (reviewId: string) => {
    setExploreLoading(true);
    try {
      const response = await fetch(`/api/kg/mining/reviews/${encodeURIComponent(reviewId)}/undo`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "撤销审核失败");
      toast.success("审核已撤销");
      await loadMiningResults("governance");
      loadGraphData();
      loadStats();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "撤销审核失败");
    } finally {
      setExploreLoading(false);
    }
  };

  const submitMiningReview = async (
    endpoint: string,
    payload: Record<string, string>,
    mode: "cross-document" | "aliases" | "governance" | "inference" | "prediction" | "causal",
  ) => {
    setExploreLoading(true);
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "审核操作失败");
      toast.success("审核结果已保存");
      await loadMiningResults(mode);
      loadStats();
      loadGraphData();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "审核操作失败");
    } finally {
      setExploreLoading(false);
    }
  };

  const loadEmbeddingDiagnostics = async () => {
    setExploreLoading(true);
    try {
      const [statusResponse, qualityResponse] = await Promise.all([
        fetch("/api/kg/mining/embeddings/status"),
        fetch("/api/kg/mining/embeddings/evaluate?k=5"),
      ]);
      const [statusData, qualityData] = await Promise.all([
        statusResponse.json(), qualityResponse.json(),
      ]);
      if (!statusResponse.ok) throw new Error(statusData.detail || "图嵌入状态加载失败");
      setEmbeddingStatus(statusData);
      setEmbeddingVersion(statusData.current_version || null);
      setEmbeddingQuality(qualityResponse.ok ? qualityData : null);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "图嵌入状态加载失败");
    } finally {
      setExploreLoading(false);
    }
  };

  const loadCausalChains = async () => {
    const params = new URLSearchParams({ max_hops: "4", limit: "100" });
    if (causalSource.trim()) params.set("source", causalSource.trim());
    if (causalTarget.trim()) params.set("target", causalTarget.trim());
    setExploreLoading(true);
    try {
      const response = await fetch(`/api/kg/mining/causal-chains?${params.toString()}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "因果链查询失败");
      setCausalChains(data.chains || []);
      if (!data.chains?.length) toast.info("没有找到已审核的因果链");
    } catch (error) {
      setCausalChains([]);
      toast.error(error instanceof Error ? error.message : "因果链查询失败");
    } finally {
      setExploreLoading(false);
    }
  };

  const generateEmbeddings = async () => {
    setExploreLoading(true);
    try {
      const response = await fetch("/api/kg/mining/embeddings/generate?dimensions=16", { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "图嵌入生成失败");
      setEmbeddingVersion(data.version || null);
      toast.success(`已更新 ${data.entity_count} 个实体向量`);
      await loadEmbeddingDiagnostics();
      if (similarQuery.trim()) await loadSimilarEntities();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "图嵌入生成失败");
    } finally {
      setExploreLoading(false);
    }
  };

  const loadSimilarEntities = async () => {
    const entityName = similarQuery.trim();
    if (!entityName) return;
    setExploreLoading(true);
    try {
      const response = await fetch(`/api/kg/mining/similar/${encodeURIComponent(entityName)}?limit=30&min_score=0&same_type=true`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "相似实体查询失败");
      setSimilarEntities(data.results || []);
      setEmbeddingVersion(data.version || null);
    } catch (error) {
      setSimilarEntities([]);
      toast.error(error instanceof Error ? error.message : "相似实体查询失败");
    } finally {
      setExploreLoading(false);
    }
  };

  // 批量处理文章
  const handleBatchProcess = async (limit = 50) => {
    setIsProcessing(true);
    try {
      const response = await fetch(`/api/kg/process-pending?limit=${limit}&include_failed=true&include_partial=true`, {
        method: "POST",
      });
      const data = await response.json();
      if (data.status === "success") {
        toast.success(
          `处理完成！成功: ${data.results.success}, 跳过: ${data.results.skipped}, 失败: ${data.results.failed}`
        );
        loadGraphData();
        loadStats();
        loadSyncStatus();
      }
    } catch (error) {
      console.error("批量处理失败:", error);
    } finally {
      setIsProcessing(false);
    }
  };

  // 获取实体详情
  const getEntityDetails = async (entityName: string) => {
    try {
      const response = await fetch(`/api/kg/entity/${encodeURIComponent(entityName)}`);
      const data = await response.json();
      if (data.status === "success") {
        return data.data;
      }
    } catch (error) {
      console.error("获取实体详情失败:", error);
    }
    return null;
  };

  // 节点点击处理
  const handleNodeClick = async (node: GraphNode) => {
    setSelectedNode(node);

    // 实体节点:打开出处弹窗(显示来源文章 + 跳转回 articles 页)
    if (node.type !== "Article") {
      setPopoverEntity(node.label);
      return;
    }

    // Article 节点:获取邻居实体信息
    try {
      const details = await getEntityDetails(node.label);
      if (details) {
        console.log("文章邻居实体:", details);
      }
    } catch (e) {
      console.error("获取文章详情失败:", e);
    }
  };

  // D3.js 可视化渲染
  useEffect(() => {
    if (!svgRef.current || !canvasRef.current || graphData.nodes.length === 0) return;

    const svgElement = svgRef.current;
    const canvas = canvasRef.current;
    const svg = d3.select(svgElement);
    const width = svgElement.clientWidth;
    const height = svgElement.clientHeight;
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
    const context = canvas.getContext("2d");
    if (!context || width === 0 || height === 0) return;

    canvas.width = Math.round(width * pixelRatio);
    canvas.height = Math.round(height * pixelRatio);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;

    // 清空已有内容
    svg.selectAll("*").remove();

    // 创建缩放容器
    const g = svg.append("g").attr("class", "kg-root");
    let currentTransform = d3.zoomIdentity;
    let frameId: number | null = null;
    let framePending = false;

    // D3 会把边的端点改成节点对象，克隆数据以避免污染 React state。
    let nodes: Array<GraphNode & d3.SimulationNodeDatum> = graphData.nodes.map((node) => ({
      ...node,
      data: { ...node.data },
    }));
    let edges: Array<GraphEdge & d3.SimulationLinkDatum<GraphNode & d3.SimulationNodeDatum>> =
      graphData.edges.map((edge) => ({ ...edge, data: { ...edge.data } }));

    if (entityTypeFilter) {
      nodes = nodes.filter((n) => n.type === entityTypeFilter);
      const nodeIds = new Set(nodes.map((n) => n.id));
      edges = edges.filter(
        (e) => nodeIds.has(String(e.source)) && nodeIds.has(String(e.target))
      );
    }
    positionedNodesRef.current = nodes;

    // 节点颜色映射
    // - 顶层用 entity_type 区分(PERSON/TECHNOLOGY 等)
    // - 同一 type 下的不同 subtype 用同色系深浅区分(SCIENTIST 深,ENGINEER 浅)
    const colorMap: Record<string, string> = {
      Article: "#4f46e5",
      Entity: "#10b981",
      PERSON: "#f59e0b",
      ORGANIZATION: "#3b82f6",
      LOCATION: "#8b5cf6",
      TECHNOLOGY: "#ec4899",
      EVENT: "#ef4444",
      CONCEPT: "#06b6d4",
      DATE: "#64748b",
    };

    // subtype 颜色变体(在主色基础上做深浅)
    const subtypeShade = (baseColor: string, idx: number): string => {
      // idx 0 = 主色; 1 = 浅; 2 = 深; 3 = 更浅
      const shades: Record<number, string> = {
        0: baseColor,
        1: baseColor + "cc",
        2: baseColor + "88",
        3: baseColor + "55",
      };
      return shades[idx % 4] || baseColor;
    };

    // 节点填色:有 subtype 时按 subtype 哈希到 shade,无 subtype 用主色
    const subtypeIndex: Record<string, number> = {};
    let subtypeCounter = 0;
    for (const n of nodes) {
      if (n.type !== "Article") {
        const st = (n.data as any)?.subtype;
        if (st && !(st in subtypeIndex)) {
          subtypeIndex[st] = subtypeCounter++ % 4;
        }
      }
    }
    const nodeFill = (d: GraphNode): string => {
      const base = colorMap[d.type] || "#64748b";
      const st = (d.data as any)?.subtype;
      if (st && subtypeIndex[st] !== undefined) {
        return subtypeShade(base, subtypeIndex[st]);
      }
      return base;
    };

    const degree = new Map<string, number>();
    edges.forEach((edge) => {
      const source = String(edge.source);
      const target = String(edge.target);
      degree.set(source, (degree.get(source) || 0) + 1);
      degree.set(target, (degree.get(target) || 0) + 1);
    });
    const labelledNodeIds = new Set(
      [...nodes]
        .sort((left, right) => (degree.get(right.id) || 0) - (degree.get(left.id) || 0))
        .slice(0, Math.min(90, nodes.length))
        .map((node) => node.id)
    );

    // 连线用 Canvas 一次性绘制，节点保留 SVG 交互。
    const drawEdges = () => {
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      context.clearRect(0, 0, width, height);
      context.save();
      context.translate(currentTransform.x, currentTransform.y);
      context.scale(currentTransform.k, currentTransform.k);
      context.beginPath();
      for (const edge of edges) {
        const source = edge.source as GraphNode & d3.SimulationNodeDatum;
        const target = edge.target as GraphNode & d3.SimulationNodeDatum;
        if (source.x == null || source.y == null || target.x == null || target.y == null) continue;
        context.moveTo(source.x, source.y);
        context.lineTo(target.x, target.y);
      }
      context.strokeStyle = "rgba(148, 163, 184, 0.55)";
      context.lineWidth = 1.25 / Math.max(currentTransform.k, 0.5);
      context.stroke();
      context.restore();
    };

    let drawNodePositions = () => undefined;
    const scheduleFrame = () => {
      if (framePending) return;
      framePending = true;
      frameId = window.requestAnimationFrame(() => {
        framePending = false;
        drawEdges();
        drawNodePositions();
      });
    };

    const zoom = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.1, 4])
      .on("zoom", (event) => {
        currentTransform = event.transform;
        g.attr("transform", currentTransform.toString());
        scheduleFrame();
      });
    svg.call(zoom);

    const simulation = d3.forceSimulation(nodes)
      .alphaDecay(0.055)
      .alphaMin(0.025)
      .velocityDecay(0.48)
      .force(
        "link",
        d3.forceLink<GraphNode & d3.SimulationNodeDatum, typeof edges[number]>(edges)
          .id((node) => node.id)
          .distance(82)
          .strength(0.14)
      )
      .force(
        "charge",
        d3.forceManyBody<GraphNode & d3.SimulationNodeDatum>()
          .strength(nodes.length > 350 ? -115 : -170)
          .distanceMax(900)
          .theta(0.9)
      )
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force(
        "collision",
        d3.forceCollide<GraphNode & d3.SimulationNodeDatum>()
          .radius((node) => node.type === "Article" ? 19 : 14)
          .strength(0.65)
          .iterations(1)
      );

    simulationRef.current = simulation;

    // 绘制节点
    const node = g
      .append("g")
      .selectAll("circle")
      .data(nodes)
      .enter()
      .append("circle")
      .attr("class", "kg-node")
      .attr("r", (d: GraphNode) => d.type === "Article" ? 12 : 8)
      .attr("fill", (d: GraphNode) => nodeFill(d))
      .attr("stroke", "#fff")
      .attr("stroke-width", (d: GraphNode) => highlightedNodeIds.has(d.id) ? 4 : 2)
      .attr("stroke-opacity", (d: GraphNode) => highlightedNodeIds.has(d.id) ? 1 : 0.6)
      .style("cursor", "pointer")
      .style("filter", (d: GraphNode) => highlightedNodeIds.has(d.id) ? "drop-shadow(0 0 8px rgba(79, 70, 229, 0.8))" : "none")
      .on("click", (event, d: GraphNode) => {
        event.stopPropagation();
        handleNodeClick(d);
      });

    node.append("title").text((node) => node.label);

    // 添加标签(实体节点带 subtype 时,后面括号标注中文)
    const label = g
      .append("g")
      .attr("class", "kg-labels")
      .selectAll("text")
      .data(nodes.filter((node) => labelledNodeIds.has(node.id)))
      .enter()
      .append("text")
      .attr("class", "kg-label")
      .text((d: GraphNode) => {
        const st = d.data?.subtype as string | undefined;
        if (st && d.type !== "Article") {
          const stLabel = SUBTYPE_LABELS[st] || st;
          return `${d.label.substring(0, 12)}[${stLabel}]`;
        }
        return d.label.substring(0, 20);
      })
      .attr("font-size", 10)
      .attr("fill", "#475569")
      .attr("text-anchor", "middle")
      .attr("dy", 20)
      .style("pointer-events", "none")
      .style("opacity", 0);

    g.append("g").attr("class", "kg-highlight-labels");

    // 拖拽事件
    const drag = d3.drag<SVGCircleElement, GraphNode & d3.SimulationNodeDatum>()
      .on("start", (event, d) => {
        if (!event.active) simulation.alpha(0.16).alphaTarget(0.06).restart();
        d.fx = d.x;
        d.fy = d.y;
      })
      .on("drag", (event, d) => {
        d.fx = event.x;
        d.fy = event.y;
      })
      .on("end", (event, d) => {
        if (!event.active) simulation.alphaTarget(0);
        d.fx = null;
        d.fy = null;
      });

    node.call(drag);

    drawNodePositions = () => {
      node.attr("cx", (d) => d.x || 0).attr("cy", (d) => d.y || 0);
      label.attr("x", (d) => d.x || 0).attr("y", (d) => d.y || 0);
      g.select<SVGGElement>(".kg-highlight-labels")
        .selectAll<SVGTextElement, GraphNode & d3.SimulationNodeDatum>("text")
        .attr("x", (node) => node.x || 0)
        .attr("y", (node) => node.y || 0);
    };

    simulation.on("tick", () => {
      scheduleFrame();
    });
    simulation.on("end", () => {
      scheduleFrame();
      label.style("opacity", 1);
    });

    return () => {
      simulation.stop();
      if (frameId !== null) window.cancelAnimationFrame(frameId);
      context.setTransform(1, 0, 0, 1, 0, 0);
      context.clearRect(0, 0, canvas.width, canvas.height);
      positionedNodesRef.current = [];
    };
  }, [graphData, entityTypeFilter]);

  // 高亮只更新样式和少量标签，不销毁布局或重启模拟。
  useEffect(() => {
    if (!svgRef.current) return;
    const svg = d3.select(svgRef.current);
    svg.selectAll<SVGCircleElement, GraphNode>("circle.kg-node")
      .attr("stroke-width", (node) => highlightedNodeIds.has(node.id) ? 4 : 2)
      .attr("stroke-opacity", (node) => highlightedNodeIds.has(node.id) ? 1 : 0.6)
      .style("filter", (node) => highlightedNodeIds.has(node.id)
        ? "drop-shadow(0 0 8px rgba(79, 70, 229, 0.8))"
        : "none");

    const highlightedNodes = positionedNodesRef.current.filter((node) => highlightedNodeIds.has(node.id));
    svg.select<SVGGElement>("g.kg-highlight-labels")
      .selectAll<SVGTextElement, GraphNode & d3.SimulationNodeDatum>("text")
      .data(highlightedNodes, (node) => node.id)
      .join("text")
      .attr("class", "kg-highlight-label")
      .attr("x", (node) => node.x || 0)
      .attr("y", (node) => node.y || 0)
      .attr("dy", -15)
      .attr("text-anchor", "middle")
      .attr("font-size", 11)
      .attr("font-weight", 600)
      .attr("fill", "#312e81")
      .style("pointer-events", "none")
      .text((node) => node.label.substring(0, 24));
  }, [highlightedNodeIds]);

  // 实体类型中英文映射
  const ENTITY_TYPE_LABELS: Record<string, string> = {
    Article: "文章",
    PERSON: "人物",
    ORGANIZATION: "组织",
    LOCATION: "地点",
    TECHNOLOGY: "技术",
    EVENT: "事件",
    CONCEPT: "概念",
    DATE: "时间",
  };

  const SUBTYPE_LABELS: Record<string, string> = {
    SCIENTIST: "科学家",
    ENGINEER: "工程师",
    ACADEMIC: "学者",
    LEADER: "领导",
    ENTREPRENEUR: "企业家",
    WRITER: "作家",
    ARTIST: "艺术家",
    HISTORICAL: "历史人物",
    COMPANY: "公司",
    RESEARCH_INST: "研究机构",
    UNIVERSITY: "大学",
    GOVERNMENT: "政府",
    INTERNATIONAL: "国际组织",
    NGO: "NGO",
    CITY: "城市",
    COUNTRY: "国家",
    REGION: "地区",
    BUILDING: "建筑",
    ASTRONOMICAL: "天文",
    NATURAL: "自然",
    AI_MODEL: "AI 模型",
    ALGORITHM: "算法",
    PRODUCT: "产品",
    LANGUAGE: "编程语言",
    FRAMEWORK: "框架",
    TOOL: "工具",
    MATERIAL: "材料",
    BIOTECH: "生物技术",
    ENERGY: "能源",
    DEVICE: "设备",
    DISCOVERY: "发现",
    CONFERENCE: "会议",
    PUBLICATION: "出版物",
    AWARD: "奖项",
    AGREEMENT: "协议",
    DISASTER: "灾害",
    CONFLICT: "冲突",
    THEORY: "理论",
    LAW: "定律",
    METHOD: "方法",
    MODEL: "模型",
    SYSTEM: "系统",
    IDEA: "思想",
    DISCIPLINE: "学科",
    FIELD: "领域",
    YEAR: "年",
    MONTH: "月",
    DAY: "日",
    ERA: "时代",
    PERIOD: "时期",
    OTHER: "其他",
  };

  // 获取唯一实体类型(带中文显示)
  const entityTypes = [...new Set(graphData.nodes.map((n) => n.type))];

  return (
    <div className="flex h-[calc(100vh-4rem)]">
      {/* 左侧边栏 */}
      <div className="w-80 border-r bg-gray-50/50 flex flex-col">
        <div className="p-4 border-b">
          <div className="flex items-center justify-between">
            <h1 className="text-lg font-semibold flex items-center gap-2">
              <Network className="w-5 h-5" />
              知识图谱
            </h1>
            {highlightQuery && (
              <Button size="sm" variant="ghost" onClick={clearHighlight}>
                <X className="w-4 h-4" />
              </Button>
            )}
          </div>
          {highlightQuery && (
            <div className="mt-2 flex items-center gap-2 text-sm text-indigo-600 bg-indigo-50 px-3 py-2 rounded-lg">
              <Star className="w-4 h-4" />
              <span className="truncate">{highlightQuery}</span>
            </div>
          )}
        </div>

        {/* 统计信息 */}
        {stats && (
          <div className="p-4 border-b">
            {/* 一致性同步(对账) */}
            <div className="mb-3 flex items-center gap-2 text-sm">
              <span className="text-gray-500">文档管理</span>
              <span className="font-semibold">{stats.articles_in_db ?? "-"}</span>
              <span className="text-gray-400">/</span>
              <span className="text-gray-500">图谱</span>
              <span className="font-semibold">{stats.articles}</span>
              <span className="text-gray-400">篇</span>
              {stats.drift_detected && (
                <span className="text-xs bg-red-100 text-red-700 px-2 py-0.5 rounded">漂移</span>
              )}
              <button
                onClick={async () => {
                  if (!confirm("是否自动修复漂移?点确定将自动补抽缺失文章、删除孤儿。")) return;
                  try {
                    const res = await fetch("/api/kg/reconcile?apply=true", { method: "POST" });
                    const data = await res.json();
                    if (!res.ok) {
                      throw new Error(data.detail || "对账接口执行失败");
                    }
                    const cleanupRes = await fetch("/api/kg/cleanup-orphans", { method: "POST" });
                    const cleanupData = await cleanupRes.json();
                    if (!cleanupRes.ok) {
                      throw new Error(cleanupData.detail || "孤立实体清理失败");
                    }
                    alert(
                      `对账结果:\n` +
                      `  文档管理: ${data.sqlite_count}\n` +
                      `  图谱: ${data.kg_count}\n` +
                      `  缺失: ${data.missing_in_kg.length}\n` +
                      `  孤儿: ${data.orphan_in_kg.length}\n` +
                      `  脏数据: ${data.dirty_in_kg.length}` +
                      (data.fixed ? `\n已修复: ${JSON.stringify(data.fixed)}` : "")
                    );
                    loadStats();
                    loadSyncStatus();
                    loadGraphData();
                  } catch (e) {
                    alert("对账失败: " + e);
                  }
                }}
                className="ml-auto text-xs bg-indigo-50 text-indigo-700 hover:bg-indigo-100 px-2 py-1 rounded border border-indigo-200"
              >
                对账
              </button>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="bg-white p-3 rounded-lg">
                <div className="text-2xl font-bold text-indigo-600">
                  {stats.articles}
                </div>
                <div className="text-xs text-gray-500">文章</div>
              </div>
              <div className="bg-white p-3 rounded-lg">
                <div className="text-2xl font-bold text-emerald-600">
                  {stats.entities}
                </div>
                <div className="text-xs text-gray-500">实体</div>
              </div>
              <div className="bg-white p-3 rounded-lg">
                <div className="text-2xl font-bold text-blue-600">
                  {stats.article_entity_links}
                </div>
                <div className="text-xs text-gray-500">文章-实体</div>
              </div>
              <div className="bg-white p-3 rounded-lg">
                <div className="text-2xl font-bold text-purple-600">
                  {stats.entity_relations}
                </div>
                <div className="text-xs text-gray-500">实体关系</div>
              </div>
            </div>

            {/* 实体按类型细分 */}
            {stats.entities_by_type && Object.keys(stats.entities_by_type).length > 0 && (
              <div className="mt-3">
                <div className="text-[10px] text-gray-500 mb-1">实体类型分布</div>
                <div className="space-y-1">
                  {Object.entries(stats.entities_by_type)
                    .sort(([, a], [, b]) => b - a)
                    .map(([t, c]) => {
                      const pct = stats.entities ? (c / stats.entities) * 100 : 0;
                      const color =
                        t === "PERSON" ? "#f59e0b" :
                        t === "ORGANIZATION" ? "#3b82f6" :
                        t === "LOCATION" ? "#8b5cf6" :
                        t === "TECHNOLOGY" ? "#ec4899" :
                        t === "EVENT" ? "#ef4444" :
                        t === "CONCEPT" ? "#06b6d4" :
                        t === "DATE" ? "#64748b" : "#10b981";
                      const label = ENTITY_TYPE_LABELS[t] || t;
                      return (
                        <div key={t} className="flex items-center gap-2 text-xs">
                          <div className="w-2 h-2 rounded-full shrink-0" style={{ background: color }} />
                          <span className="text-gray-700 w-20 truncate">{label}</span>
                          <div className="flex-1 h-1.5 bg-gray-100 rounded overflow-hidden">
                            <div className="h-full" style={{ width: `${pct}%`, background: color }} />
                          </div>
                          <span className="text-gray-500 w-8 text-right">{c}</span>
                        </div>
                      );
                    })}
                </div>
              </div>
            )}
          </div>
        )}

        {/* 同步状态面板(各 kg_status 计数 + 后台任务进度) */}
        {syncStatus && (
          <div className="p-4 border-b">
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium">同步状态</span>
              {syncStatus.sync_state.in_progress ? (
                <span className="flex items-center gap-1 text-xs bg-amber-50 text-amber-700 px-2 py-0.5 rounded border border-amber-200">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  抽取中 ({syncStatus.sync_state.active_count})
                </span>
              ) : (
                <span className="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded">
                  空闲
                </span>
              )}
            </div>
            {(syncStatus.sync_state.batch_total ?? 0) > 0 && (
              <div className="mb-3 rounded-lg border border-indigo-100 bg-indigo-50/60 p-3">
                <div className="mb-1 flex items-center justify-between text-xs text-indigo-900">
                  <span>图谱处理进度</span>
                  <span className="font-semibold">
                    {Math.min(
                      100,
                      Math.round(((syncStatus.sync_state.batch_processed ?? 0) + (syncStatus.sync_state.batch_failed ?? 0)) /
                        Math.max(1, syncStatus.sync_state.batch_total ?? 1) * 100),
                    )}%
                  </span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-indigo-100">
                  <div
                    className="h-full rounded-full bg-indigo-600 transition-all duration-500"
                    style={{
                      width: `${Math.min(
                        100,
                        (((syncStatus.sync_state.batch_processed ?? 0) + (syncStatus.sync_state.batch_failed ?? 0)) /
                          Math.max(1, syncStatus.sync_state.batch_total ?? 1)) * 100,
                      )}%`,
                    }}
                  />
                </div>
                <div className="mt-1 flex justify-between text-[10px] text-indigo-700">
                  <span>
                    已完成 {syncStatus.sync_state.batch_processed ?? 0} / {syncStatus.sync_state.batch_total}
                  </span>
                  <span>
                    处理中 {syncStatus.sync_state.active_count}
                    {(syncStatus.sync_state.batch_failed ?? 0) > 0 && ` · 失败 ${syncStatus.sync_state.batch_failed}`}
                  </span>
                </div>
              </div>
            )}
            <div className="grid grid-cols-6 gap-1 text-center text-xs">
              <div className="bg-white p-2 rounded">
                <div className="text-base font-semibold text-emerald-600">
                  {syncStatus.by_status.success}
                </div>
                <div className="text-gray-500">已入图</div>
              </div>
              <div className="bg-white p-2 rounded">
                <div className="text-base font-semibold text-amber-600">
                  {syncStatus.by_status.pending}
                </div>
                <div className="text-gray-500">待抽</div>
              </div>
              <div className="bg-white p-2 rounded">
                <div className="text-base font-semibold text-blue-600">
                  {syncStatus.by_status.processing}
                </div>
                <div className="text-gray-500">抽中</div>
              </div>
              <div className="bg-white p-2 rounded">
                <div className="text-base font-semibold text-red-600">
                  {syncStatus.by_status.failed}
                </div>
                <div className="text-gray-500">失败</div>
              </div>
              <div className="bg-white p-2 rounded">
                <div className="text-base font-semibold text-orange-600">
                  {syncStatus.by_status.partial}
                </div>
                <div className="text-gray-500">部分</div>
              </div>
              <div className="bg-white p-2 rounded">
                <div className="text-base font-semibold text-gray-500">
                  {syncStatus.by_status.skipped}
                </div>
                <div className="text-gray-500">跳过</div>
              </div>
            </div>
            {syncStatus.sync_state.total_processed > 0 && (
              <div className="mt-2 text-[10px] text-gray-500 leading-tight">
                本会话已处理 {syncStatus.sync_state.total_processed} 篇
                {syncStatus.sync_state.total_failed > 0 &&
                  `,失败 ${syncStatus.sync_state.total_failed} 篇`}
                {syncStatus.sync_state.last_finished_at &&
                  ` · 上次完成 ${new Date(syncStatus.sync_state.last_finished_at).toLocaleTimeString("zh-CN")}`}
              </div>
            )}
            {(syncStatus.by_status.pending > 0 || syncStatus.by_status.skipped > 0 || syncStatus.by_status.failed > 0) && (
              <Button
                size="sm"
                className="w-full mt-2 h-7 text-xs bg-indigo-600 hover:bg-indigo-700"
                disabled={syncStatus.sync_state.in_progress}
                onClick={async () => {
                  const retryCount = syncStatus.by_status.pending + syncStatus.by_status.skipped + syncStatus.by_status.failed;
                  if (!confirm(`立即处理 ${retryCount} 篇待处理或失败文章？`)) return;
                  try {
                    const res = await fetch("/api/kg/process-pending?limit=200", { method: "POST" });
                    const data = await res.json();
                    if (data.status === "success") {
                      toast.success(`已排入 ${data.scheduled} 个抽取任务(扫描 ${data.scanned} 篇)`);
                      setTimeout(() => loadSyncStatus(), 500);
                    } else {
                      toast.error(data.detail || data.error || "启动失败");
                    }
                  } catch (e) {
                    toast.error("启动失败: " + (e instanceof Error ? e.message : String(e)));
                  }
                }}
              >
                {syncStatus.sync_state.in_progress ? (
                  <><Loader2 className="w-3 h-3 mr-1 animate-spin" />抽取中</>
                ) : (
                  <><Play className="w-3 h-3 mr-1" />立即处理 ({syncStatus.by_status.pending + syncStatus.by_status.skipped + syncStatus.by_status.failed})</>
                )}
              </Button>
            )}
            {syncStatus.failed_articles?.length > 0 && (
              <div className="mt-2 space-y-1">
                {syncStatus.failed_articles.slice(0, 3).map((article) => (
                  <div key={article.id} className="border-l-2 border-red-300 pl-2 text-[10px] text-gray-600">
                    <div className="truncate font-medium">{article.title || article.id}</div>
                    <div className="line-clamp-2 text-red-600">{article.error || "知识抽取失败"}</div>
                  </div>
                ))}
              </div>
            )}
            {syncStatus.partial_articles?.length > 0 && (
              <div className="mt-2 space-y-1">
                {syncStatus.partial_articles.slice(0, 3).map((article) => (
                  <div key={article.id} className="border-l-2 border-orange-300 pl-2 text-[10px] text-gray-600">
                    <div className="truncate font-medium">{article.title || article.id}</div>
                    <div className="line-clamp-2 text-orange-700">{article.warning || "已使用关键词降级提取"}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* 搜索 */}
        <div className="p-4 border-b">
          <div className="flex gap-2">
            <Input
              placeholder="搜索实体..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            />
            <Button size="icon" onClick={handleSearch}>
              <Search className="w-4 h-4" />
            </Button>
          </div>

          {searchResults.length > 0 && (
            <div className="mt-3 space-y-2">
              {searchResults.slice(0, 5).map((entity, idx) => (
                <div
                  key={idx}
                  className="bg-white p-2 rounded cursor-pointer hover:bg-gray-100"
                  onClick={() => {
                    setSelectedNode({
                      id: entity.name,
                      label: entity.name,
                      type: entity.entity_type,
                      data: entity,
                    });
                  }}
                >
                  <div className="font-medium text-sm">{entity.name}</div>
                  <Badge variant="secondary" className="text-xs mt-1">
                    {ENTITY_TYPE_LABELS[entity.entity_type] || entity.entity_type}
                  </Badge>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 筛选 */}
        <div className="p-4 border-b">
          <div className="flex items-center gap-2 mb-2">
            <Filter className="w-4 h-4" />
            <span className="text-sm font-medium">筛选类型</span>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant={entityTypeFilter === null ? "default" : "outline"}
              onClick={() => setEntityTypeFilter(null)}
            >
              全部
            </Button>
            {entityTypes.map((type) => (
              <Button
                key={type}
                size="sm"
                variant={entityTypeFilter === type ? "default" : "outline"}
                onClick={() => setEntityTypeFilter(type)}
              >
                {ENTITY_TYPE_LABELS[type] || type}
              </Button>
            ))}
          </div>
        </div>

        {/* 操作 */}
        <div className="p-4 border-b space-y-2">
          <Button
            className="w-full"
            onClick={() => handleBatchProcess(50)}
            disabled={isProcessing}
          >
            {isProcessing ? (
              <>
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                处理中...
              </>
            ) : (
              <>
                <Play className="w-4 h-4 mr-2" />
                批量处理文章
              </>
            )}
          </Button>
          <Button
            variant="outline"
            className="w-full"
            onClick={() => loadGraphData()}
            disabled={isLoading}
          >
            <RefreshCw className="w-4 h-4 mr-2" />
            刷新图谱
          </Button>
        </div>

        {/* 选中节点详情 */}
        {selectedNode && (
          <div className="p-4 flex-1 overflow-auto">
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium">选中节点</span>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setSelectedNode(null)}
              >
                <Trash2 className="w-4 h-4" />
              </Button>
            </div>
            <Card className="p-3">
              <div className="space-y-2">
                <div>
                  <div className="text-xs text-gray-500">名称</div>
                  <div className="font-medium">{selectedNode.label}</div>
                </div>
                <div>
                  <div className="text-xs text-gray-500">类型</div>
                  <Badge>{ENTITY_TYPE_LABELS[selectedNode.type] || selectedNode.type}</Badge>
                </div>
                {selectedNode.data?.description && (
                  <div>
                    <div className="text-xs text-gray-500">描述</div>
                    <div className="text-sm text-gray-700">
                      {selectedNode.data.description}
                    </div>
                  </div>
                )}
              </div>
            </Card>
          </div>
        )}

        {/* 状态信息 */}
        <div className="p-4 border-t mt-auto">
          <div className="flex items-center justify-between gap-2 text-sm">
            <div className="flex items-center gap-2">
              {health?.neo4j?.connected ? (
                <>
                  <CheckCircle2 className="w-4 h-4 text-emerald-500" />
                  <span className="text-emerald-600">Neo4j 已连接</span>
                </>
              ) : (
                <>
                  <AlertCircle className="w-4 h-4 text-red-500" />
                  <span className="text-red-600">Neo4j 未连接</span>
                </>
              )}
            </div>
            <Button
              size="sm"
              variant="outline"
              onClick={checkHealth}
              className="h-7 text-xs"
            >
              <RefreshCw className="w-3 h-3 mr-1" />
              测试连接
            </Button>
          </div>
        </div>
      </div>

      {/* 主图谱区域 */}
      <div className="flex-1 flex flex-col bg-gray-100">
        {/* 工具栏 */}
        <div className="h-12 border-b bg-white flex items-center px-4 gap-4">
          <span className="text-sm text-gray-500">
            {graphData.nodes.length} 节点 / {graphData.edges.length} 边
          </span>
          <span className="text-xs text-gray-400">
            Article {graphData.nodes.filter((node) => node.type === "Article").length}
            {' / '}
            Entity {graphData.nodes.filter((node) => node.type !== "Article").length}
          </span>
          <Separator orientation="vertical" className="h-6" />
          <span className="text-xs text-gray-400">
            拖拽节点可移动 | 滚轮缩放 | 点击节点查看详情
          </span>
          <Button
            size="sm"
            variant={exploreOpen ? "default" : "outline"}
            className="ml-auto"
            onClick={() => {
              const nextOpen = !exploreOpen;
              setExploreOpen(nextOpen);
              if (nextOpen && exploreTab === "community" && communities.length === 0 && entityRanking.length === 0) {
                loadDiscoveryOverview();
              }
            }}
          >
            <Route className="w-4 h-4 mr-2" />
            知识探索
          </Button>
        </div>

        {/* 图谱画布 */}
        <div className="flex-1 relative">
          {exploreOpen && (
            <Card className="absolute right-4 top-4 z-10 w-[min(34rem,calc(100%-2rem))] max-h-[calc(100%-2rem)] overflow-hidden bg-white shadow-lg">
              <div className="flex items-center justify-between border-b px-4 py-3">
                <div>
                  <div className="font-semibold">知识探索</div>
                  <div className="text-xs text-gray-500">从热点主题出发，逐步查看实体、关系与事件演进</div>
                </div>
                <Button size="icon" variant="ghost" onClick={() => setExploreOpen(false)} title="关闭">
                  <X className="w-4 h-4" />
                </Button>
              </div>
              <Tabs value={exploreTab} onValueChange={changeExploreTab} className="max-h-[calc(100vh-12rem)]">
                <div className="px-4 pt-3 text-xs leading-5 text-gray-500">
                  建议顺序：选主题或热点实体 → 查看实体档案 → 验证关系路径 → 观察事件时序
                </div>
                <TabsList className="mx-4 mt-2 grid h-auto w-[calc(100%-2rem)] grid-cols-4 gap-1 bg-gray-100 p-1">
                  <TabsTrigger value="community" className="px-2 py-1.5 bg-transparent data-active:bg-white data-active:shadow-sm"><Users className="w-4 h-4 mr-1" />主题热点</TabsTrigger>
                  <TabsTrigger value="profile" className="px-2 py-1.5 bg-transparent data-active:bg-white data-active:shadow-sm"><BookOpen className="w-4 h-4 mr-1" />实体探索</TabsTrigger>
                  <TabsTrigger value="path" className="px-2 py-1.5 bg-transparent data-active:bg-white data-active:shadow-sm"><Route className="w-4 h-4 mr-1" />关系路径</TabsTrigger>
                  <TabsTrigger value="timeline" className="px-2 py-1.5 bg-transparent data-active:bg-white data-active:shadow-sm"><Clock3 className="w-4 h-4 mr-1" />事件时序</TabsTrigger>
                </TabsList>
                <TabsContent value="path" className="m-0 p-4">
                  <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-2">
                    <Input list="kg-entity-names" value={pathSource} onChange={(e) => setPathSource(e.target.value)} placeholder="起点实体" />
                    <span className="text-gray-400">→</span>
                    <Input list="kg-entity-names" value={pathTarget} onChange={(e) => setPathTarget(e.target.value)} placeholder="终点实体" onKeyDown={(e) => e.key === "Enter" && findKnowledgePaths()} />
                  </div>
                  <div className="mt-2 flex items-center gap-2">
                    <label className="text-xs text-gray-500" htmlFor="path-depth">最大深度</label>
                    <select id="path-depth" className="h-9 rounded-md border bg-white px-2 text-sm" value={pathDepth} onChange={(e) => setPathDepth(Number(e.target.value))}>
                      {[2, 3, 4, 5, 6].map((depth) => <option key={depth} value={depth}>{depth}</option>)}
                    </select>
                    <Button className="ml-auto" onClick={findKnowledgePaths} disabled={exploreLoading}>
                      {exploreLoading ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Search className="w-4 h-4 mr-2" />}
                      查找路径
                    </Button>
                  </div>
                  <ScrollArea className="mt-4 h-[22rem] pr-3">
                    {paths.length === 0 ? (
                      <div className="py-12 text-center text-sm text-gray-400">输入两个实体，探索它们之间的最短关系链</div>
                    ) : paths.map((path, pathIndex) => (
                      <div key={pathIndex} className="mb-4 border-b pb-4 last:border-0">
                        <div className="mb-2 text-sm font-medium">路径 {pathIndex + 1} · {path.length} 跳</div>
                        <div className="flex flex-wrap items-center gap-1 text-sm">
                          {path.nodes.map((node, nodeIndex) => (
                            <span key={`${node.name}-${nodeIndex}`} className="contents">
                              <button className="text-indigo-700 hover:underline" onClick={() => { setExploreTab("profile"); loadEntityProfile(node.name); }}>{node.name}</button>
                              {nodeIndex < path.nodes.length - 1 && <span className="text-gray-400">→</span>}
                            </span>
                          ))}
                        </div>
                        <div className="mt-3 space-y-2">
                          {path.relationships.map((rel, relIndex) => (
                            <div key={relIndex} className="bg-gray-50 p-2 text-xs">
                              <div className="font-medium">{rel.source} — {rel.rel_type} → {rel.target}</div>
                              <div className="mt-1 flex flex-wrap items-center gap-1 text-gray-500">
                                <span>支持 {rel.support_count} 次 · 来源 {rel.source_articles.length} 篇{rel.confidence != null ? ` · 置信度 ${Math.round(rel.confidence * 100)}%` : ""}</span>
                                <Badge variant="outline" className="h-5 px-1 text-[10px]">
                                  {provenanceLabels[rel.provenance_status || "legacy"] || "来源待确认"}
                                </Badge>
                              </div>
                              {rel.evidence_samples?.[0] && <div className="mt-1 text-gray-700">“{rel.evidence_samples[0]}”</div>}
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                  </ScrollArea>
                </TabsContent>
                <TabsContent value="profile" className="m-0 p-4">
                  <div className="flex gap-2">
                    <Input list="kg-entity-names" value={profileQuery} onChange={(e) => setProfileQuery(e.target.value)} placeholder="输入实体名称" onKeyDown={(e) => e.key === "Enter" && loadEntityProfile()} />
                    <Button size="icon" onClick={() => loadEntityProfile()} disabled={exploreLoading} title="查询实体档案">
                      {exploreLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
                    </Button>
                  </div>
                  <ScrollArea className="mt-4 h-[24rem] pr-3">
                    {!entityProfile ? (
                      <div className="py-12 text-center text-sm text-gray-400">查询实体的跨文档来源、邻居和原文证据</div>
                    ) : (
                      <div className="space-y-4">
                        <div>
                          <div className="text-lg font-semibold">{entityProfile.entity.name || profileQuery}</div>
                          <div className="mt-1 text-sm text-gray-600">{entityProfile.entity.description || "暂无描述"}</div>
                          <div className="mt-2 text-xs text-gray-500">出现于 {entityProfile.article_count} 篇文章 · {entityProfile.neighbors.length} 个关联实体 · {entityProfile.evidence.length} 条可审计证据</div>
                        </div>
                        <div>
                          <div className="mb-2 text-sm font-medium">主要关系</div>
                          <div className="flex flex-wrap gap-2">
                            {entityProfile.neighbors.map((neighbor) => (
                              <button key={`${neighbor.name}-${neighbor.rel_type}`} className="border bg-gray-50 px-2 py-1 text-left text-xs hover:bg-gray-100" onClick={() => { setPathSource(profileQuery); setPathTarget(neighbor.name); setExploreTab("path"); }}>
                                {neighbor.name} · {neighbor.rel_type} ({neighbor.support_count})
                              </button>
                            ))}
                          </div>
                        </div>
                        {similarEntities.length > 0 && (
                          <div>
                            <div className="mb-2 flex items-center justify-between text-sm font-medium">
                              <span>相似实体</span>
                              <span className="text-[10px] font-normal text-gray-400">基于图谱关系结构</span>
                            </div>
                            <div className="flex flex-wrap gap-2">
                              {similarEntities.slice(0, 8).map((entity) => (
                                <button key={entity.name} className="border bg-gray-50 px-2 py-1 text-left text-xs hover:bg-gray-100" onClick={() => loadEntityProfile(entity.name)}>
                                  {entity.name} · {Math.round(entity.score * 100)}%
                                </button>
                              ))}
                            </div>
                          </div>
                        )}
                        <div>
                          <div className="mb-2 text-sm font-medium">关系证据</div>
                          <div className="space-y-2">
                            {entityProfile.evidence.length === 0 ? <div className="text-sm text-gray-400">旧关系尚无可审计证据，等待文章重建完成</div> : entityProfile.evidence.map((claim) => (
                              <div key={claim.claim_id} className="border-l-2 border-indigo-300 pl-3 text-sm">
                                <div className="flex items-center gap-2 font-medium">
                                  <span>{claim.source} — {claim.rel_type} → {claim.target}</span>
                                  {claim.status === "recovered" && <Badge variant="outline" className="h-5 px-1 text-[10px]">恢复证据</Badge>}
                                </div>
                                <div className="mt-1 text-gray-600">{claim.evidence || "未保留原文片段"}</div>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}
                  </ScrollArea>
                </TabsContent>
                <TabsContent value="community" className="m-0 p-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm font-medium">主题与热点</div>
                      <div className="text-xs text-gray-500">先看高频实体，再进入关系紧密的主题群组</div>
                    </div>
                    <Button size="icon" variant="outline" onClick={loadDiscoveryOverview} disabled={exploreLoading} title="刷新主题与热点">
                      <RefreshCw className={`w-4 h-4 ${exploreLoading ? "animate-spin" : ""}`} />
                    </Button>
                  </div>
                  <div className="mt-3 border-y py-3">
                    <div className="mb-2 text-xs font-medium text-gray-600">高频实体</div>
                    <div className="grid grid-cols-2 gap-2">
                      {entityRanking.slice(0, 8).map((item, index) => (
                        <button key={item.name} className="flex min-w-0 items-center gap-2 border bg-gray-50 px-2 py-1.5 text-left hover:bg-gray-100" onClick={() => { setExploreTab("profile"); loadEntityProfile(item.name); }}>
                          <span className="w-4 shrink-0 text-right text-[10px] text-gray-400">{index + 1}</span>
                          <span className="min-w-0 flex-1 truncate text-xs font-medium">{item.name}</span>
                          <span className="shrink-0 text-[10px] text-indigo-600">{item.occurrence_count} 篇</span>
                        </button>
                      ))}
                      {entityRanking.length === 0 && !exploreLoading && <div className="col-span-2 py-2 text-center text-xs text-gray-400">暂无热点实体</div>}
                    </div>
                  </div>
                  <ScrollArea className="mt-3 h-[18rem] pr-3">
                    <div className="mb-2 text-xs font-medium text-gray-600">关联主题</div>
                    {communities.length === 0 && !exploreLoading ? (
                      <div className="py-12 text-center text-sm text-gray-400">尚未发现满足条件的实体社区</div>
                    ) : communities.map((community) => (
                      <div key={community.id} className="border-b py-3 first:pt-0 last:border-0">
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0 text-sm font-medium">{community.label}</div>
                          <Badge variant="secondary" className="shrink-0">{community.size} 实体</Badge>
                        </div>
                        <div className="mt-1 text-xs text-gray-500">{community.article_count} 篇文章 · {community.internal_edges} 条内部关系 · 密度 {Math.round(community.density * 100)}%</div>
                        <div className="mt-2 text-xs text-gray-700">{community.summary}</div>
                        <div className="mt-2 flex flex-wrap gap-1">
                          {community.core_entities.map((entity) => (
                            <Badge key={entity.name} variant="secondary">核心 {entity.name}</Badge>
                          ))}
                          {community.bridge_entities.map((entity) => (
                            <Badge key={entity.name} variant="outline">桥接 {entity.name} · {entity.external_connections}</Badge>
                          ))}
                        </div>
                        <div className="mt-2 flex flex-wrap gap-1">
                          {community.members.slice(0, 10).map((member) => (
                            <button key={member.name} className="border bg-gray-50 px-2 py-1 text-xs hover:bg-gray-100" onClick={() => { setExploreTab("profile"); loadEntityProfile(member.name); }}>{member.name}</button>
                          ))}
                        </div>
                      </div>
                    ))}
                  </ScrollArea>
                </TabsContent>
                <TabsContent value="inference" className="m-0 p-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm font-medium">可解释规则推理</div>
                      <div className="text-xs text-gray-500">候选 {inferences.length} 条</div>
                    </div>
                    <Button size="icon" variant="outline" onClick={() => loadMiningResults("inference")} disabled={exploreLoading} title="重新计算推理候选">
                      <RefreshCw className={`w-4 h-4 ${exploreLoading ? "animate-spin" : ""}`} />
                    </Button>
                  </div>
                  <ScrollArea className="mt-3 h-[25rem] pr-3">
                    {inferences.length === 0 && !exploreLoading ? (
                      <div className="py-12 text-center text-sm text-gray-400">当前证据关系未形成可用传递链</div>
                    ) : inferences.map((inference, index) => (
                      <div key={`${inference.source}-${inference.rel_type}-${inference.target}-${index}`} className="border-b py-3 first:pt-0 last:border-0">
                        <div className="flex items-start justify-between gap-2">
                          <div className="text-sm font-medium">{inference.source} <span className="text-gray-400">→</span> {inference.target}</div>
                          <Badge variant="outline">{Math.round(inference.confidence * 100)}%</Badge>
                        </div>
                        <div className="mt-1 text-xs text-gray-500">{inference.rule} · {inference.hops} 跳 · {inference.source_articles.length} 篇来源</div>
                        <div className="mt-2 flex flex-wrap items-center gap-1 text-xs text-indigo-700">
                          {inference.path.map((entity, pathIndex) => (
                            <span key={`${entity}-${pathIndex}`} className="contents">
                              <button className="hover:underline" onClick={() => { setExploreTab("profile"); loadEntityProfile(entity); }}>{entity}</button>
                              {pathIndex < inference.path.length - 1 && <span className="text-gray-400">→</span>}
                            </span>
                          ))}
                        </div>
                        <div className="mt-2 flex gap-1">
                          <Button size="sm" className="h-7 px-2 text-xs" disabled={exploreLoading} onClick={() => submitMiningReview(
                            "/api/kg/mining/inferences/review",
                            { source: inference.source, target: inference.target, rel_type: inference.rel_type, decision: "approved" },
                            "inference",
                          )}><CheckCircle2 className="mr-1 h-3 w-3" />确认推理</Button>
                          <Button size="icon" variant="outline" className="h-7 w-7" disabled={exploreLoading} title="拒绝推理候选" onClick={() => submitMiningReview(
                            "/api/kg/mining/inferences/review",
                            { source: inference.source, target: inference.target, rel_type: inference.rel_type, decision: "rejected" },
                            "inference",
                          )}><X className="h-3 w-3" /></Button>
                        </div>
                      </div>
                    ))}
                  </ScrollArea>
                </TabsContent>
                <TabsContent value="timeline" className="m-0 p-4">
                  <div className="flex gap-2">
                    <Input value={timelineQuery} onChange={(event) => setTimelineQuery(event.target.value)} placeholder="搜索事件" onKeyDown={(event) => event.key === "Enter" && loadMiningResults("timeline")} />
                    <Button size="icon" onClick={() => loadMiningResults("timeline")} disabled={exploreLoading} title="查询时间线">
                      {exploreLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
                    </Button>
                  </div>
                  <ScrollArea className="mt-3 h-[25rem] pr-3">
                    {timelineEvents.length === 0 && !exploreLoading ? (
                      <div className="py-12 text-center text-sm text-gray-400">没有匹配的事件时间记录</div>
                    ) : timelineEvents.map((event, index) => (
                      <div key={`${event.name}-${index}`} className="border-b py-3 first:pt-0 last:border-0">
                        <div className="flex items-start justify-between gap-2">
                          <button className="text-left text-sm font-medium text-indigo-700 hover:underline" onClick={() => { setExploreTab("profile"); loadEntityProfile(event.name); }}>{event.name}</button>
                          <Badge variant="outline">{event.observed_at || "日期待定"}</Badge>
                        </div>
                        {event.description && <div className="mt-1 line-clamp-2 text-xs text-gray-600">{event.description}</div>}
                        <div className="mt-1 text-xs text-gray-500">文中时间 {event.date_markers.filter(Boolean).join("、") || "未标注"} · 来源 {event.articles.length} 篇</div>
                        {event.temporal_relations.some((relation) => relation.target) && (
                          <div className="mt-1 text-xs text-gray-500">
                            {event.temporal_relations.filter((relation) => relation.target).slice(0, 3).map((relation) => `${relation.rel_type} ${relation.target}`).join(" · ")}
                          </div>
                        )}
                      </div>
                    ))}
                  </ScrollArea>
                </TabsContent>
                <TabsContent value="prediction" className="m-0 p-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm font-medium">结构链接预测</div>
                      <div className="text-xs text-gray-500">候选 {linkPredictions.length} 条</div>
                    </div>
                    <Button size="icon" variant="outline" onClick={() => loadMiningResults("prediction")} disabled={exploreLoading} title="重新计算链接预测">
                      <RefreshCw className={`h-4 w-4 ${exploreLoading ? "animate-spin" : ""}`} />
                    </Button>
                  </div>
                  <ScrollArea className="mt-3 h-[25rem] pr-3">
                    {linkPredictions.length === 0 && !exploreLoading ? (
                      <div className="py-12 text-center text-sm text-gray-400">没有达到阈值的结构预测</div>
                    ) : linkPredictions.map((prediction, index) => (
                      <div key={`${prediction.source}-${prediction.target}-${index}`} className="border-b py-3 first:pt-0 last:border-0">
                        <div className="flex items-start justify-between gap-2">
                          <div className="text-sm font-medium">{prediction.source} <span className="text-gray-400">↔</span> {prediction.target}</div>
                          <Badge variant="outline">{Math.round(prediction.score * 100)}%</Badge>
                        </div>
                        <div className="mt-1 text-xs text-gray-500">{prediction.reasons.join(" · ")}</div>
                        <div className="mt-1 text-xs text-gray-600">共同邻居：{prediction.common_neighbors.slice(0, 8).join("、")}</div>
                        <div className="mt-2 flex gap-1">
                          <Button size="sm" className="h-7 px-2 text-xs" disabled={exploreLoading} onClick={() => submitMiningReview(
                            "/api/kg/mining/link-predictions/review",
                            { source: prediction.source, target: prediction.target, decision: "approved" },
                            "prediction",
                          )}><CheckCircle2 className="mr-1 h-3 w-3" />确认关联</Button>
                          <Button size="icon" variant="outline" className="h-7 w-7" disabled={exploreLoading} title="拒绝预测候选" onClick={() => submitMiningReview(
                            "/api/kg/mining/link-predictions/review",
                            { source: prediction.source, target: prediction.target, decision: "rejected" },
                            "prediction",
                          )}><X className="h-3 w-3" /></Button>
                        </div>
                      </div>
                    ))}
                  </ScrollArea>
                </TabsContent>
                <TabsContent value="similar" className="m-0 p-4">
                  <div className="flex gap-2">
                    <Input list="kg-entity-names" value={similarQuery} onChange={(event) => setSimilarQuery(event.target.value)} placeholder="输入实体名称" onKeyDown={(event) => event.key === "Enter" && loadSimilarEntities()} />
                    <Button size="icon" onClick={loadSimilarEntities} disabled={exploreLoading} title="查询相似实体">
                      {exploreLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
                    </Button>
                    <Button size="icon" variant="outline" onClick={generateEmbeddings} disabled={exploreLoading} title="生成或刷新图嵌入">
                      <RefreshCw className={`h-4 w-4 ${exploreLoading ? "animate-spin" : ""}`} />
                    </Button>
                  </div>
                  <div className="mt-2 flex items-center justify-between gap-2 text-[10px] text-gray-500">
                    <span className="truncate">{embeddingVersion ? `版本 ${embeddingVersion}` : "尚未生成图嵌入"}</span>
                    {embeddingStatus && (
                      <Badge variant={embeddingStatus.stale ? "destructive" : "secondary"} className="shrink-0 text-[10px]">
                        {embeddingStatus.stale ? "需刷新" : `${embeddingStatus.embedded_count}/${embeddingStatus.entity_count}`}
                      </Badge>
                    )}
                  </div>
                  {embeddingQuality && (
                    <div className="mt-2 grid grid-cols-3 gap-2 border-y py-2 text-center text-[10px] text-gray-500">
                      <div><div className="font-medium text-gray-800">{Math.round(embeddingQuality.precision_at_k * 100)}%</div>精确率@{embeddingQuality.k}</div>
                      <div><div className="font-medium text-gray-800">{Math.round(embeddingQuality.recall_at_k * 100)}%</div>召回率@{embeddingQuality.k}</div>
                      <div><div className="font-medium text-gray-800">{Math.round(embeddingQuality.mean_neighbor_similarity * 100)}%</div>邻居相似度</div>
                    </div>
                  )}
                  <ScrollArea className="mt-3 h-[24rem] pr-3">
                    {similarEntities.length === 0 && !exploreLoading ? (
                      <div className="py-12 text-center text-sm text-gray-400">生成嵌入后查询实体相似度</div>
                    ) : similarEntities.map((entity) => (
                      <button key={entity.name} className="flex w-full items-center justify-between gap-3 border-b py-3 text-left first:pt-0 last:border-0 hover:bg-gray-50" onClick={() => { setExploreTab("profile"); loadEntityProfile(entity.name); }}>
                        <div className="min-w-0">
                          <div className="truncate text-sm font-medium text-indigo-700">{entity.name}</div>
                          <div className="text-xs text-gray-500">{entity.entity_type || "OTHER"} · {entity.subtype || "未分类"}</div>
                        </div>
                        <Badge variant="outline">{Math.round(entity.score * 100)}%</Badge>
                      </button>
                    ))}
                  </ScrollArea>
                </TabsContent>
                <TabsContent value="causal" className="m-0 p-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm font-medium">证据约束因果候选</div>
                      <div className="text-xs text-gray-500">候选 {causalCandidates.length} 条</div>
                    </div>
                    <Button size="icon" variant="outline" onClick={() => loadMiningResults("causal")} disabled={exploreLoading} title="重新发现因果候选">
                      <RefreshCw className={`h-4 w-4 ${exploreLoading ? "animate-spin" : ""}`} />
                    </Button>
                  </div>
                  <ScrollArea className="mt-3 h-[25rem] pr-3">
                    {causalCandidates.length === 0 && !exploreLoading ? (
                      <div className="py-12 text-center text-sm text-gray-400">现有证据中没有满足严格条件的因果候选</div>
                    ) : causalCandidates.map((candidate, index) => (
                      <div key={`${candidate.source}-${candidate.rel_type}-${candidate.target}-${index}`} className="border-b py-3 first:pt-0 last:border-0">
                        <div className="flex items-start justify-between gap-2">
                          <div className="text-sm font-medium">{candidate.source} <span className="text-gray-400">→</span> {candidate.target}</div>
                          <Badge variant="outline">{Math.round(candidate.confidence * 100)}%</Badge>
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-1 text-xs text-gray-500">
                          <span>{candidate.rel_type} · 触发词 {candidate.markers.join("、")} · {candidate.source_articles.length} 篇来源</span>
                          {candidate.discovery_sources?.includes("historical_article") && <Badge variant="secondary" className="text-[10px]">历史原文</Badge>}
                        </div>
                        {candidate.evidence_samples[0] && <div className="mt-2 line-clamp-3 text-xs text-gray-700">{candidate.evidence_samples[0]}</div>}
                        <div className="mt-2 flex gap-1">
                          <Button size="sm" className="h-7 px-2 text-xs" disabled={exploreLoading} onClick={() => submitMiningReview(
                            "/api/kg/mining/causal-candidates/review",
                            { source: candidate.source, target: candidate.target, rel_type: candidate.rel_type, decision: "approved" },
                            "causal",
                          )}><CheckCircle2 className="mr-1 h-3 w-3" />确认因果</Button>
                          <Button size="icon" variant="outline" className="h-7 w-7" disabled={exploreLoading} title="拒绝因果候选" onClick={() => submitMiningReview(
                            "/api/kg/mining/causal-candidates/review",
                            { source: candidate.source, target: candidate.target, rel_type: candidate.rel_type, decision: "rejected" },
                            "causal",
                          )}><X className="h-3 w-3" /></Button>
                        </div>
                      </div>
                    ))}
                  </ScrollArea>
                </TabsContent>
                <TabsContent value="cross-document" className="m-0 p-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm font-medium">跨文档关系候选</div>
                      <div className="text-xs text-gray-500">至少在两篇文章共同出现，尚无显式关系</div>
                    </div>
                    <Button size="icon" variant="outline" onClick={() => loadMiningResults("cross-document")} disabled={exploreLoading} title="重新发现候选">
                      <RefreshCw className={`w-4 h-4 ${exploreLoading ? "animate-spin" : ""}`} />
                    </Button>
                  </div>
                  <ScrollArea className="mt-3 h-[25rem] pr-3">
                    {crossDocumentCandidates.map((candidate, index) => (
                      <div key={`${candidate.source}-${candidate.target}-${index}`} className="border-b py-3 first:pt-0 last:border-0">
                        <div className="text-sm font-medium">
                          <button className="text-indigo-700 hover:underline" onClick={() => { setExploreTab("profile"); loadEntityProfile(candidate.source); }}>{candidate.source}</button>
                          <span className="px-2 text-gray-400">↔</span>
                          <button className="text-indigo-700 hover:underline" onClick={() => { setExploreTab("profile"); loadEntityProfile(candidate.target); }}>{candidate.target}</button>
                        </div>
                        <div className="mt-1 text-xs text-gray-500">共同出现 {candidate.support_count} 篇 · 关联评分 {Math.round(candidate.score * 100)}%</div>
                        <div className="mt-2 flex gap-1">
                          <Button size="sm" className="h-7 px-2 text-xs" disabled={exploreLoading} onClick={() => submitMiningReview(
                            "/api/kg/mining/cross-document/review",
                            { source: candidate.source, target: candidate.target, decision: "approved" },
                            "cross-document",
                          )}><CheckCircle2 className="mr-1 h-3 w-3" />确认共现</Button>
                          <Button size="icon" variant="outline" className="h-7 w-7" disabled={exploreLoading} title="拒绝候选" onClick={() => submitMiningReview(
                            "/api/kg/mining/cross-document/review",
                            { source: candidate.source, target: candidate.target, decision: "rejected" },
                            "cross-document",
                          )}><X className="h-3 w-3" /></Button>
                          <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" onClick={() => { setPathSource(candidate.source); setPathTarget(candidate.target); setExploreTab("path"); }}>检查路径</Button>
                        </div>
                      </div>
                    ))}
                  </ScrollArea>
                </TabsContent>
                <TabsContent value="aliases" className="m-0 p-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm font-medium">实体别名候选</div>
                      <div className="text-xs text-gray-500">仅供人工审查，不会自动合并</div>
                    </div>
                    <Button size="icon" variant="outline" onClick={() => loadMiningResults("aliases")} disabled={exploreLoading} title="重新识别别名">
                      <RefreshCw className={`w-4 h-4 ${exploreLoading ? "animate-spin" : ""}`} />
                    </Button>
                  </div>
                  <ScrollArea className="mt-3 h-[25rem] pr-3">
                    {aliasCandidates.length === 0 && !exploreLoading ? (
                      <div className="py-12 text-center text-sm text-gray-400">未发现高可信别名候选</div>
                    ) : aliasCandidates.map((candidate, index) => (
                      <div key={`${candidate.left.name}-${candidate.right.name}-${index}`} className="border-b py-3 first:pt-0 last:border-0">
                        <div className="flex items-start justify-between gap-2">
                          <div className="text-sm font-medium">{candidate.left.name} <span className="text-gray-400">≈</span> {candidate.right.name}</div>
                          <Badge variant="outline">{Math.round(candidate.score * 100)}%</Badge>
                        </div>
                        <div className="mt-1 text-xs text-gray-500">{candidate.reasons.join(" · ")}</div>
                        <div className="mt-2 flex gap-2">
                          {[candidate.left, candidate.right].map((entity) => (
                            <Button key={entity.name} size="sm" variant="ghost" className="h-7 px-2 text-xs" onClick={() => { setExploreTab("profile"); loadEntityProfile(entity.name); }}>查看 {entity.name}</Button>
                          ))}
                        </div>
                        <div className="mt-2 flex flex-wrap gap-1">
                          <Button size="sm" className="h-7 px-2 text-xs" disabled={exploreLoading} onClick={() => submitMiningReview(
                            "/api/kg/mining/aliases/review",
                            { source: candidate.left.name, target: candidate.right.name, canonical_name: candidate.left.name, decision: "approved" },
                            "aliases",
                          )}><CheckCircle2 className="mr-1 h-3 w-3" />左侧为主</Button>
                          <Button size="sm" variant="outline" className="h-7 px-2 text-xs" disabled={exploreLoading} onClick={() => submitMiningReview(
                            "/api/kg/mining/aliases/review",
                            { source: candidate.left.name, target: candidate.right.name, canonical_name: candidate.right.name, decision: "approved" },
                            "aliases",
                          )}><CheckCircle2 className="mr-1 h-3 w-3" />右侧为主</Button>
                          <Button size="icon" variant="outline" className="h-7 w-7" disabled={exploreLoading} title="拒绝别名候选" onClick={() => submitMiningReview(
                            "/api/kg/mining/aliases/review",
                            { source: candidate.left.name, target: candidate.right.name, decision: "rejected" },
                            "aliases",
                          )}><X className="h-3 w-3" /></Button>
                        </div>
                      </div>
                    ))}
                  </ScrollArea>
                </TabsContent>
                <TabsContent value="governance" className="m-0 p-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm font-medium">历史无证据关系</div>
                      <div className="text-xs text-gray-500">待审核 {legacyRelations.length} 条</div>
                    </div>
                    <Button size="icon" variant="outline" onClick={() => loadMiningResults("governance")} disabled={exploreLoading} title="刷新待审核关系">
                      <RefreshCw className={`w-4 h-4 ${exploreLoading ? "animate-spin" : ""}`} />
                    </Button>
                  </div>
                  <ScrollArea className="mt-3 h-[25rem] pr-3">
                    {miningReviews.length > 0 && (
                      <div className="mb-4 border-b pb-3">
                        <div className="mb-2 text-xs font-medium text-gray-700">审核记录</div>
                        <div className="space-y-2">
                          {miningReviews.slice(0, 10).map((review) => (
                            <div key={review.id} className="flex items-center justify-between gap-2 bg-gray-50 p-2 text-xs">
                              <div className="min-w-0">
                                <div className="truncate font-medium">{review.source} → {review.target}</div>
                                <div className="text-gray-500">{review.review_type} · {review.decision} · {review.status === "undone" ? "已撤销" : "生效中"}</div>
                              </div>
                              {review.status !== "undone" && (
                                <Button size="icon" variant="ghost" className="h-7 w-7 shrink-0" title="撤销审核" disabled={exploreLoading} onClick={() => undoMiningReview(review.id)}>
                                  <Undo2 className="h-3.5 w-3.5" />
                                </Button>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {legacyRelations.length === 0 && !exploreLoading ? (
                      <div className="py-12 text-center text-sm text-gray-400">没有待审核的历史关系</div>
                    ) : legacyRelations.map((relation, index) => (
                      <div key={`${relation.source}-${relation.rel_type}-${relation.target}-${index}`} className="border-b py-3 first:pt-0 last:border-0">
                        <div className="text-sm font-medium">{relation.source} <span className="text-gray-400">→</span> {relation.target}</div>
                        <div className="mt-1 text-xs text-gray-500">{relation.rel_type}</div>
                        <div className="mt-2 flex gap-1">
                          <Button size="sm" variant="outline" className="h-7 px-2 text-xs" disabled={exploreLoading} onClick={() => submitMiningReview(
                            "/api/kg/mining/legacy-relations/review",
                            { source: relation.source, target: relation.target, rel_type: relation.rel_type, decision: "kept" },
                            "governance",
                          )}><CheckCircle2 className="mr-1 h-3 w-3" />保留</Button>
                          <Button size="icon" variant="destructive" className="h-7 w-7" disabled={exploreLoading} title="删除历史关系" onClick={() => {
                            if (confirm(`删除关系“${relation.source} → ${relation.target}”？审核快照会保留。`)) {
                              submitMiningReview(
                                "/api/kg/mining/legacy-relations/review",
                                { source: relation.source, target: relation.target, rel_type: relation.rel_type, decision: "deleted" },
                                "governance",
                              );
                            }
                          }}><Trash2 className="h-3 w-3" /></Button>
                        </div>
                      </div>
                    ))}
                  </ScrollArea>
                </TabsContent>
                <TabsContent value="ranking" className="m-0 p-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm font-medium">实体出现次数排行</div>
                      <div className="text-xs text-gray-500">按文章引用次数排序</div>
                    </div>
                    <Button size="icon" variant="outline" onClick={loadEntityRanking} disabled={exploreLoading} title="刷新排行">
                      <RefreshCw className={`w-4 h-4 ${exploreLoading ? "animate-spin" : ""}`} />
                    </Button>
                  </div>
                  <ScrollArea className="mt-3 h-[25rem] pr-3">
                    {entityRanking.length === 0 && !exploreLoading ? (
                      <div className="py-12 text-center text-sm text-gray-400">暂无实体排行数据</div>
                    ) : (
                      <div className="space-y-2">
                        {entityRanking.map((item, index) => {
                          const maxCount = entityRanking[0]?.occurrence_count || 1;
                          const percentage = (item.occurrence_count / maxCount) * 100;
                          const typeColors: Record<string, string> = {
                            PERSON: "bg-amber-100 text-amber-700",
                            ORGANIZATION: "bg-blue-100 text-blue-700",
                            LOCATION: "bg-violet-100 text-violet-700",
                            TECHNOLOGY: "bg-pink-100 text-pink-700",
                            EVENT: "bg-red-100 text-red-700",
                            CONCEPT: "bg-cyan-100 text-cyan-700",
                            DATE: "bg-slate-100 text-slate-700",
                          };
                          return (
                            <div
                              key={item.name}
                              className="border-b py-2 first:pt-0 last:border-0 cursor-pointer hover:bg-gray-50 transition-colors"
                              onClick={() => {
                                // 高亮该实体节点及其邻居
                                const entityNode = graphData.nodes.find((n) => n.id === item.name || n.label === item.name);
                                if (entityNode) {
                                  const neighborIds = new Set<string>([entityNode.id]);
                                  graphData.edges.forEach((e) => {
                                    if (e.source === entityNode.id) neighborIds.add(e.target);
                                    if (e.target === entityNode.id) neighborIds.add(e.source);
                                  });
                                  setHighlightedNodeIds(neighborIds);
                                  setExploreOpen(false);
                                }
                              }}
                              title={`点击聚焦图谱中的 "${item.name}"`}
                            >
                              <div className="flex items-center justify-between">
                                <div className="flex items-center gap-2 min-w-0">
                                  <span className="text-xs font-mono text-gray-400 w-5 text-right">{index + 1}</span>
                                  <span className="text-sm font-medium truncate">{item.name}</span>
                                  <span className={`text-[10px] px-1.5 py-0.5 rounded ${typeColors[item.entity_type] || "bg-gray-100 text-gray-700"}`}>
                                    {item.entity_type}
                                  </span>
                                </div>
                                <span className="text-sm font-semibold text-indigo-600 shrink-0">{item.occurrence_count}</span>
                              </div>
                              <div className="mt-1.5 h-1.5 bg-gray-100 rounded-full overflow-hidden ml-7">
                                <div
                                  className="h-full bg-indigo-500 rounded-full transition-all"
                                  style={{ width: `${percentage}%` }}
                                />
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </ScrollArea>
                </TabsContent>
              </Tabs>
              <datalist id="kg-entity-names">
                {graphData.nodes.filter((node) => node.type !== "Article").map((node) => <option key={node.id} value={node.label} />)}
              </datalist>
            </Card>
          )}
          {isLoading ? (
            <div className="absolute inset-0 flex items-center justify-center">
              <Loader2 className="w-8 h-8 animate-spin text-indigo-600" />
            </div>
          ) : graphData.nodes.length === 0 ? (
            <div className="absolute inset-0 flex flex-col items-center justify-center text-gray-500">
              <Network className="w-16 h-16 mb-4 text-gray-300" />
              <p className="text-lg mb-2">暂无图谱数据</p>
              <p className="text-sm">点击“批量处理文章”开始构建知识图谱</p>
            </div>
          ) : (
            <>
              <canvas
                ref={canvasRef}
                className="pointer-events-none absolute inset-0 h-full w-full bg-slate-50"
              />
              <svg
                ref={svgRef}
                className="absolute inset-0 h-full w-full"
              />
            </>
          )}
        </div>

        {/* 图例 - 包含所有 entity_type 细分领域 */}
        <div className="h-10 border-t bg-white flex items-center px-4 gap-4 overflow-x-auto">
          <span className="text-xs text-gray-500 shrink-0">图例：</span>
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-1 shrink-0">
              <div className="w-3 h-3 rounded-full bg-indigo-600" />
              <span className="text-xs">文章</span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <div className="w-3 h-3 rounded-full bg-amber-500" />
              <span className="text-xs">人物</span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <div className="w-3 h-3 rounded-full bg-blue-500" />
              <span className="text-xs">组织</span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <div className="w-3 h-3 rounded-full bg-violet-500" />
              <span className="text-xs">地点</span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <div className="w-3 h-3 rounded-full bg-pink-500" />
              <span className="text-xs">技术</span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <div className="w-3 h-3 rounded-full bg-red-500" />
              <span className="text-xs">事件</span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <div className="w-3 h-3 rounded-full bg-cyan-500" />
              <span className="text-xs">概念</span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <div className="w-3 h-3 rounded-full bg-slate-500" />
              <span className="text-xs">时间</span>
            </div>
            {stats?.entities_by_subtype && Object.keys(stats.entities_by_subtype).length > 0 && (
              <>
                <Separator orientation="vertical" className="h-4" />
                <span className="text-xs text-gray-400 shrink-0">细分:</span>
                {Object.entries(stats.entities_by_subtype)
                  .sort(([, a], [, b]) => b - a)
                  .slice(0, 8)
                  .map(([sub, count]) => (
                    <div key={sub} className="flex items-center gap-1 shrink-0">
                      <span className="text-[10px] text-gray-500">{SUBTYPE_LABELS[sub] || sub}({count})</span>
                    </div>
                  ))}
              </>
            )}
          </div>
        </div>
      </div>

      {/* 节点原文出处弹窗 */}
      {popoverEntity && (
        <EntitySourcePopover
          entityName={popoverEntity}
          onClose={() => setPopoverEntity(null)}
          onJumpToArticle={(id) => {
            const name = popoverEntity;
            setPopoverEntity(null);
            window.location.href = `/articles?highlight=${encodeURIComponent(name)}&article=${id}`;
          }}
        />
      )}
    </div>
  );
}

// 使用 Suspense 包装以支持 useSearchParams
export default function KnowledgeGraphPage() {
  return (
    <Suspense fallback={<div className="flex items-center justify-center h-screen"><Loader2 className="w-8 h-8 animate-spin" /></div>}>
      <KnowledgeGraphPageContent />
    </Suspense>
  );
}
