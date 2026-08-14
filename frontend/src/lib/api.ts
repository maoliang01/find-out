/**
 * API 客户端封装
 * 统一处理与后端的 API 通信
 */
import type {
  ChatRequestAPI,
  ChatResponseAPI,
  ModelInfo,
  ModelConfigInput,
  TestResult,
  ScrapeOptions,
  ScrapeResult,
  TabAnalyzeParams,
  TabAnalyzeResult,
} from "@/types";

const API_BASE = "/api";

// ================================================
// 对话 API
// ================================================

/**
 * 发送对话请求
 */
export async function sendChat(request: ChatRequestAPI, signal?: AbortSignal): Promise<ChatResponseAPI> {
  const response = await fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
    signal,
  });

  if (!response.ok) {
    throw new Error(`聊天请求失败: ${response.status}`);
  }

  return response.json();
}

/**
 * 发送流式对话请求 (SSE)
 */
export async function* streamChat(
  request: ChatRequestAPI,
  signal?: AbortSignal
): AsyncGenerator<string, void, unknown> {
  const response = await fetch(`${API_BASE}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
    signal,
  });

  if (!response.ok) {
    throw new Error(`聊天请求失败: ${response.status}`);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("无法读取响应流");
  }

  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (signal?.aborted) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (line.startsWith("data: ")) {
          const data = line.slice(6);
          try {
            const parsed = JSON.parse(data);
            if (parsed.content || parsed.done) {
              yield parsed.content;
            }
            if (parsed.done) return;
          } catch {
            // 忽略解析错误
          }
        }
      }
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}

/**
 * 获取后端可用模型列表
 */
export async function fetchAvailableModels(): Promise<{ id: string; name: string; model_name?: string }[]> {
  const response = await fetch(`${API_BASE}/chat/models`);
  if (!response.ok) {
    throw new Error(`获取可用模型失败: ${response.status}`);
  }
  return response.json();
}

/**
 * 获取本机数据库保存的对话会话列表（不含消息）。
 */
export async function getSessions(): Promise<Array<{
  id: string;
  title: string;
  model: string;
  use_rag: boolean;
  created_at: string;
  updated_at: string;
}>> {
  const response = await fetch(`${API_BASE}/chat/sessions`);
  if (!response.ok) {
    throw new Error(`获取会话列表失败: ${response.status}`);
  }
  const data = await response.json();
  return data.sessions || [];
}

/**
 * 创建对话会话。
 */
export async function createSession(data: {
  title?: string;
  model?: string;
}): Promise<{
  id: string;
  title: string;
  model: string;
  use_rag: boolean;
  created_at: string;
  updated_at: string;
}> {
  const response = await fetch(`${API_BASE}/chat/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    throw new Error(`创建会话失败: ${response.status}`);
  }
  return response.json();
}

/**
 * 获取某会话的历史消息。
 */
export async function getSessionMessages(sessionId: string): Promise<Array<{
  id: string;
  session_id: string;
  role: string;
  content: string;
  model: string | null;
  created_at: string;
}>> {
  const response = await fetch(`${API_BASE}/chat/sessions/${encodeURIComponent(sessionId)}/messages`);
  if (!response.ok) {
    throw new Error(`获取会话消息失败: ${response.status}`);
  }
  return response.json();
}

/**
 * 删除会话及其消息。
 */
export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/chat/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    throw new Error(`删除会话失败: ${response.status}`);
  }
}

// ================================================
// 模型管理 API
// ================================================

/**
 * 获取已配置的模型列表
 */
export async function fetchModels(): Promise<ModelInfo[]> {
  const response = await fetch(`${API_BASE}/models`);
  if (!response.ok) {
    throw new Error(`获取模型列表失败: ${response.status}`);
  }
  return response.json();
}

/**
 * 添加模型
 */
export async function createModel(config: ModelConfigInput): Promise<ModelInfo> {
  const response = await fetch(`${API_BASE}/models`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });

  if (!response.ok) {
    throw new Error(`添加模型失败: ${response.status}`);
  }

  return response.json();
}

/**
 * 更新模型配置
 */
export async function updateModel(modelId: string, config: ModelConfigInput): Promise<ModelInfo> {
  const response = await fetch(`${API_BASE}/models/${modelId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });

  if (!response.ok) {
    throw new Error(`更新模型失败: ${response.status}`);
  }

  return response.json();
}

/**
 * 删除模型
 */
export async function deleteModel(modelId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/models/${modelId}`, {
    method: "DELETE",
  });

  if (!response.ok) {
    throw new Error(`删除模型失败: ${response.status}`);
  }
}

/**
 * 测试模型连接
 */
export async function testModel(modelId: string): Promise<TestResult> {
  const response = await fetch(`${API_BASE}/models/${modelId}/test`, {
    method: "POST",
  });

  if (!response.ok) {
    throw new Error(`测试模型连接失败: ${response.status}`);
  }

  return response.json();
}

/**
 * 同步模型配置到后端
 */
export async function syncModels(models: ModelConfigInput[]): Promise<{ message: string }> {
  const response = await fetch(`${API_BASE}/models/sync`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(models),
  });

  if (!response.ok) {
    throw new Error(`同步模型失败: ${response.status}`);
  }

  return response.json();
}

// ================================================
// 爬取 API
// ================================================

/**
 * 爬取单个 URL
 */
export async function scrapeUrl(url: string, options?: Partial<ScrapeOptions>): Promise<ScrapeResult> {
  const response = await fetch(`${API_BASE}/scrape`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, options }),
  });

  if (!response.ok) {
    throw new Error(`爬取失败: ${response.status}`);
  }

  return response.json();
}

/**
 * 批量爬取多个 URL
 */
export async function scrapeBatch(urls: string[], options?: Partial<ScrapeOptions>): Promise<ScrapeResult[]> {
  const response = await fetch(`${API_BASE}/scrape/batch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ urls, options }),
  });

  if (!response.ok) {
    throw new Error(`批量爬取失败: ${response.status}`);
  }

  return response.json();
}

/**
 * 分析 URL 的页签结构
 *
 * @param params 分析参数
 * @returns 页签树结构
 */
export async function analyzeTabs(params: TabAnalyzeParams): Promise<TabAnalyzeResult> {
  const response = await fetch(`${API_BASE}/scrape/tabs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url: params.url,
      include_nav: params.includeNav ?? true,
      include_tabs: params.includeTabs ?? true,
      max_depth: params.maxDepth ?? 3,
    }),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(errorData.error || `页签分析失败: ${response.status}`);
  }

  // 后端返回 snake_case，这里转换为 camelCase
  const data = await response.json();

  if (data.tree) {
    data.tree = normalizeTabTree(data.tree);
  }

  return data;
}

/**
 * 规范化后端返回的 TabTree，将 snake_case 转换为 camelCase
 */
interface RawTabTree {
  domain: string;
  site_title?: string;
  siteTitle?: string;
  root: RawTabNode;
  all_nodes?: RawTabNode[];
  allNodes?: RawTabNode[];
  generated_at?: string;
  generatedAt?: string;
  total_count?: number;
  totalCount?: number;
}

interface RawTabNode {
  id: string;
  label: string;
  url: string;
  children?: RawTabNode[];
  level?: number;
  type?: string;
  expandable?: boolean;
  url_pattern?: string;
  urlPattern?: string;
}

function normalizeTabTree(tree: RawTabTree) {
  return {
    domain: tree.domain,
    siteTitle: tree.site_title || tree.siteTitle || tree.domain,
    root: normalizeTabNode(tree.root),
    allNodes: (tree.all_nodes || tree.allNodes || []).map(normalizeTabNode),
    generatedAt: tree.generated_at || tree.generatedAt || new Date().toISOString(),
    totalCount: tree.total_count ?? tree.totalCount ?? 0,
  };
}

/**
 * 规范化 TabNode，将 snake_case 转换为 camelCase
 */
function normalizeTabNode(node: RawTabNode): {
  id: string;
  label: string;
  url: string;
  children: ReturnType<typeof normalizeTabNode>[];
  level?: number;
  type?: string;
  expandable?: boolean;
  urlPattern?: string | null;
} {
  return {
    id: node.id,
    label: node.label,
    url: node.url,
    children: (node.children || []).map((c) => normalizeTabNode(c)),
    level: node.level,
    type: node.type,
    expandable: node.expandable,
    urlPattern: node.url_pattern,
  };
}

// ================================================
// Firecrawl API
// ================================================

export interface FirecrawlScrapeResult {
  success: boolean;
  url: string;
  title: string;
  content: string;
  html: string;
  word_count: number;
  links: string[];
  status: string;
  error_message?: string;
}

export interface FirecrawlMapResult {
  success: boolean;
  url: string;
  links: string[];
  metadata: {
    title?: string;
    description?: string;
  };
  error_message?: string;
}

export interface FirecrawlHealthResult {
  available: boolean;
  url: string;
  message: string;
}

/**
 * 检查 Firecrawl 服务状态
 */
export async function checkFirecrawlHealth(): Promise<FirecrawlHealthResult> {
  const response = await fetch(`${API_BASE}/firecrawl/health`);
  if (!response.ok) {
    throw new Error(`检查 Firecrawl 状态失败: ${response.status}`);
  }
  return response.json();
}

/**
 * 使用 Firecrawl 爬取网页
 */
export async function firecrawlScrape(
  url: string,
  formats?: string[]
): Promise<FirecrawlScrapeResult> {
  const response = await fetch(`${API_BASE}/firecrawl/scrape`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url,
      formats: formats || ["markdown", "html", "links"],
    }),
  });

  if (!response.ok) {
    throw new Error(`Firecrawl 爬取失败: ${response.status}`);
  }

  return response.json();
}

/**
 * 使用 Firecrawl 获取网站地图
 */
export async function firecrawlMap(url: string): Promise<FirecrawlMapResult> {
  const response = await fetch(`${API_BASE}/firecrawl/map`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });

  if (!response.ok) {
    throw new Error(`Firecrawl 获取地图失败: ${response.status}`);
  }

  return response.json();
}

// ================================================
// 设置 API
// ================================================

/**
 * 获取设置
 */
export async function fetchSettings(): Promise<Record<string, unknown>> {
  const response = await fetch(`${API_BASE}/settings`);
  if (!response.ok) {
    throw new Error(`获取设置失败: ${response.status}`);
  }
  return response.json();
}

/**
 * 保存设置
 */
export async function saveSettings(data: Record<string, unknown>): Promise<{ message: string }> {
  const response = await fetch(`${API_BASE}/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    throw new Error(`保存设置失败: ${response.status}`);
  }

  return response.json();
}

// ================================================
// 文章管理 API（数据库）
// ================================================

import type {
  Article,
  ArticleListResponse,
  ArticleStats,
  ArticleSearchRequest,
} from "@/types";

/**
 * 获取文章统计
 */
export async function fetchArticleStats(): Promise<ArticleStats> {
  const response = await fetch(`${API_BASE}/articles/stats`);
  if (!response.ok) {
    throw new Error(`获取文章统计失败: ${response.status}`);
  }
  return response.json();
}

/**
 * 获取文章列表
 */
export async function fetchArticles(params?: {
  categoryId?: string;
  sourceId?: string;
  status?: string;
  startDate?: string;
  endDate?: string;
  page?: number;
  pageSize?: number;
}): Promise<ArticleListResponse> {
  const searchParams = new URLSearchParams();
  if (params?.categoryId) searchParams.set("category_id", params.categoryId);
  if (params?.sourceId) searchParams.set("source_id", params.sourceId);
  if (params?.status) searchParams.set("status", params.status);
  if (params?.startDate) searchParams.set("start_date", params.startDate);
  if (params?.endDate) searchParams.set("end_date", params.endDate);
  if (params?.page) searchParams.set("page", String(params.page));
  if (params?.pageSize) searchParams.set("page_size", String(params.pageSize));

  const response = await fetch(`${API_BASE}/articles?${searchParams}`);
  if (!response.ok) {
    throw new Error(`获取文章列表失败: ${response.status}`);
  }

  const data = await response.json();
  // 规范化 snake_case -> camelCase
  return {
    ...data,
    pageSize: data.page_size,
  };
}

/**
 * 获取单个文章
 */
export async function fetchArticle(id: string): Promise<Article> {
  const response = await fetch(`${API_BASE}/articles/${id}`);
  if (!response.ok) {
    throw new Error(`获取文章失败: ${response.status}`);
  }
  return normalizeArticle(await response.json());
}

/**
 * 创建文章
 */
export async function createArticle(data: {
  url: string;
  title?: string;
  content?: string;
  keywords?: string[];
  categoryId?: string;
  sourceId?: string;
}): Promise<Article> {
  const response = await fetch(`${API_BASE}/articles`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url: data.url,
      title: data.title,
      content: data.content,
      keywords: data.keywords,
      category_id: data.categoryId,
      source_id: data.sourceId,
    }),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(errorData.detail || `创建文章失败: ${response.status}`);
  }

  return normalizeArticle(await response.json());
}

/**
 * 更新文章
 */
export async function updateArticle(id: string, data: Partial<Article>): Promise<Article> {
  const body: Record<string, unknown> = {};
  if (data.title !== undefined) body.title = data.title;
  if (data.content !== undefined) body.content = data.content;
  if (data.author !== undefined) body.author = data.author;
  if (data.summary !== undefined) body.summary = data.summary;
  if (data.categoryId !== undefined) body.category_id = data.categoryId;
  if (data.keywords !== undefined) body.keywords = data.keywords;

  const response = await fetch(`${API_BASE}/articles/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    throw new Error(`更新文章失败: ${response.status}`);
  }

  return normalizeArticle(await response.json());
}

/**
 * 删除文章
 */
export async function deleteArticle(id: string): Promise<void> {
  const response = await fetch(`${API_BASE}/articles/${id}`, {
    method: "DELETE",
  });

  if (!response.ok) {
    throw new Error(`删除文章失败: ${response.status}`);
  }
}

/**
 * 搜索文章
 */
export async function searchArticles(params: ArticleSearchRequest): Promise<ArticleListResponse> {
  const searchParams = new URLSearchParams();
  searchParams.set("q", params.q);
  if (params.categoryId) searchParams.set("category_id", params.categoryId);
  if (params.sourceId) searchParams.set("source_id", params.sourceId);
  if (params.status) searchParams.set("status", params.status);
  if (params.page) searchParams.set("page", String(params.page));
  if (params.pageSize) searchParams.set("page_size", String(params.pageSize));

  const response = await fetch(`${API_BASE}/articles/search?${searchParams}`);
  if (!response.ok) {
    throw new Error(`搜索文章失败: ${response.status}`);
  }

  const data = await response.json();
  return {
    ...data,
    pageSize: data.page_size,
  };
}

/**
 * 根据 URL 获取文章
 */
export async function fetchArticleByUrl(url: string): Promise<Article | null> {
  try {
    const response = await fetch(`${API_BASE}/articles/url/${encodeURIComponent(url)}`);
    if (response.ok) {
      return normalizeArticle(await response.json());
    }
    if (response.status === 404) {
      return null;
    }
    throw new Error(`查询失败: ${response.status}`);
  } catch (error) {
    if ((error as { status?: number }).status === 404) {
      return null;
    }
    throw error;
  }
}

/**
 * 检查 URL 是否已存在
 */
export async function checkArticleUrlExists(url: string): Promise<{ exists: boolean; articleId?: string }> {
  const response = await fetch(`${API_BASE}/articles/check-url/${encodeURIComponent(url)}`);
  if (!response.ok) {
    throw new Error(`检查 URL 失败: ${response.status}`);
  }
  return response.json();
}

/**
 * 规范化后端返回的文章数据 (snake_case -> camelCase)
 */
function normalizeArticle(data: Record<string, unknown>): Article {
  return {
    id: data.id as string,
    url: data.url as string,
    title: data.title as string || "",
    content: data.content as string || "",
    html: data.html as string | undefined,
    wordCount: data.word_count as number ?? data.wordCount as number ?? 0,
    author: data.author as string | undefined,
    summary: data.summary as string | undefined,
    style: data.style as string | undefined,
    contentHash: data.content_hash as string | undefined,
    sourceId: data.source_id as string | undefined,
    sourceName: data.source_name as string | undefined,
    sourceType: data.source_type as string | undefined,
    categoryId: data.category_id as string | undefined,
    categoryName: data.category_name as string | undefined,
    publishedAt: data.published_at as string | undefined,
    scrapedAt: data.scraped_at as string,
    status: (data.status as Article["status"]) || "success",
    errorMessage: data.error_message as string | undefined,
    keywords: (data.keywords as string[]) || [],
    createdAt: data.created_at as string | undefined,
    updatedAt: data.updated_at as string | undefined,
    kgStatus: data.kg_status as Article["kgStatus"],
    kgProcessedAt: data.kg_processed_at as string | undefined,
    kgContentHash: data.kg_content_hash as string | undefined,
    kgErrorMessage: data.kg_error_message as string | undefined,
  };
}
// ================================================
// 微信公众号 API
// ================================================

/**
 * 微信公众号 Cookie 类型
 */
export interface WechatCookie {
  id: string;
  name: string;
  isActive: boolean;
  expiresAt?: string;
  lastUsedAt?: string;
  lastDiscoveryAt?: string;
  nextDiscoveryAt?: string;
  lastDiscoveryStatus?: string;
  rateLimitCount?: number;
  createdAt?: string;
  updatedAt?: string;
}

/**
 * 微信公众号账号类型
 */
export interface WechatAccount {
  id: string;
  name: string;
  wechatId?: string;
  fakeid?: string;
  description?: string;
  isEnabled: boolean;
  lastCrawledAt?: string;
  articleCount: number;
  minCrawlIntervalMinutes?: number;
  lastDiscoveryAt?: string;
  nextDiscoveryAt?: string;
  lastDiscoveryStatus?: string;
  rateLimitCount?: number;
  createdAt?: string;
  updatedAt?: string;
}

/**
 * 微信公众号定时任务类型
 */
export interface WechatTask {
  id: string;
  accountId: string;
  scheduleType: string;
  scheduleTime?: string;
  maxArticles: number;
  isEnabled: boolean;
  lastRunAt?: string;
  nextRunAt?: string;
  createdAt?: string;
  updatedAt?: string;
}

/**
 * 获取微信公众号 Cookie 列表
 */
export async function fetchWechatCookies(activeOnly: boolean = false): Promise<WechatCookie[]> {
  const params = new URLSearchParams();
  if (activeOnly) params.set("active_only", "true");

  const response = await fetch(`${API_BASE}/wechat/cookies?${params}`);
  if (!response.ok) {
    throw new Error(`获取 Cookie 列表失败: ${response.status}`);
  }
  const data = await response.json();
  return data.items || [];
}

/**
 * 创建微信公众号 Cookie
 */
export async function createWechatCookie(data: {
  name: string;
  cookieData: string;
  expiresAt?: string;
}): Promise<{ success: boolean; item?: WechatCookie }> {
  const response = await fetch(`${API_BASE}/wechat/cookies`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: data.name,
      cookie_data: data.cookieData,
      expires_at: data.expiresAt,
    }),
  });
  return response.json();
}

/**
 * 删除微信公众号 Cookie
 */
export async function deleteWechatCookie(id: string): Promise<{ success: boolean }> {
  const response = await fetch(`${API_BASE}/wechat/cookies/${id}`, {
    method: "DELETE",
  });
  return response.json();
}

/**
 * 激活微信公众号 Cookie
 */
export async function activateWechatCookie(id: string): Promise<{ success: boolean; item?: WechatCookie }> {
  const response = await fetch(`${API_BASE}/wechat/cookies/${id}/activate`, {
    method: "POST",
  });
  return response.json();
}

/**
 * 停用微信公众号 Cookie
 */
export async function deactivateWechatCookie(id: string): Promise<{ success: boolean; item?: WechatCookie }> {
  const response = await fetch(`${API_BASE}/wechat/cookies/${id}/deactivate`, {
    method: "POST",
  });
  return response.json();
}

/**
 * 验证微信公众号 Cookie
 */
export async function validateWechatCookie(id: string): Promise<{
  valid: boolean;
  message: string;
  expiresAt?: string;
  hasRequiredKeys?: boolean;
  foundKeys?: string[];
}> {
  const response = await fetch(`${API_BASE}/wechat/cookies/${id}/validate`, {
    method: "POST",
  });
  return response.json();
}

/**
 * 获取微信公众号列表
 */
export async function fetchWechatAccounts(enabledOnly: boolean = false): Promise<WechatAccount[]> {
  const params = new URLSearchParams();
  if (enabledOnly) params.set("enabled_only", "true");

  const response = await fetch(`${API_BASE}/wechat/accounts?${params}`);
  if (!response.ok) {
    throw new Error(`获取公众号列表失败: ${response.status}`);
  }
  const data = await response.json();
  return data.items || [];
}

/**
 * 创建微信公众号
 */
export async function createWechatAccount(data: {
  name: string;
  wechatId?: string;
  description?: string;
}): Promise<{ success: boolean; item?: WechatAccount }> {
  const response = await fetch(`${API_BASE}/wechat/accounts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: data.name,
      wechat_id: data.wechatId,
      description: data.description,
    }),
  });
  return response.json();
}

/**
 * 删除微信公众号
 */
export async function deleteWechatAccount(id: string): Promise<{ success: boolean }> {
  const response = await fetch(`${API_BASE}/wechat/accounts/${id}`, {
    method: "DELETE",
  });
  return response.json();
}

/**
 * 立即爬取微信公众号
 */
export async function crawlWechatAccount(
  id: string,
  maxArticles: number = 10
): Promise<{ success: boolean; message?: string }> {
  const response = await fetch(`${API_BASE}/wechat/accounts/${id}/crawl`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ max_articles: maxArticles }),
  });
  return response.json();
}

/**
 * 获取微信公众号定时任务列表
 */
export async function fetchWechatTasks(): Promise<WechatTask[]> {
  const response = await fetch(`${API_BASE}/wechat/tasks`);
  if (!response.ok) {
    throw new Error(`获取定时任务列表失败: ${response.status}`);
  }
  const data = await response.json();
  return data.items || [];
}

/**
 * 解析定时任务接口响应，并避免 HTML 错误页触发 JSON 语法异常。
 */
async function readWechatTaskResponse<T>(response: Response, fallbackMessage: string): Promise<T> {
  const body = await response.text();
  if (!body) {
    if (!response.ok) throw new Error(`${fallbackMessage}（HTTP ${response.status}）`);
    return {} as T;
  }

  let result: T & { detail?: string; error?: string };
  try {
    result = JSON.parse(body);
  } catch {
    throw new Error(`${fallbackMessage}：服务返回了非 JSON 响应（HTTP ${response.status}）`);
  }

  if (!response.ok) {
    throw new Error(result.detail || result.error || `${fallbackMessage}（HTTP ${response.status}）`);
  }
  return result;
}

/**
 * 创建微信公众号定时任务
 */
export async function createWechatTask(data: {
  accountId: string;
  scheduleType: string;
  scheduleTime?: string;
  maxArticles?: number;
  isEnabled?: boolean;
}): Promise<{ success: boolean; item?: WechatTask }> {
  const response = await fetch(`${API_BASE}/wechat/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      account_id: data.accountId,
      schedule_type: data.scheduleType,
      schedule_time: data.scheduleTime,
      max_articles: data.maxArticles,
      is_enabled: data.isEnabled,
    }),
  });
  return readWechatTaskResponse(response, "创建定时任务失败");
}

/**
 * 删除微信公众号定时任务
 */
export async function deleteWechatTask(id: string): Promise<{ success: boolean }> {
  const response = await fetch(`${API_BASE}/wechat/tasks/${id}`, {
    method: "DELETE",
  });
  return readWechatTaskResponse(response, "删除定时任务失败");
}

/**
 * 启用/禁用微信公众号定时任务
 */
export async function toggleWechatTask(id: string): Promise<{ success: boolean; item?: WechatTask }> {
  const response = await fetch(`${API_BASE}/wechat/tasks/${id}/toggle`, {
    method: "POST",
  });
  return readWechatTaskResponse(response, "更新定时任务状态失败");
}

/**
 * 立即执行微信公众号定时任务
 */
export async function runWechatTask(id: string): Promise<{ success: boolean; message?: string }> {
  const response = await fetch(`${API_BASE}/wechat/tasks/${id}/run`, {
    method: "POST",
  });
  return readWechatTaskResponse(response, "执行定时任务失败");
}

/**
 * 爬取微信公众号文章
 */
export async function crawlWechatArticles(data: {
  urls: string[];
  categoryId?: string;
}): Promise<{ success: boolean; message?: string; job_id?: string; jobId?: string; status?: string }> {
  const response = await fetch(`${API_BASE}/wechat/crawl`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      urls: data.urls,
      category_id: data.categoryId,
    }),
  });
  return response.json();
}

export interface WechatAccountArticleProfile {
  name: string;
  wechatId?: string;
  sampleArticleUrl: string;
  sampleArticleTitle?: string;
  sampleArticlePublishedAt?: string;
}

/** 从一篇典型文章自动识别并创建公众号档案。 */
export async function createWechatAccountFromArticle(articleUrl: string): Promise<{
  success: boolean;
  created: boolean;
  message: string;
  item?: WechatAccount;
  profile?: WechatAccountArticleProfile;
}> {
  const response = await fetch(`${API_BASE}/wechat/accounts/from-article`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ article_url: articleUrl }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || result.error || `识别公众号失败: ${response.status}`);
  return result;
}

/**
 * 更新微信公众号 Cookie。更新 Cookie JSON 后，后端会自动续期并重新启用。
 */
export async function updateWechatCookie(id: string, data: {
  name?: string;
  cookieData?: string;
  expiresAt?: string;
}): Promise<{ success: boolean; item?: WechatCookie }> {
  const response = await fetch(`${API_BASE}/wechat/cookies/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: data.name,
      cookie_data: data.cookieData,
      expires_at: data.expiresAt,
    }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || result.error || `更新 Cookie 失败: ${response.status}`);
  return result;
}

export async function crawlWechatAccountRange(data: {
  accountId: string;
  startDate: string;
  endDate: string;
  maxArticles?: number;
  categoryId?: string;
  repeatIntervalMinutes?: number;
}): Promise<{ success: boolean; message?: string; detail?: string; job_id?: string; discovered_count?: number; cached?: boolean; next_allowed_at?: string }> {
  const response = await fetch(`${API_BASE}/wechat/accounts/${encodeURIComponent(data.accountId)}/crawl-range`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      start_date: data.startDate,
      end_date: data.endDate,
      max_articles: data.maxArticles ?? 50,
      category_id: data.categoryId,
      repeat_interval_minutes: data.repeatIntervalMinutes ?? 60,
    }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || result.error || `提交失败: ${response.status}`);
  return result;
}

export interface WechatCrawlJob {
  job_id: string;
  status: "pending" | "running" | "completed" | "failed";
  total: number;
  success_count: number;
  failed_count: number;
  message: string;
  results: Array<{ success: boolean; article_id?: string; title?: string; url?: string; error?: string }>;
}

export async function fetchWechatCrawlJob(jobId: string): Promise<WechatCrawlJob> {
  const response = await fetch(`${API_BASE}/wechat/crawl/${encodeURIComponent(jobId)}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`获取爬取结果失败: ${response.status}`);
  return response.json();
}

export interface WechatPublicDiscoveryJob {
  job_id: string;
  status: "pending" | "running" | "completed" | "ingesting" | "ingested" | "failed";
  candidate_count: number;
  eligible_count: number;
  verified_count: number;
  rejected_count: number;
  success_count: number;
  failed_count: number;
  message: string;
  sources: Record<string, number>;
  candidates: Array<{
    url: string;
    title: string;
    account_name: string;
    published_at: string;
    eligible: boolean;
    reason: string;
  }>;
  results: Array<{ success: boolean; article_id?: string; title?: string; url?: string; published_at?: string }>;
  rejected: Array<{ url: string; reason: string }>;
}

export async function startWechatPublicDiscovery(data: {
  accountId: string;
  startDate: string;
  endDate: string;
  seedUrls?: string[];
  maxArticles?: number;
  categoryId?: string;
}): Promise<{ success: boolean; job_id: string; status: string; message: string }> {
  const response = await fetch(`${API_BASE}/wechat/accounts/${encodeURIComponent(data.accountId)}/public-discovery`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      start_date: data.startDate,
      end_date: data.endDate,
      seed_urls: data.seedUrls || [],
      max_articles: data.maxArticles ?? 30,
      category_id: data.categoryId,
    }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || result.error || `提交公开来源发现任务失败: ${response.status}`);
  return result;
}

export async function fetchWechatPublicDiscoveryJob(jobId: string): Promise<WechatPublicDiscoveryJob> {
  const response = await fetch(`${API_BASE}/wechat/public-discovery/${encodeURIComponent(jobId)}`, { cache: "no-store" });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || result.error || `获取公开来源发现结果失败: ${response.status}`);
  return result;
}

export async function ingestWechatPublicDiscoveryCandidates(data: {
  jobId: string;
  urls: string[];
  categoryId?: string;
}): Promise<{ success: boolean; job_id: string; status: string; message: string }> {
  const response = await fetch(`${API_BASE}/wechat/public-discovery/${encodeURIComponent(data.jobId)}/ingest`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ urls: data.urls, category_id: data.categoryId }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || result.error || `候选文章入库失败: ${response.status}`);
  return result;
}
