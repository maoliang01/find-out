"""
定时任务 CRUD API

提供定时爬取任务的增删改查、定时调度和历史记录功能
"""
import logging
import json
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.time_utils import beijing_iso, beijing_now, local_to_utc_naive
from app.models.scheduled_task import (
    ScheduledTask, ScrapeHistory, TaskStatus
)
from app.models.article import ScrapeSource

logger = logging.getLogger("ai-studio")

# Manual runs must use the same limits as scheduled runs; otherwise the UI can
# report a different timeout than the scheduler for the same source.
IMMEDIATE_TASK_MAX_RUNTIME_SECONDS = int(
    os.getenv("SCHEDULED_TASK_MAX_RUNTIME_SECONDS", "1200")
)
IMMEDIATE_URL_TIMEOUT_SECONDS = int(
    os.getenv("SCHEDULED_URL_TIMEOUT_SECONDS", "180")
)
IMMEDIATE_PAGE_TIMEOUT_SECONDS = int(
    os.getenv("SCHEDULED_PAGE_TIMEOUT_SECONDS", "60")
)
IMMEDIATE_MAX_ARTICLES = int(os.getenv("SCHEDULED_MAX_ARTICLES", "10"))
_immediate_executor = ThreadPoolExecutor(
    max_workers=5,
    thread_name_prefix="scheduled-immediate",
)

router = APIRouter(prefix="/api/scheduled", tags=["定时任务"])


# ============ 辅助函数 ============

def _calculate_next_run(schedule_time: str) -> datetime:
    """计算下次执行时间"""
    hour, minute = map(int, schedule_time.split(":"))
    now = beijing_now()
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return local_to_utc_naive(next_run)


def _sync_scheduler():
    """同步调度器任务"""
    try:
        from app.core.scheduler import get_scheduler, sync_scheduler_tasks
        scheduler = get_scheduler()
        if scheduler:
            sync_scheduler_tasks(scheduler)
    except Exception:
        pass  # 调度器可能未初始化


def _get_source_name(source_id: Optional[str]) -> Optional[str]:
    """获取爬取源名称（从 settings.json）"""
    if not source_id:
        return None
    from app.api.settings import settings_store
    source = settings_store.scrape_sources.get(source_id)
    return source.get("name") if source else None


def _ensure_scrape_source_in_db(db: Session, settings_store, source_id: str):
    """确保爬取源和分类在数据库中存在（同步 settings.json → scrape_sources 表）"""
    from app.models.article import ScrapeSource, Category
    existing = db.query(ScrapeSource).filter(ScrapeSource.id == source_id).first()
    if existing:
        return
    src = settings_store.scrape_sources[source_id]
    category_id = src.get("category")
    if category_id and not db.query(Category).filter(Category.id == category_id).first():
        cat = settings_store.categories.get(category_id, {})
        db.add(Category(
            id=category_id,
            name=cat.get("name", category_id),
            color=cat.get("color", "#6B7280"),
            folder_name=cat.get("folder_name", cat.get("name", category_id)),
        ))
    db.add(ScrapeSource(
        id=source_id,
        name=src.get("name", source_id),
        url=src.get("url", ""),
        category_id=category_id,
        description=src.get("description", ""),
    ))
    db.flush()


def _task_to_response(task: ScheduledTask) -> dict:
    """将任务模型转换为响应字典"""
    source_ids_list = task.get_source_ids_list()

    # 获取源名称列表
    source_names = []
    for sid in source_ids_list:
        name = _get_source_name(sid)
        if name:
            source_names.append(name)

    return {
        "id": task.id,
        "name": task.name,
        "source_id": task.source_id,
        "source_ids": source_ids_list,
        "source_names": source_names,
        "custom_url": task.custom_url,
        "schedule_time": task.schedule_time,
        "scrape_range": task.scrape_range,
        "is_enabled": task.is_enabled,
        "last_run_at": beijing_iso(task.last_run_at),
        "next_run_at": beijing_iso(task.next_run_at),
        "created_at": beijing_iso(task.created_at),
        "updated_at": beijing_iso(task.updated_at),
    }


def _history_to_response(history: ScrapeHistory) -> dict:
    """将历史记录模型转换为响应字典"""
    from app.api.settings import settings_store

    duration = None
    if history.started_at and history.finished_at:
        duration = (history.finished_at - history.started_at).total_seconds()

    # 获取任务名称和源名称
    task_name = None
    source_name = None
    if history.task_id:
        from app.core.database import get_session_local
        db = get_session_local()()
        try:
            task = db.query(ScheduledTask).filter(ScheduledTask.id == history.task_id).first()
            if task:
                task_name = task.name
                if task.source_id:
                    source_name = _get_source_name(task.source_id)
        finally:
            db.close()

    return {
        "id": history.id,
        "task_id": history.task_id,
        "task_name": task_name,
        "source_name": source_name,
        "url": history.url,
        "article_title": history.article_title,
        "article_id": history.article_id,
        "status": history.status,
        "error_message": history.error_message,
        "started_at": beijing_iso(history.started_at),
        "finished_at": beijing_iso(history.finished_at),
        "duration": duration,
        "articles_count": history.articles_count,
        "created_at": beijing_iso(history.created_at),
    }


# ============ 基础 Pydantic Schemas ============

class ScheduledTaskCreate(BaseModel):
    name: str
    source_ids: Optional[List[str]] = []
    custom_url: Optional[str] = None
    schedule_time: str
    scrape_range: str = "1d"
    is_enabled: bool = False  # 新任务默认禁用，点击"启动"后才启用


class ScheduledTaskUpdate(BaseModel):
    name: Optional[str] = None
    source_ids: Optional[List[str]] = None
    custom_url: Optional[str] = None
    schedule_time: Optional[str] = None
    scrape_range: Optional[str] = None
    is_enabled: Optional[bool] = None


class ScheduledTaskResponse(BaseModel):
    id: str
    name: str
    source_ids: List[str]
    source_names: List[str]
    custom_url: Optional[str] = None
    schedule_time: str
    scrape_range: str
    is_enabled: bool
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ScrapeHistoryResponse(BaseModel):
    id: str
    task_id: Optional[str] = None
    task_name: Optional[str] = None
    source_name: Optional[str] = None
    url: str
    article_title: Optional[str] = None
    article_id: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration: Optional[float] = None
    articles_count: int = 0
    created_at: Optional[str] = None


class TaskStatsResponse(BaseModel):
    total_tasks: int
    enabled_tasks: int
    today_runs: int
    today_success: int
    today_failed: int


class RunningTaskResponse(BaseModel):
    """正在运行的任务信息"""
    running_count: int
    running_tasks: List[dict]  # 正在运行的历史记录列表


# ============ 基础列表/统计路由（无路径参数）============

@router.get("", response_model=List[ScheduledTaskResponse])
async def list_tasks(
    include_disabled: bool = Query(False, description="是否包含已禁用的任务"),
    db: Session = Depends(get_db)
):
    """获取所有定时任务"""
    query = db.query(ScheduledTask)
    if not include_disabled:
        query = query.filter(ScheduledTask.is_enabled == True)
    tasks = query.order_by(ScheduledTask.created_at.desc()).all()
    return [_task_to_response(t) for t in tasks]


@router.get("/stats", response_model=TaskStatsResponse)
async def get_stats(db: Session = Depends(get_db)):
    """获取任务统计"""
    total_tasks = db.query(ScheduledTask).count()
    enabled_tasks = db.query(ScheduledTask).filter(ScheduledTask.is_enabled == True).count()

    # 今日统计
    today = local_to_utc_naive(beijing_now().replace(hour=0, minute=0, second=0, microsecond=0))
    today_histories = db.query(ScrapeHistory).filter(
        ScrapeHistory.started_at >= today
    ).all()

    today_runs = len(today_histories)
    today_success = sum(1 for h in today_histories if h.status == TaskStatus.SUCCESS.value)
    today_failed = sum(1 for h in today_histories if h.status == TaskStatus.FAILED.value)

    return TaskStatsResponse(
        total_tasks=total_tasks,
        enabled_tasks=enabled_tasks,
        today_runs=today_runs,
        today_success=today_success,
        today_failed=today_failed,
    )


@router.get("/running", response_model=RunningTaskResponse)
async def get_running_tasks(db: Session = Depends(get_db)):
    """获取当前正在运行的爬取任务"""
    # 只查询状态为 running 的历史记录（最近4小时内的，避免遗漏长时间运行的任务）
    # 使用 UTC 时间保持与数据库一致
    cutoff_time = datetime.utcnow() - timedelta(hours=4)
    running_histories = db.query(ScrapeHistory).filter(
        ScrapeHistory.status == TaskStatus.RUNNING.value,
        ScrapeHistory.started_at >= cutoff_time
    ).order_by(ScrapeHistory.started_at.desc()).all()

    running_tasks = []
    for h in running_histories:
        # 计算已运行时长
        elapsed = None
        if h.started_at:
            elapsed = (datetime.utcnow() - h.started_at).total_seconds()

        running_tasks.append({
            "id": h.id,
            "task_id": h.task_id,
            "task_name": _history_to_response(h).get("task_name"),
            "url": h.url,
            "started_at": beijing_iso(h.started_at),
            "elapsed_seconds": elapsed,
        })

    return RunningTaskResponse(
        running_count=len(running_tasks),
        running_tasks=running_tasks,
    )


# ============ 历史记录路由 ============

@router.get("/history", response_model=List[ScrapeHistoryResponse])
async def list_history(
    task_id: Optional[str] = Query(None, description="按任务ID过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    limit: Optional[int] = Query(None, description="返回数量限制（别名，用于兼容）"),
    db: Session = Depends(get_db)
):
    # 使用 limit 参数覆盖 page_size（如果提供）
    if limit is not None and limit > 0:
        page_size = min(limit, 200)
    """获取爬取历史记录"""
    query = db.query(ScrapeHistory)

    if task_id:
        query = query.filter(ScrapeHistory.task_id == task_id)

    histories = query.order_by(ScrapeHistory.started_at.desc()).offset((page - 1) * page_size).limit(page_size).all()

    return [_history_to_response(h) for h in histories]


@router.get("/history/summary")
async def get_history_summary(db: Session = Depends(get_db)):
    """获取每日爬取汇总（最近7天）"""
    today = local_to_utc_naive(beijing_now().replace(hour=0, minute=0, second=0, microsecond=0))
    week_ago = today - timedelta(days=7)

    histories = db.query(ScrapeHistory).filter(
        ScrapeHistory.started_at >= week_ago
    ).order_by(ScrapeHistory.started_at.desc()).all()

    # 按日期分组
    daily_summary = {}
    for h in histories:
        date_str = h.started_at.strftime("%Y-%m-%d")
        if date_str not in daily_summary:
            daily_summary[date_str] = {
                "date": date_str,
                "total": 0,
                "success": 0,
                "failed": 0,
                "articles": 0,
            }
        daily_summary[date_str]["total"] += 1
        if h.status == TaskStatus.SUCCESS.value:
            daily_summary[date_str]["success"] += 1
            daily_summary[date_str]["articles"] += h.articles_count
        elif h.status == TaskStatus.FAILED.value:
            daily_summary[date_str]["failed"] += 1

    return list(daily_summary.values())


@router.delete("/history/{history_id}")
async def delete_history(history_id: str, db: Session = Depends(get_db)):
    """删除历史记录"""
    history = db.query(ScrapeHistory).filter(ScrapeHistory.id == history_id).first()
    if not history:
        raise HTTPException(status_code=404, detail="历史记录不存在")

    db.delete(history)
    db.commit()
    return {"message": "历史记录已删除"}


# ============ 任务 CRUD 路由 ============

@router.post("", response_model=ScheduledTaskResponse)
async def create_task(request: ScheduledTaskCreate, db: Session = Depends(get_db)):
    """创建定时任务"""
    # 验证时间格式
    try:
        hour, minute = map(int, request.schedule_time.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Invalid time")
    except ValueError:
        raise HTTPException(status_code=400, detail="时间格式错误，请使用 HH:MM 格式")

    # 验证爬取范围
    if request.scrape_range not in ["1d", "7d", "30d"]:
        raise HTTPException(status_code=400, detail="爬取范围只能是 1d、7d 或 30d")

    # 验证爬取源存在（从 settings.json 读取）
    if request.source_ids:
        from app.api.settings import settings_store
        for sid in request.source_ids:
            if sid not in settings_store.scrape_sources:
                raise HTTPException(status_code=400, detail=f"爬取源不存在: {sid}")

        # 同步爬取源到数据库（确保外键约束满足）
        for sid in request.source_ids:
            _ensure_scrape_source_in_db(db, settings_store, sid)
        db.commit()

    # 计算下次执行时间
    next_run_at = _calculate_next_run(request.schedule_time)

    # 保存源ID列表
    source_id = request.source_ids[0] if request.source_ids else None

    task = ScheduledTask(
        name=request.name,
        source_ids=json.dumps(request.source_ids) if request.source_ids else None,
        source_id=source_id,
        custom_url=request.custom_url,
        schedule_time=request.schedule_time,
        scrape_range=request.scrape_range,
        is_enabled=request.is_enabled,
        next_run_at=next_run_at,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # 同步调度器
    _sync_scheduler()

    return _task_to_response(task)


# ============ 调度器管理路由（必须在 /{task_id} 之前） ============

@router.post("/sync-scheduler")
async def sync_scheduler_api():
    """手动同步调度器任务（用于服务重启后或任务状态不一致时）"""
    try:
        from app.core.scheduler import get_scheduler, sync_scheduler_tasks, _update_next_run_times

        scheduler = get_scheduler()
        if not scheduler:
            return {"status": "error", "message": "调度器未运行"}

        # 更新下次执行时间
        _update_next_run_times()

        # 同步任务到调度器
        sync_scheduler_tasks(scheduler)

        return {"status": "success", "message": "调度器已同步"}
    except Exception as e:
        logger.error(f"同步调度器失败: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/scheduler-jobs")
async def get_scheduler_jobs():
    """获取调度器中当前的任务列表"""
    try:
        from app.core.scheduler import get_scheduler

        scheduler = get_scheduler()
        if not scheduler:
            return {"status": "error", "message": "调度器未运行"}

        jobs = []
        for job in scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            })

        return {"status": "success", "jobs": jobs, "total": len(jobs)}
    except Exception as e:
        logger.error(f"获取调度器任务失败: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/{task_id}", response_model=ScheduledTaskResponse)
async def get_task(task_id: str, db: Session = Depends(get_db)):
    """获取单个定时任务"""
    task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _task_to_response(task)


@router.put("/{task_id}", response_model=ScheduledTaskResponse)
async def update_task(task_id: str, request: ScheduledTaskUpdate, db: Session = Depends(get_db)):
    """更新定时任务"""
    task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 验证时间格式
    if request.schedule_time:
        try:
            hour, minute = map(int, request.schedule_time.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("Invalid time")
        except ValueError:
            raise HTTPException(status_code=400, detail="时间格式错误，请使用 HH:MM 格式")

    # 验证爬取范围
    if request.scrape_range and request.scrape_range not in ["1d", "7d", "30d"]:
        raise HTTPException(status_code=400, detail="爬取范围只能是 1d、7d 或 30d")

    # 处理源ID列表（从 settings.json 验证）
    if request.source_ids is not None:
        from app.api.settings import settings_store
        for sid in request.source_ids:
            if sid not in settings_store.scrape_sources:
                raise HTTPException(status_code=400, detail=f"爬取源不存在: {sid}")
            # 同步爬取源到数据库（确保外键约束满足）
            _ensure_scrape_source_in_db(db, settings_store, sid)
        task.source_ids = json.dumps(request.source_ids) if request.source_ids else None
        task.source_id = request.source_ids[0] if request.source_ids else None

    # 更新字段
    if request.name is not None:
        task.name = request.name
    if request.custom_url is not None:
        task.custom_url = request.custom_url
    if request.schedule_time is not None:
        task.schedule_time = request.schedule_time
        task.next_run_at = _calculate_next_run(request.schedule_time)
    if request.scrape_range is not None:
        task.scrape_range = request.scrape_range
    if request.is_enabled is not None:
        task.is_enabled = request.is_enabled

    task.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(task)

    # 同步调度器
    _sync_scheduler()

    return _task_to_response(task)


@router.delete("/{task_id}")
async def delete_task(task_id: str, db: Session = Depends(get_db)):
    """删除定时任务"""
    task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    db.delete(task)
    db.commit()

    # 同步调度器
    _sync_scheduler()

    return {"message": "任务已删除"}


# ============ 任务操作路由 ============

@router.post("/{task_id}/toggle", response_model=ScheduledTaskResponse)
async def toggle_task(task_id: str, db: Session = Depends(get_db)):
    """切换任务启用状态"""
    task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    task.is_enabled = not task.is_enabled
    task.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(task)

    # 同步调度器
    _sync_scheduler()

    return _task_to_response(task)


@router.post("/{task_id}/run")
async def run_task_now(task_id: str, db: Session = Depends(get_db)):
    """手动触发立即执行任务（与定时任务完全独立）"""
    task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 获取要爬取的URL列表
    source_ids = task.get_source_ids_list()
    urls = []
    if task.custom_url:
        urls = [task.custom_url]
    else:
        for sid in source_ids:
            source = db.query(ScrapeSource).filter(ScrapeSource.id == sid).first()
            if source:
                urls.append(source.url)

    if not urls:
        raise HTTPException(status_code=400, detail="任务没有配置爬取URL")

    running_history = db.query(ScrapeHistory).filter(
        ScrapeHistory.task_id == task_id,
        ScrapeHistory.status == TaskStatus.RUNNING.value,
    ).first()
    if running_history:
        raise HTTPException(status_code=409, detail="该任务已有实例正在运行")

    # 创建历史记录
    history = ScrapeHistory(
        task_id=task_id,
        url=", ".join(urls[:3]) + ("..." if len(urls) > 3 else ""),
        status=TaskStatus.RUNNING.value,
    )
    db.add(history)
    task.last_run_at = datetime.utcnow()
    task.next_run_at = _calculate_next_run(task.schedule_time)
    db.commit()
    db.refresh(history)

    history_id = history.id
    task_name = task.name
    scrape_range = task.scrape_range
    worker_source_ids = list(source_ids)
    worker_urls = list(urls)

    # 独立执行函数（不通过 APScheduler）
    def run_immediately():
        """立即执行函数 - 独立于调度器"""
        logger.info(f"[立即执行] 任务 {task_name} 开始执行 (history_id={history_id})")

        from app.core.database import get_session_local
        worker_db = get_session_local()()
        loop = None
        start_time = datetime.utcnow()
        total_articles = 0
        scraped_articles = []
        errors = []
        final_status = TaskStatus.SUCCESS.value
        try:
            from app.services.scraper import get_scraper, ScrapeOptions

            # 获取爬取源的 category_id
            category_id = None
            source_id = worker_source_ids[0] if worker_source_ids else None
            if worker_source_ids:
                source = worker_db.query(ScrapeSource).filter(
                    ScrapeSource.id == worker_source_ids[0]
                ).first()
                if source:
                    category_id = source.category_id
            worker_db.rollback()

            scraper = get_scraper()
            options = ScrapeOptions.for_background_task(IMMEDIATE_PAGE_TIMEOUT_SECONDS)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            def progress(step, message, detail, current=None, total=None):
                position = f" ({current}/{total})" if current is not None and total else ""
                logger.info("[立即执行进度] %s%s: %s", message, position, detail)

            for url in worker_urls:
                logger.info(f"[立即执行] 深度爬取: {url}")
                try:
                    # 不再设置总超时，让爬取自然完成
                    list_page, article_results = loop.run_until_complete(
                        scraper.deep_scrape(
                            url=url,
                            options=options,
                            max_articles=IMMEDIATE_MAX_ARTICLES,
                            date_range=scrape_range,
                            scrape_level="deep",
                            progress_callback=progress,
                        )
                    )
                    logger.info(f"  [立即执行] 识别到 {len(article_results)} 篇文章")

                    for result in article_results:
                        if result.status == "metadata_only":
                            message = f"详情页不允许公开爬取，未保存到文档管理: {result.url}"
                            errors.append(message)
                            logger.warning(f"    [立即执行] {message}")
                            continue
                        if result.content and result.word_count > 50:
                            saved, article_id = scraper.save_to_database(
                                result, category_id=category_id, source_id=source_id
                            )
                            if saved:
                                total_articles += 1
                                title = result.title or "无标题"
                                scraped_articles.append(title)
                                logger.info(f"    [立即执行] 已保存: {title[:50]}")
                            else:
                                message = f"保存失败 {result.url}: {article_id}"
                                errors.append(message)
                                logger.error(f"    [立即执行] {message}")

                except Exception as url_error:
                    logger.error(f"  [立即执行] 爬取失败: {url_error}")
                    errors.append(f"{url}: {url_error}")

            if errors and not scraped_articles:
                final_status = TaskStatus.FAILED.value

        except Exception as e:
            logger.error(f"[立即执行] 任务执行失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            final_status = TaskStatus.FAILED.value
            errors.append(str(e))
        finally:
            if loop is not None:
                loop.close()
                asyncio.set_event_loop(None)

            try:
                worker_db.rollback()
                history_obj = worker_db.query(ScrapeHistory).filter(
                    ScrapeHistory.id == history_id
                ).first()
                if history_obj:
                    if scraped_articles:
                        article_list = [
                            f"{i+1}. {title[:40]}"
                            for i, title in enumerate(scraped_articles[:10])
                        ]
                        if len(scraped_articles) > 10:
                            article_list.append(f"... 还有 {len(scraped_articles) - 10} 篇")
                        history_obj.article_title = "\n".join(article_list)
                    else:
                        history_obj.article_title = "无文章"
                    history_obj.status = final_status
                    history_obj.error_message = "\n".join(errors)[:4000] if errors else None
                    history_obj.finished_at = datetime.utcnow()
                    history_obj.articles_count = total_articles
                    worker_db.commit()
            except Exception as status_error:
                worker_db.rollback()
                logger.error(f"[立即执行] 更新任务最终状态失败: {status_error}")
            finally:
                worker_db.close()

            logger.info(
                f"[立即执行] 任务 {task_name} 结束，状态={final_status}，"
                f"保存了 {total_articles} 篇文章"
            )

    # 在独立线程中执行
    try:
        _immediate_executor.submit(run_immediately)
        logger.info("[立即执行] 任务已提交到独立线程池")
    except Exception as e:
        logger.error(f"[立即执行] 启动任务失败: {e}")
        history.status = TaskStatus.FAILED.value
        history.error_message = f"启动任务失败: {e}"
        history.finished_at = datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=500, detail="启动任务失败") from e

    return {"message": "任务已开始执行", "history_id": history_id}
