import { create } from "zustand";
import type {
  ScrapeOptions,
  ScrapeResult,
  DateRangePreset,
  CustomDateRange,
  ScrapeLevel,
  TabTree,
  TabAnalyzeResult,
} from "@/types";
import { analyzeTabs as apiAnalyzeTabs } from "@/lib/api";

/**
 * 将后端返回的 snake_case 转换为前端 camelCase 格式
 */
function normalizeScrapeResult(raw: Record<string, unknown>): ScrapeResult {
  return {
    url: String(raw.url || ""),
    title: String(raw.title || ""),
    content: String(raw.content || ""),
    html: raw.html ? String(raw.html) : undefined,
    wordCount: Number(raw.word_count ?? raw.wordCount ?? 0),
    links: Array.isArray(raw.links) ? raw.links.map(String) : [],
    status: (raw.status === "error" ? "error" : raw.status === "anti_bot_blocked" ? "anti_bot_blocked" : "success") as ScrapeResult["status"],
    errorMessage: raw.error_message as string | undefined,
    scrapedAt: String(raw.scraped_at || raw.scrapedAt || new Date().toISOString()),
    // 新增字段
    publishedAt: raw.published_at as string | undefined,
    author: raw.author as string | undefined,
    summary: raw.summary as string | undefined,
    keywords: Array.isArray(raw.keywords) ? raw.keywords.map(String) : [],
    style: raw.style as string | undefined,  // 文体
    dbId: raw.db_id as string | undefined,   // 数据库 ID
    // 反爬相关
    needsCookie: raw.needs_cookie as boolean | undefined,
    blockedDomain: raw.blocked_domain as string | undefined,
    crawlRoute: raw.crawl_route as ScrapeResult["crawlRoute"],
    // 来源信息（用于文章列表显示）
    sourceId: raw.source_id as string | undefined,
    sourceName: raw.source_name as string | undefined,
    categoryId: raw.category_id as string | undefined,
    categoryName: raw.category_name as string | undefined,
  };
}

interface ScrapeStore {
  // 状态
  results: ScrapeResult[];
  isScraping: boolean;
  isCancelling: boolean;  // 是否正在取消
  currentResult: ScrapeResult | null;
  error: string | null;
  progress: {
    current: number;
    total: number;
    currentTitle?: string;
    currentUrl?: string;
    stage?: number;        // 当前阶段 1-5
    stageName?: string;    // 阶段名称
    stageDetail?: string;  // 阶段详情
  } | null;

  // 页签识别状态
  tabTree: TabTree | null;           // 页签树
  selectedTabIds: string[];          // 选中的节点ID
  isAnalyzingTabs: boolean;          // 是否正在分析页签
  tabError: string | null;           // 页签分析错误

  // Actions
  scrapeUrl: (url: string, options?: Partial<ScrapeOptions>, cookies?: string) => Promise<ScrapeResult | null>;
  scrapeBatch: (urls: string[], options?: Partial<ScrapeOptions>) => Promise<ScrapeResult[]>;
  scrapeSources: (sourceIds?: string[], options?: Partial<ScrapeOptions>) => Promise<ScrapeResult[]>;
  deepScrape: (
    url: string,
    maxArticles?: number,
    dateRange?: DateRangePreset,
    customDateRange?: CustomDateRange,
    options?: Partial<ScrapeOptions>,
    scrapeLevel?: ScrapeLevel,
    cookies?: string,
    categoryId?: string,
    sourceId?: string
  ) => Promise<{ success: boolean; scrapeId?: string; blocked?: boolean; error?: string }>;
  cancelScrape: () => Promise<void>;
  clearResults: () => void;
  setCurrentResult: (result: ScrapeResult | null) => void;
  // 进度订阅
  subscribeProgress: (scrapeId: string) => () => void;

  // 页签识别 Actions
  analyzeTabs: (url: string, options?: { includeNav?: boolean; includeTabs?: boolean }) => Promise<TabAnalyzeResult | null>;
  clearTabTree: () => void;
  setSelectedTabIds: (ids: string[]) => void;
}

const defaultOptions: ScrapeOptions = {
  extractContent: true,
  fetchHtml: false,
  preserveFormat: false,
  maxDepth: 0,
  timeout: 30,
};

export const useScrapeStore = create<ScrapeStore>((set, get) => ({
  results: [],
  isScraping: false,
  isCancelling: false,
  currentResult: null,
  error: null,
  progress: null,

  // 页签识别状态
  tabTree: null,
  selectedTabIds: [],
  isAnalyzingTabs: false,
  tabError: null,

  /**
   * 爬取单个 URL
   */
  scrapeUrl: async (url: string, options?: Partial<ScrapeOptions>, cookies?: string) => {
    set({ isScraping: true, error: null });

    try {
      const requestData: Record<string, unknown> = {
        url,
        options: { ...defaultOptions, ...options },
      };
      if (cookies) {
        requestData.cookies = cookies;
      }

      const res = await fetch("/api/scrape", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestData),
      });

      if (!res.ok) {
        const errorData = await res.json();
        throw new Error(errorData.error || "爬取失败");
      }

      const rawResult = await res.json();
      const result = normalizeScrapeResult(rawResult);

      // 更新历史记录
      set((state) => ({
        results: [result, ...state.results],
        currentResult: result,
        isScraping: false,
      }));

      return result;
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "未知错误";
      set({ error: errorMessage, isScraping: false });
      return null;
    }
  },

  /**
   * 批量爬取多个 URL
   */
  scrapeBatch: async (urls: string[], options?: Partial<ScrapeOptions>) => {
    set({ isScraping: true, error: null });

    try {
      const requestData = {
        urls,
        options: { ...defaultOptions, ...options },
      };

      const res = await fetch("/api/scrape/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestData),
      });

      if (!res.ok) {
        const errorData = await res.json();
        throw new Error(errorData.error || "批量爬取失败");
      }

      const rawResults = await res.json();
      const results = rawResults.map((r: Record<string, unknown>) => normalizeScrapeResult(r));

      // 更新历史记录
      set((state) => ({
        results: [...results, ...state.results],
        currentResult: results[0] || null,
        isScraping: false,
      }));

      return results;
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "未知错误";
      set({ error: errorMessage, isScraping: false });
      return [];
    }
  },

  /**
   * 从配置的爬取源爬取
   */
  scrapeSources: async (sourceIds?: string[], options?: Partial<ScrapeOptions>) => {
    set({ isScraping: true, error: null });

    try {
      const requestData = {
        source_ids: sourceIds,
        options: { ...defaultOptions, ...options },
      };

      const res = await fetch("/api/scrape/sources", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestData),
      });

      if (!res.ok) {
        const errorData = await res.json();
        throw new Error(errorData.error || "从爬取源爬取失败");
      }

      const rawResults = await res.json();
      const results = rawResults.map((r: Record<string, unknown>) => normalizeScrapeResult(r));

      // 更新历史记录
      set((state) => ({
        results: [...results, ...state.results],
        currentResult: results[0] || null,
        isScraping: false,
      }));

      return results;
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "未知错误";
      set({ error: errorMessage, isScraping: false });
      return [];
    }
  },

  /**
   * 清空历史记录
   */
  clearResults: () => {
    set({ results: [], currentResult: null, progress: null });
  },

  /**
   * 深度爬取：从列表页自动识别并爬取文章
   * 使用轮询方式获取后端真实进度
   */
  deepScrape: async (
    url: string,
    maxArticles: number = 10,
    dateRange?: DateRangePreset,
    customDateRange?: CustomDateRange,
    options?: Partial<ScrapeOptions>,
    scrapeLevel?: ScrapeLevel,
    cookies?: string,
    categoryId?: string,
    sourceId?: string
  ) => {
    set({ isScraping: true, error: null, progress: { current: 0, total: 1, stage: 0, stageName: "正在启动...", stageDetail: "准备爬取任务" } });

    let scrapeId = "";

    try {
      const requestData: Record<string, unknown> = {
        url,
        options: { ...defaultOptions, ...options },
        max_articles: maxArticles,
        scrape_level: scrapeLevel,
      };

      // 添加日期范围参数
      if (dateRange) {
        requestData.date_range = dateRange;
      }

      if (customDateRange && (customDateRange.startDate || customDateRange.endDate)) {
        requestData.custom_date_range = {
          start_date: customDateRange.startDate || null,
          end_date: customDateRange.endDate || null,
        };
      }

      // 添加 cookies 参数
      if (cookies) {
        requestData.cookies = cookies;
      }

      // 添加分类和来源参数（即使为空也强制发送）
      console.log('[DEBUG] deepScrape 参数: categoryId=', categoryId, 'sourceId=', sourceId);
      requestData.category_id = categoryId || null;
      requestData.source_id = sourceId || null;

      // 调用 API（后端会在后台线程执行爬取，立即返回 scrape_id）
      console.log('发送爬取请求:', JSON.stringify(requestData, null, 2));
      const response = await fetch("/api/scrape/deep", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestData),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.error || "深度爬取失败");
      }

      const data = await response.json();
      scrapeId = data.scrape_id;

      // 轮询进度
      const pollProgress = async () => {
        let maxPolls = 600; // 最多轮询 20 分钟（2秒 * 600）
        const poll = async (): Promise<void> => {
          if (!get().isScraping || maxPolls <= 0) return;

          try {
            const res = await fetch(`/api/scrape/progress/${scrapeId}`);
            const progress = await res.json();

            if (progress.status === "error") {
              set({ error: progress.stage_detail || "爬取失败", isScraping: false, progress: null });
              return;
            }

            if (progress.status === "completed" && progress.results) {
              // 爬取完成，处理结果（不要包含列表页本身）
              const articles: ScrapeResult[] = (progress.results.articles || []).map((r: Record<string, unknown>) =>
                normalizeScrapeResult(r)
              );

              // 检查列表页是否被反爬拦截
              const listPage = progress.results.list_page;
              if (articles.length === 0 && listPage && listPage.status === "anti_bot_blocked") {
                // 返回一个特殊的结果，让页面层处理 Cookie 对话框
                const blockedResult = normalizeScrapeResult(listPage);
                set({
                  results: [blockedResult],
                  currentResult: blockedResult,
                  isScraping: false,
                  progress: null,
                });
                return;
              }

              set({
                // 替换为新结果（不再追加旧结果）
                results: articles,
                // 选中第一篇文章
                currentResult: articles[0] || null,
                isScraping: false,
                progress: {
                  current: articles.length,
                  total: articles.length,
                  stage: 5,
                  stageName: "已完成",
                  stageDetail: `成功爬取 ${articles.length} 篇文章`,
                }
              });
              return;
            }

            if (progress.status === "scraping" || progress.status === "starting") {
              // 更新进度到 store
              set({
                progress: {
                  current: progress.current || 0,
                  total: progress.total || 1,
                  stage: progress.stage || 0,
                  stageName: progress.stage_name || "处理中",
                  stageDetail: progress.stage_detail || "",
                  currentTitle: progress.current_article || undefined,
                }
              });
            }

            // 继续轮询
            maxPolls--;
            setTimeout(poll, 2000); // 每 2 秒轮询一次
          } catch (e) {
            console.error("轮询进度失败:", e);
            maxPolls--;
            setTimeout(poll, 2000);
          }
        };

        await poll();
      };

      // 开始轮询
      pollProgress();

      return { success: true, scrapeId };
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "未知错误";
      set({ error: errorMessage, isScraping: false, progress: null });
      return { success: false, error: errorMessage };
    }
  },

  /**
   * 订阅爬取进度（SSE）
   */
  subscribeProgress: (scrapeId: string) => {
    let cancelled = false;

    const handleMessage = (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data);

        if (data.event === "stage") {
          // 阶段更新（如：正在解析列表页、正在识别文章链接等）
          set({
            progress: {
              current: 0,
              total: 1,
              stage: data.data.stage,
              stageName: data.data.name,
              stageDetail: data.data.detail,
            }
          });
        } else if (data.event === "progress") {
          // 文章爬取进度更新
          set({
            progress: {
              current: data.data.completed,
              total: data.data.total,
              currentTitle: data.data.current?.title,
              currentUrl: data.data.current?.url,
              stage: 3, // 正在爬取文章阶段
              stageName: "正在爬取文章",
              stageDetail: `已爬取 ${data.data.completed}/${data.data.total} 篇`,
            }
          });
        } else if (data.event === "complete") {
          set({
            isScraping: false,
            progress: {
              current: data.data.completed,
              total: data.data.total,
              stage: 5,
              stageName: "已完成",
              stageDetail: `成功爬取 ${data.data.articles_count || data.data.success} 篇文章`,
            }
          });
          cancelled = true;
        }
      } catch (e) {
        console.error("解析进度事件失败:", e);
      }
    };

    const eventSource = new EventSource(`/api/scrape/progress/${scrapeId}`);
    eventSource.onmessage = handleMessage;

    // 返回取消订阅的函数
    return () => {
      eventSource.close();
    };
  },

  /**
   * 设置当前结果
   */
  setCurrentResult: (result: ScrapeResult | null) => {
    set({ currentResult: result });
  },

  /**
   * 取消当前爬取
   */
  cancelScrape: async () => {
    if (!get().isScraping) return;

    set({ isCancelling: true, error: null });

    try {
      const res = await fetch("/api/scrape/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });

      if (res.ok) {
        // 等待一小段时间让后端处理取消
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    } catch (error) {
      console.error("取消爬取失败:", error);
    } finally {
      set({ isCancelling: false, isScraping: false, progress: null });
    }
  },

  /**
   * 分析页面的页签结构
   */
  analyzeTabs: async (url: string, options?: { includeNav?: boolean; includeTabs?: boolean }) => {
    set({ isAnalyzingTabs: true, tabError: null });

    try {
      const result = await apiAnalyzeTabs({
        url,
        includeNav: options?.includeNav ?? true,
        includeTabs: options?.includeTabs ?? true,
      });

      if (result.success && result.tree) {
        // 默认选中所有顶级分类
        const topLevelIds = (result.tree.root.children || []).map(c => c.id);
        set({
          tabTree: result.tree,
          selectedTabIds: topLevelIds,
          isAnalyzingTabs: false,
        });
        return result;
      } else {
        set({
          tabError: result.error || "页签分析失败",
          isAnalyzingTabs: false,
        });
        return null;
      }
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "未知错误";
      set({ tabError: errorMessage, isAnalyzingTabs: false });
      return null;
    }
  },

  /**
   * 清空页签树
   */
  clearTabTree: () => {
    set({ tabTree: null, selectedTabIds: [], tabError: null });
  },

  /**
   * 设置选中的节点ID
   */
  setSelectedTabIds: (ids: string[]) => {
    set({ selectedTabIds: ids });
  },
}));
