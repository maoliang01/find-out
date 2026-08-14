"""
网页爬取 API
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, HttpUrl
from typing import Optional, List, Literal
from datetime import datetime, date
import asyncio
import json

# 全局取消事件管理器
class CancelManager:
    """爬取取消管理器"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._cancel_event = asyncio.Event()
            cls._instance._current_scrape_id = None
        return cls._instance

    def start_scrape(self, scrape_id: str):
        """开始新的爬取，清除之前的取消状态"""
        self._cancel_event.clear()
        self._current_scrape_id = scrape_id

    def cancel(self):
        """取消当前爬取"""
        self._cancel_event.set()
        return self._cancel_event

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def get_cancel_event(self) -> asyncio.Event:
        return self._cancel_event


cancel_manager = CancelManager()

from app.services.scraper import get_scraper, ScrapeOptions, ScrapedResult

router = APIRouter(prefix="/scrape", tags=["网页爬取"])


class ScrapeRequest(BaseModel):
    """爬取请求"""
    url: str
    options: Optional[ScrapeOptions] = None
    save_to_db: bool = False  # 是否保存到数据库
    category_id: Optional[str] = None  # 分类 ID
    source_id: Optional[str] = None  # 来源 ID
    cookies: Optional[str] = None  # Cookie 字符串，用于绕过反爬


class ScrapeBatchRequest(BaseModel):
    """批量爬取请求"""
    urls: List[str]
    options: Optional[ScrapeOptions] = None
    save_to_db: bool = False  # 是否保存到数据库
    category_id: Optional[str] = None  # 分类 ID
    source_ids: Optional[List[str]] = None  # 来源 ID 列表（与 urls 一一对应）
    cookies: Optional[str] = None  # Cookie 字符串，用于绕过反爬


class ScrapeSourcesRequest(BaseModel):
    """从配置源爬取请求"""
    source_ids: Optional[List[str]] = None
    options: Optional[ScrapeOptions] = None
    cookies: Optional[str] = None  # Cookie 字符串，用于绕过反爬


class ScrapedResultResponse(BaseModel):
    """爬取结果响应"""
    url: str
    title: str
    content: str
    html: str
    word_count: int
    links: List[str]
    status: str
    error_message: Optional[str] = None
    scraped_at: str
    published_at: Optional[str] = None
    author: Optional[str] = None
    summary: Optional[str] = None
    keywords: List[str] = []
    style: Optional[str] = None  # 文体
    db_id: Optional[str] = None  # 数据库文章 ID（保存后返回）
    needs_cookie: bool = False  # 是否需要 Cookie 才能继续
    blocked_domain: Optional[str] = None  # 被反爬的域名


def _result_to_response(result: ScrapedResult) -> ScrapedResultResponse:
    """转换爬取结果为响应模型"""
    # 检查是否被反爬
    needs_cookie = result.status == "anti_bot_blocked"
    blocked_domain = None
    if needs_cookie and result.error_message:
        # 从错误信息中提取域名
        import re
        domain_match = re.search(r'\(([^)]+)\)', result.error_message)
        if domain_match:
            blocked_domain = domain_match.group(1)

    return ScrapedResultResponse(
        url=result.url,
        title=result.title,
        content=result.content,
        html=result.html,
        word_count=result.word_count,
        links=result.links,
        status=result.status,
        error_message=result.error_message,
        scraped_at=result.scraped_at,
        published_at=result.published_at,
        author=result.author,
        summary=result.summary,
        keywords=result.keywords,
        style=result.style,  # 文体
        db_id=getattr(result, "db_id", None),  # 获取数据库 ID
        needs_cookie=needs_cookie,
        blocked_domain=blocked_domain,
    )


@router.post("")
async def scrape_url(request: ScrapeRequest):
    """爬取单个网页"""
    scraper = get_scraper()
    options = request.options or ScrapeOptions()

    # 添加cookies到选项
    if request.cookies:
        options.cookies = request.cookies

    result = await scraper.scrape(request.url, options)

    # 如果设置了保存到数据库
    if request.save_to_db and result.status == "success":
        saved, info = scraper.save_to_database(
            result,
            category_id=request.category_id,
            source_id=request.source_id
        )
        if saved:
            result.db_id = info  # 将保存的文章 ID 附加到结果中

    return _result_to_response(result)


@router.post("/batch")
async def scrape_batch(request: ScrapeBatchRequest):
    """批量爬取多个网页"""
    if not request.urls:
        raise HTTPException(status_code=400, detail="URL 列表不能为空")

    if len(request.urls) > 50:
        raise HTTPException(status_code=400, detail="最多同时爬取 50 个 URL")

    scraper = get_scraper()
    options = request.options or ScrapeOptions()
    results = await scraper.scrape_batch(request.urls, options)

    # 如果设置了保存到数据库
    if request.save_to_db:
        saved_count = 0
        for i, result in enumerate(results):
            if result.status == "success":
                source_id = None
                if request.source_ids and i < len(request.source_ids):
                    source_id = request.source_ids[i]

                saved, info = scraper.save_to_database(
                    result,
                    category_id=request.category_id,
                    source_id=source_id
                )
                if saved:
                    saved_count += 1
                    result.db_id = info

    return [_result_to_response(r) for r in results]


@router.post("/sources")
async def scrape_sources(request: ScrapeSourcesRequest):
    """从配置的爬取源爬取"""
    from app.api.settings import settings_store

    sources = settings_store.get_settings().get("scrape_sources", [])
    enabled_sources = [s for s in sources if s.get("is_enabled", True)]

    if request.source_ids:
        enabled_sources = [s for s in enabled_sources if s.get("id") in request.source_ids]

    if not enabled_sources:
        raise HTTPException(status_code=404, detail="没有找到启用的爬取源")

    urls = [s["url"] for s in enabled_sources]
    scraper = get_scraper()
    options = request.options or ScrapeOptions()
    results = await scraper.scrape_batch(urls, options)

    return [_result_to_response(r) for r in results]


@router.get("/sources")
async def get_scrape_sources():
    """获取所有爬取源列表"""
    from app.api.settings import settings_store
    sources = settings_store.get_settings().get("scrape_sources", [])
    return sources


@router.get("/test")
async def test_scrape():
    """测试爬取功能"""
    scraper = get_scraper()
    result = await scraper.scrape("https://httpbin.org/html")
    return _result_to_response(result)


class DateRangeModel(BaseModel):
    """日期范围模型"""
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class ScrapeDeepRequest(BaseModel):
    """深度爬取请求"""
    url: str
    options: Optional[ScrapeOptions] = None
    max_articles: int = 10
    date_range: Optional[Literal["today", "week", "month"]] = None
    custom_date_range: Optional[DateRangeModel] = None
    scrape_level: Optional[Literal["list", "detail", "deep"]] = "deep"
    scrape_id: Optional[str] = None
    cookies: Optional[str] = None  # Cookie 字符串，用于绕过反爬
    save_to_db: bool = True  # 是否自动保存到数据库
    category_id: Optional[str] = None  # 分类 ID
    source_id: Optional[str] = None  # 来源 ID


class DeepScrapeResponse(BaseModel):
    """深度爬取响应"""
    scrape_id: str
    status: str
    error_message: Optional[str] = None
    list_page: Optional[ScrapedResultResponse] = None
    articles: List[ScrapedResultResponse] = []
    total_articles: int = 0


@router.post("/deep", response_model=DeepScrapeResponse)
async def scrape_deep(request: ScrapeDeepRequest):
    """
    深度爬取：在后台线程执行爬取，立即返回 scrape_id

    前端获取 scrape_id 后，轮询 /scrape/progress/{scrape_id} 获取实时进度
    """
    from app.services.scraper import WebScraper, scrape_logger, progress_manager
    import logging
    from concurrent.futures import ThreadPoolExecutor

    api_logger = logging.getLogger(__name__)
    api_logger.info(f"[DEBUG] 接收请求: url={request.url}, date_range={request.date_range}, scrape_level={request.scrape_level}, category_id={request.category_id}, source_id={request.source_id}, save_to_db={request.save_to_db}")

    # 生成 scrape_id
    import uuid
    scrape_id = request.scrape_id or str(uuid.uuid4())[:8]

    # 手动深度爬取也写入现有历史表，避免任务完成或服务重启后无法追踪。
    history_id = str(uuid.uuid4())
    try:
        from app.core.database import get_session_local
        from app.models.scheduled_task import ScrapeHistory, TaskStatus
        history_db = get_session_local()()
        try:
            history_db.add(ScrapeHistory(
                id=history_id,
                task_id=None,
                url=request.url,
                status=TaskStatus.RUNNING.value,
            ))
            history_db.commit()
        finally:
            history_db.close()
    except Exception as db_err:
        api_logger.warning(f"无法写入爬取历史记录（数据库不可用）: {db_err}")
        history_id = None

    # 初始化进度状态
    progress_manager.set_progress(scrape_id, {
        "status": "starting",
        "stage": 0,
        "stage_name": "正在启动...",
        "stage_detail": "准备爬取任务",
        "current": 0,
        "total": 0,
    })

    # 启动爬取并设置取消管理器
    cancel_manager.start_scrape(scrape_id)

    options = request.options or ScrapeOptions()

    # 添加 cookies 到选项
    if request.cookies:
        options.cookies = request.cookies

    max_articles = min(request.max_articles, 50)

    # 解析自定义日期范围
    custom_range = None
    if request.custom_date_range:
        start_dt = None
        end_dt = None

        if request.custom_date_range.start_date:
            start_dt = datetime.strptime(request.custom_date_range.start_date, "%Y-%m-%d").date()
        if request.custom_date_range.end_date:
            end_dt = datetime.strptime(request.custom_date_range.end_date, "%Y-%m-%d").date()

        # 确保起始日期 <= 结束日期（如果用户输反了，自动交换）
        if start_dt and end_dt and start_dt > end_dt:
            api_logger.warning(f"日期范围输入反了，自动交换: start={start_dt}, end={end_dt}")
            start_dt, end_dt = end_dt, start_dt

        custom_range = {
            "start_date": start_dt,
            "end_date": end_dt,
        }

    def do_scrape():
        """在后台线程中执行爬取"""
        def update_history(status: str, articles_count: int = 0, article_title: str = "", error_message: str = ""):
            if not history_id:
                return
            try:
                db = get_session_local()()
                try:
                    history = db.query(ScrapeHistory).filter(ScrapeHistory.id == history_id).first()
                    if history:
                        history.status = status
                        history.articles_count = articles_count
                        history.article_title = article_title[:500] if article_title else ("无文章" if articles_count == 0 else None)
                        history.error_message = error_message[:4000] if error_message else None
                        history.finished_at = datetime.utcnow()
                        db.commit()
                finally:
                    db.close()
            except Exception as e:
                api_logger.warning(f"更新爬取历史失败: {e}")

        try:
            # 创建新的事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                scraper = WebScraper(cancel_event=cancel_manager.get_cancel_event())

                # 进度回调函数
                def update_progress(stage: int, stage_name: str, stage_detail: str = "", current: int = 0, total: int = 0):
                    progress_manager.set_progress(scrape_id, {
                        "status": "scraping",
                        "stage": stage,
                        "stage_name": stage_name,
                        "stage_detail": stage_detail,
                        "current": current,
                        "total": total,
                    })

                # 更新进度：开始解析列表页
                update_progress(1, "正在解析列表页", f"访问 {request.url}")

                # 执行爬取
                list_page_result, article_results = loop.run_until_complete(
                    scraper.deep_scrape(
                        url=request.url,
                        options=options,
                        max_articles=max_articles,
                        date_range=request.date_range,
                        custom_date_range=custom_range,
                        scrape_level=request.scrape_level,
                        scrape_id=scrape_id,
                        progress_callback=update_progress
                    )
                )

                # 如果启用自动保存，保存到数据库
                saved_count = 0
                db_ids = []
                if request.save_to_db:
                    update_progress(4, "正在保存到数据库", f"保存 {len(article_results)} 篇文章...")
                    for result in article_results:
                        if result.status == "success" and result.content and result.word_count and result.word_count > 50:
                            # 传递分类和来源信息
                            saved, article_id = scraper.save_to_database(
                                result,
                                category_id=request.category_id,
                                source_id=request.source_id
                            )
                            if saved:
                                saved_count += 1
                                db_ids.append(str(article_id))
                                api_logger.info(f"  ✓ 自动保存: {result.title[:40]} [分类:{request.category_id}, 来源:{request.source_id}]")

                    api_logger.info(f"✅ [自动保存] 共保存 {saved_count} 篇文章到数据库")

                complete_count = sum(1 for result in article_results if result.status == "success")
                metadata_only_count = sum(1 for result in article_results if result.status == "metadata_only")
                result_summary = f"完整正文 {complete_count} 篇"
                if metadata_only_count:
                    result_summary += f"，不允许公开爬取且未保存 {metadata_only_count} 篇"

                # 保存结果到进度管理器
                progress_manager.set_progress(scrape_id, {
                    "status": "completed",
                    "stage": 5,
                    "stage_name": "已完成" + ("，已保存到数据库" if saved_count > 0 else ""),
                    "stage_detail": result_summary + (f"，保存 {saved_count} 篇" if request.save_to_db else ""),
                    "current": len(article_results),
                    "total": len(article_results),
                    "saved_to_db": saved_count,
                    "db_ids": db_ids,
                    "results": {
                        "list_page": _result_to_response(list_page_result).__dict__ if list_page_result else None,
                        "articles": [_result_to_response(r).__dict__ for r in article_results],
                        "total_articles": complete_count,
                        "metadata_only_articles": metadata_only_count,
                    }
                })
                history_status = (
                    TaskStatus.FAILED.value
                    if complete_count == 0 and metadata_only_count > 0
                    else TaskStatus.SUCCESS.value
                )
                update_history(
                    history_status,
                    complete_count,
                    "\n".join(r.title for r in article_results if r.status == "success" and r.title),
                    error_message=(
                        "详情页不允许公开爬取或未提供公开正文，未向文档管理保存任何记录"
                        if history_status == TaskStatus.FAILED.value else None
                    ),
                )

            finally:
                loop.close()
        except Exception as e:
            api_logger.error(f"后台爬取异常: {e}")
            update_history(TaskStatus.FAILED.value, error_message=str(e))
            progress_manager.set_progress(scrape_id, {
                "status": "error",
                "stage": -1,
                "stage_name": "爬取失败",
                "stage_detail": str(e),
                "current": 0,
                "total": 0,
                "error": str(e),
            })

    # 在后台线程执行爬取
    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(do_scrape)

    # 立即返回 scrape_id，前端可以轮询进度
    return DeepScrapeResponse(
        scrape_id=scrape_id,
        status="started",
        list_page=None,
        articles=[],
        total_articles=0
    )


@router.get("/progress/{scrape_id}")
async def get_progress(scrape_id: str):
    """获取爬取进度（轮询端点）"""
    from app.services.scraper import progress_manager
    progress = progress_manager.get_progress(scrape_id)
    # 如果没有进度记录，返回默认状态
    if not progress:
        return {
            "status": "not_found",
            "stage": 0,
            "stage_name": "未知",
            "stage_detail": "任务不存在或已过期",
            "current": 0,
            "total": 0,
        }
    return progress


@router.post("/cancel")
async def cancel_scrape():
    """取消当前正在进行的爬取任务"""
    if cancel_manager.is_cancelled:
        return {"status": "already_cancelled", "message": "爬取已经在取消中"}

    cancel_manager.cancel()
    progress_manager.emit("cancel", "cancelled", {"message": "爬取已取消"})
    return {"status": "cancelled", "message": "已发送取消信号"}


# ==================== 页签识别 API ====================

from app.schemas.tab_schema import (
    TabAnalyzeRequest, TabAnalyzeResponse,
    TabNodeModel, TabTreeModel
)
from app.services.tab_analyzer import TabAnalyzer, TabNode, TabTree


def _node_to_model(node: TabNode) -> TabNodeModel:
    """将内部 TabNode 转换为 Pydantic 模型"""
    return TabNodeModel(
        id=node.id,
        label=node.label,
        url=node.url,
        children=[_node_to_model(c) for c in node.children] if node.children else [],
        level=node.level,
        type=node.type,
        expandable=node.expandable,
        url_pattern=node.url_pattern,
    )


def _tree_to_model(tree: TabTree) -> TabTreeModel:
    """将内部 TabTree 转换为 Pydantic 模型"""
    return TabTreeModel(
        domain=tree.domain,
        site_title=tree.site_title,
        root=_node_to_model(tree.root),
        all_nodes=[_node_to_model(n) for n in tree.all_nodes],
        generated_at=tree.generated_at,
        total_count=tree.total_count,
    )


@router.post("/tabs", response_model=TabAnalyzeResponse)
async def analyze_tabs(request: TabAnalyzeRequest):
    """分析页面的页签结构，返回分类树"""
    analyzer = TabAnalyzer()

    result = await analyzer.analyze(
        url=request.url,
        include_nav=request.include_nav,
        include_tabs=request.include_tabs,
        max_depth=request.max_depth,
    )

    if result["success"]:
        return TabAnalyzeResponse(
            success=True,
            tree=_tree_to_model(result["tree"]),
            duration=result["duration"],
        )
    else:
        return TabAnalyzeResponse(
            success=False,
            error=result["error"],
            duration=result["duration"],
        )


# ==================== 导出相关 API ====================

class ExportRequest(BaseModel):
    """导出请求"""
    articles: List[dict]  # 文章列表
    format: str = "json"  # 导出格式: json, markdown, txt


class ExportResponse(BaseModel):
    """导出响应"""
    filename: str
    content: str
    size: int


@router.post("/export", response_model=ExportResponse)
async def export_articles(request: ExportRequest):
    """
    将爬取结果导出为文件内容
    前端可以选择保存到本地
    """
    if not request.articles:
        raise HTTPException(status_code=400, detail="没有可导出的文章")

    format_type = request.format.lower()
    articles = request.articles

    if format_type == "markdown":
        # Markdown 格式
        content_lines = [
            "# 爬取文章汇总",
            f"\n导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"\n共 {len(articles)} 篇文章\n",
        ]

        for i, article in enumerate(articles, 1):
            content_lines.append(f"\n---\n\n## {i}. {article.get('title', '无标题')}")

            # 元信息
            meta_parts = []
            if article.get("url"):
                meta_parts.append(f"URL: {article['url']}")
            if article.get("author"):
                meta_parts.append(f"作者: {article['author']}")
            if article.get("published_at"):
                meta_parts.append(f"发布时间: {article['published_at']}")
            if article.get("style"):
                meta_parts.append(f"文体: {article['style']}")
            if article.get("keywords"):
                meta_parts.append(f"关键词: {', '.join(article['keywords'])}")

            if meta_parts:
                content_lines.append("\n" + "\n".join(meta_parts))

            # 摘要
            if article.get("summary"):
                content_lines.append(f"\n**摘要:**\n\n{itemize['summary']}")

            # 正文
            if article.get("content"):
                content_lines.append(f"\n**正文:**\n\n{itemize['content']}")

        content = "\n".join(content_lines)
        filename = f"articles_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

    elif format_type == "txt":
        # 纯文本格式
        content_lines = [
            "=" * 50,
            f"爬取文章汇总 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"共 {len(articles)} 篇文章",
            "=" * 50,
        ]

        for i, article in enumerate(articles, 1):
            content_lines.append(f"\n\n{'=' * 40}")
            content_lines.append(f"文章 {i}: {article.get('title', '无标题')}")
            content_lines.append(f"{'=' * 40}")

            if article.get("url"):
                content_lines.append(f"链接: {article['url']}")
            if article.get("author"):
                content_lines.append(f"作者: {article['author']}")
            if article.get("published_at"):
                content_lines.append(f"时间: {article['published_at']}")
            if article.get("style"):
                content_lines.append(f"文体: {article['style']}")
            if article.get("summary"):
                content_lines.append(f"\n摘要:\n{itemize['summary']}")
            if article.get("content"):
                content_lines.append(f"\n正文:\n{itemize['content']}")

        content = "\n".join(content_lines)
        filename = f"articles_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

    else:
        # JSON 格式（默认）
        # 处理日期序列化
        def serialize_article(a):
            return {
                "title": a.get("title", ""),
                "url": a.get("url", ""),
                "author": a.get("author"),
                "published_at": a.get("published_at"),
                "style": a.get("style"),
                "summary": a.get("summary"),
                "keywords": a.get("keywords", []),
                "content": a.get("content", ""),
                "word_count": a.get("word_count", 0),
            }

        export_data = {
            "export_time": datetime.now().isoformat(),
            "total": len(articles),
            "articles": [serialize_article(a) for a in articles],
        }
        content = json.dumps(export_data, ensure_ascii=False, indent=2)
        filename = f"articles_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    return ExportResponse(
        filename=filename,
        content=content,
        size=len(content.encode("utf-8")),
    )


# ==================== 批量保存到数据库 API ====================

class SaveBatchRequest(BaseModel):
    """批量保存请求"""
    articles: List[dict]
    category_id: Optional[str] = None


class SaveBatchResponse(BaseModel):
    """批量保存响应"""
    success: bool
    saved: int
    failed: int
    db_ids: List[str] = []
    message: str


@router.post("/save-batch", response_model=SaveBatchResponse)
async def save_batch_to_database(request: SaveBatchRequest):
    """
    批量保存爬取结果到数据库

    需要先配置 PostgreSQL 数据库才能使用
    """
    scraper = get_scraper()
    saved_count = 0
    failed_count = 0
    db_ids = []
    messages = []

    for article_data in request.articles:
        try:
            result = ScrapedResult(
                url=article_data.get("url", ""),
                title=article_data.get("title", ""),
                content=article_data.get("content", ""),
                html=article_data.get("html", ""),
                word_count=article_data.get("word_count", 0),
                author=article_data.get("author"),
                summary=article_data.get("summary", ""),
                style=article_data.get("style"),
                published_at=article_data.get("published_at"),
                keywords=article_data.get("keywords", []),
                links=article_data.get("links", []),
                status="success",
            )

            saved, info = scraper.save_to_database(
                result,
                category_id=request.category_id,
                source_id=article_data.get("source_id"),
            )

            if saved:
                saved_count += 1
                db_ids.append(str(info))

                # 触发知识图谱同步：同步 metadata + 后台抽实体
                try:
                    from app.core.database import get_session_local
                    from app.models.article import Article
                    from app.services.kg_sync import on_article_sync_only, trigger_async_extraction

                    SessionLocal = get_session_local()
                    db = SessionLocal()
                    try:
                        article = db.query(Article).filter(Article.id == info).first()
                        if article:
                            await on_article_sync_only(article)
                            trigger_async_extraction(article.id)
                    finally:
                        db.close()
                except Exception as kg_error:
                    # KG 同步失败不影响保存成功响应，仅记录日志
                    import logging
                    logging.getLogger("ai-studio").warning(
                        f"文章 {info} KG 同步触发失败: {kg_error}"
                    )
            else:
                failed_count += 1
                messages.append(f"保存失败: {info}")

        except Exception as e:
            failed_count += 1
            messages.append(f"异常: {str(e)}")

    return SaveBatchResponse(
        success=failed_count == 0,
        saved=saved_count,
        failed=failed_count,
        db_ids=db_ids,
        message=f"成功保存 {saved_count} 篇，失败 {failed_count} 篇" if failed_count > 0 else f"成功保存 {saved_count} 篇文章",
    )
