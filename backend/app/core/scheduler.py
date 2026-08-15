"""
定时任务调度器

使用 APScheduler 实现定时爬取任务
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.database import get_db
from app.core.time_utils import beijing_now, local_to_utc_naive
from app.models.scheduled_task import ScheduledTask, ScrapeHistory, TaskStatus
from app.models.wechat import WechatCrawlTask

logger = logging.getLogger("scheduler")

# Scheduled scraping can be tuned without changing code or rebuilding the image.
SCHEDULED_URL_TIMEOUT_SECONDS = int(os.getenv("SCHEDULED_URL_TIMEOUT_SECONDS", "180"))
SCHEDULED_PAGE_TIMEOUT_SECONDS = int(os.getenv("SCHEDULED_PAGE_TIMEOUT_SECONDS", "60"))
SCHEDULED_URL_RETRIES = int(os.getenv("SCHEDULED_URL_RETRIES", "0"))
SCHEDULED_RETRY_BACKOFF_SECONDS = float(os.getenv("SCHEDULED_RETRY_BACKOFF_SECONDS", "5"))
SCHEDULED_TASK_MAX_RUNTIME_SECONDS = int(os.getenv("SCHEDULED_TASK_MAX_RUNTIME_SECONDS", "1200"))
SCHEDULED_MAX_ARTICLES = int(os.getenv("SCHEDULED_MAX_ARTICLES", "10"))
KNOWLEDGE_JOB_INTERVAL_SECONDS = int(os.getenv("KNOWLEDGE_JOB_INTERVAL_SECONDS", "60"))
KNOWLEDGE_JOB_BATCH_SIZE = int(os.getenv("KNOWLEDGE_JOB_BATCH_SIZE", "2"))
INSIGHT_ALERT_INTERVAL_MINUTES = int(os.getenv("INSIGHT_ALERT_INTERVAL_MINUTES", "30"))


async def _scrape_scheduled_url(
    scraper,
    url: str,
    options,
    max_articles: int,
    date_range: str | None,
    timeout_seconds: int,
):
    """Run one scheduled URL with bounded retries."""
    attempts = max(1, SCHEDULED_URL_RETRIES + 1)
    last_error = None

    def progress(step, message, detail, current=None, total=None):
        position = f" ({current}/{total})" if current is not None and total else ""
        logger.info("[定时爬取进度] %s%s: %s", message, position, detail)

    for attempt in range(attempts):
        logger.info(
            "[定时爬取] 开始来源: %s，页面上限=%s秒，尝试=%s/%s",
            url,
            getattr(options, "timeout", "unknown"),
            attempt + 1,
            attempts,
        )
        try:
            # 不再设置总超时，让爬取自然完成
            return await scraper.deep_scrape(
                url=url,
                options=options,
                max_articles=max_articles,
                date_range=date_range,
                scrape_level="deep",
                progress_callback=progress,
            )
        except Exception as exc:
            last_error = exc
            logger.error(
                "[定时爬取失败] 来源=%s，错误=%s",
                url,
                str(exc),
            )
            if attempt + 1 < attempts:
                logger.warning(
                    "Scheduled URL failed; retrying %s/%s: %s",
                    attempt + 1,
                    attempts,
                    url,
                )
                await asyncio.sleep(SCHEDULED_RETRY_BACKOFF_SECONDS)

    raise last_error or Exception(f"Scheduled URL failed: {url}")


def create_scheduler() -> BackgroundScheduler:
    """创建调度器"""
    scheduler = BackgroundScheduler(
        timezone="Asia/Shanghai",
        job_defaults={
            'max_instances': 1,  # 同一任务最多1个实例，防止重复执行
            'coalesce': False,   # 不合并错过的执行
            'misfire_grace_time': 60,  # 错过触发时间60秒内仍执行
        }
    )
    return scheduler


def cleanup_stuck_tasks(max_runtime_minutes: int = 60, reason: str | None = None):
    """清理运行时间过长的卡住任务"""
    from app.core.database import get_session_local
    SessionLocal = get_session_local()
    session = SessionLocal()
    try:
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(minutes=max_runtime_minutes)

        # 查找运行时间超过 max_runtime_minutes 的任务
        stuck_tasks = session.query(ScrapeHistory).filter(
            ScrapeHistory.status == TaskStatus.RUNNING.value,
            ScrapeHistory.started_at < cutoff
        ).all()

        if stuck_tasks:
            logger.warning(f"发现 {len(stuck_tasks)} 个卡住的任务，正在清理...")
            for task in stuck_tasks:
                task.status = TaskStatus.FAILED.value
                task.finished_at = datetime.utcnow()
                task.error_message = reason or f"任务超时（运行超过 {max_runtime_minutes} 分钟）自动终止"
            session.commit()
            logger.warning(f"已清理 {len(stuck_tasks)} 个卡住任务")

    except Exception as e:
        logger.error(f"清理卡住任务失败: {e}")
    finally:
        session.close()


def run_wechat_crawl_task(task_id: str):
    """执行微信公众号爬取任务"""
    logger.info(f"⏰ [微信爬取] 开始执行任务: {task_id}")

    # 创建新的数据库会话确保线程安全
    from app.core.database import get_session_local

    db = get_session_local()()
    try:
        task = db.query(WechatCrawlTask).filter(WechatCrawlTask.id == task_id).first()
        if not task:
            logger.error(f"任务不存在: {task_id}")
            return

        # 检查任务是否在最近10分钟内执行过（防止重复执行）
        if task.last_run_at:
            from datetime import timedelta
            if datetime.utcnow() - task.last_run_at < timedelta(minutes=10):
                logger.warning(f"微信任务 '{task_id}' 刚执行过，跳过本次执行")
                return

        start_time = datetime.utcnow()

        logger.info(f"⏰ [微信爬取] 开始爬取公众号...")

        # 执行爬取
        try:
            import asyncio
            from app.services.wechat.task_executor import WechatTaskExecutor

            # 创建新的事件循环用于线程中运行
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            executor = WechatTaskExecutor(db)
            result = loop.run_until_complete(executor.execute_task(task_id))
            loop.close()

            # 更新任务状态
            end_time = datetime.utcnow()
            duration = (end_time - start_time).total_seconds()

            task.last_run_at = datetime.utcnow()
            task.next_run_at = _calculate_next_run(task.schedule_time)
            db.commit()

            logger.info(f"✅ [微信爬取] 任务执行完成！耗时 {duration:.1f}秒")

        except Exception as e:
            logger.error(f"❌ [微信爬取] 任务执行失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            db.commit()

    except Exception as e:
        logger.error(f"执行微信任务 {task_id} 时出错: {e}")
    finally:
        db.close()


def run_scheduled_task(task_id: str):
    """执行单个定时任务"""
    logger.info(f"⏰ [定时执行] 开始执行任务: {task_id}")
    logger.info(f"⏰ [定时执行] 当前时间(北京时间): {beijing_now().isoformat()}")

    # 创建新的数据库会话确保线程安全
    from app.core.database import get_session_local

    db = get_session_local()()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task:
            logger.error(f"任务不存在: {task_id}")
            return

        logger.info(f"⏰ [定时执行] 任务名称: {task.name}")
        logger.info(f"⏰ [定时执行] 任务启用状态: {task.is_enabled}")
        logger.info(f"⏰ [定时执行] 任务执行时间: {task.schedule_time}")
        logger.info(f"⏰ [定时执行] 上次执行时间: {task.last_run_at}")
        logger.info(f"⏰ [定时执行] 下次执行时间: {task.next_run_at}")
        task_name = task.name
        task_scrape_range = task.scrape_range
        task_schedule_time = task.schedule_time

        # 检查是否有同任务正在运行（防止重复执行）
        running_count = db.query(ScrapeHistory).filter(
            ScrapeHistory.task_id == task_id,
            ScrapeHistory.status == TaskStatus.RUNNING.value
        ).count()

        if running_count > 0:
            logger.warning(f"任务 '{task.name}' 已有实例在运行，跳过本次执行")
            return

        start_time = datetime.utcnow()

        # 创建历史记录
        history = ScrapeHistory(
            task_id=task_id,
            url="调度执行",
            status=TaskStatus.RUNNING.value,
            started_at=start_time,
        )
        db.add(history)
        db.flush()
        # SQLite only permits one writer.  Persist the RUNNING marker now so
        # article saves performed through their own sessions are not blocked
        # for the entire crawl duration.
        db.commit()
        db.refresh(history)

        logger.info(f"⏰ [定时执行] 任务 '{task_name}' 开始爬取...")

        # 执行爬取
        try:
            # 获取要爬取的URL列表
            from sqlalchemy import text
            from app.services.scraper import get_scraper, ScrapeOptions
            from app.models.article import ScrapeSource
            import asyncio

            source_ids = task.get_source_ids_list()
            urls = []
            source_id = source_ids[0] if source_ids else None

            # 获取爬取源的 category_id
            category_id = None
            if source_ids:
                source = db.query(ScrapeSource).filter(ScrapeSource.id == source_ids[0]).first()
                if source:
                    category_id = source.category_id
                    source_id = source.id

            if task.custom_url:
                urls = [task.custom_url]
            else:
                for sid in source_ids:
                    result = db.execute(text(f"SELECT url FROM scrape_sources WHERE id = '{sid}'")).fetchone()
                    if result:
                        urls.append(result[0])

            # End the source lookup transaction before per-article sessions
            # begin writing.  This also avoids a stale WAL read snapshot being
            # upgraded to a writer at task completion.
            db.rollback()

            total_articles = 0
            scraper = get_scraper()

            # 使用与正常网页爬取 /api/scrape 相同的选项
            options = ScrapeOptions.for_background_task(SCHEDULED_PAGE_TIMEOUT_SECONDS)

            # 创建新的事件循环用于线程中运行
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # 保存每个爬取的文章信息
            scraped_articles = []
            errors = []
            completed_urls = 0

            for url in urls:
                logger.info(f"深度爬取 URL: {url}")
                try:
                    # 不再限制总超时，让爬取自然完成
                    list_page, article_results = loop.run_until_complete(
                        _scrape_scheduled_url(
                            scraper=scraper,
                            url=url,
                            options=options,
                            max_articles=SCHEDULED_MAX_ARTICLES,
                            date_range=task_scrape_range,
                            timeout_seconds=0,  # 0 表示不限时
                        )
                    )
                    completed_urls += 1
                    logger.info(f"  列表页: {list_page.title}, 识别到 {len(article_results)} 篇文章")

                    # 保存每篇文章到数据库
                    for result in article_results:
                        if result.status == "metadata_only":
                            message = f"详情页不允许公开爬取，未保存到文档管理: {result.url}"
                            errors.append(message)
                            logger.warning(f"    {message}")
                            continue
                        if result.content and result.word_count > 50:
                            saved, article_id = scraper.save_to_database(
                                result, category_id=category_id, source_id=source_id
                            )
                            if saved:
                                total_articles += 1
                                title = result.title or "无标题"
                                scraped_articles.append(title)
                                logger.info(f"      ✓ 已保存: {title[:50]}")
                            else:
                                message = f"保存失败 {result.url}: {article_id}"
                                errors.append(message)
                                logger.error(f"      ✗ {message}")
                        else:
                            logger.info(f"    内容不足，跳过: {result.url}")

                except Exception as url_error:
                    errors.append(f"{url}: {url_error}")
                    logger.error(f"  爬取失败: {url_error}")

            loop.close()

            # 更新历史记录 - 使用列表格式显示所有文章标题
            db.rollback()
            end_time = datetime.utcnow()
            duration = (end_time - start_time).total_seconds()

            if scraped_articles:
                # 格式化为列表
                article_list = []
                for i, title in enumerate(scraped_articles[:10], 1):  # 最多显示10个
                    article_list.append(f"{i}. {title[:40]}")
                if len(scraped_articles) > 10:
                    article_list.append(f"... 还有 {len(scraped_articles) - 10} 篇")

                history.article_title = "\n".join(article_list)
            else:
                history.article_title = "无文章"

            history.status = (
                TaskStatus.FAILED.value
                if errors and total_articles == 0
                else TaskStatus.SUCCESS.value
            )
            history.error_message = "\n".join(errors)[:4000] if errors else None
            history.started_at = start_time
            history.finished_at = end_time
            history.duration = duration
            history.articles_count = total_articles
            history.url = ", ".join(urls[:3]) + ("..." if len(urls) > 3 else "")
            task.last_run_at = datetime.utcnow()
            task.next_run_at = _calculate_next_run(task_schedule_time)
            db.commit()

            logger.info(f"✅ [定时执行] 任务 '{task_name}' 完成！保存了 {total_articles} 篇文章，耗时 {duration:.1f}秒")

        except Exception as e:
            logger.error(f"❌ [定时执行] 任务 '{task_name}' 执行失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            db.rollback()
            history.status = TaskStatus.FAILED.value
            history.error_message = str(e)
            history.started_at = start_time
            history.finished_at = datetime.utcnow()
            db.commit()

    except Exception as e:
        logger.error(f"执行任务 {task_id} 时出错: {e}")
    finally:
        db.close()


def _calculate_next_run(schedule_time: str) -> datetime:
    """计算下次执行时间"""
    now = beijing_now()
    hour, minute = map(int, schedule_time.split(":"))
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return local_to_utc_naive(next_run)


def sync_scheduler_tasks(scheduler: BackgroundScheduler):
    """同步调度器中的任务（从数据库加载）"""
    from app.core.database import get_session_local

    db = get_session_local()()
    try:
        # 获取所有启用的任务
        tasks = db.query(ScheduledTask).filter(ScheduledTask.is_enabled == True).all()

        # 获取所有启用的微信爬取任务
        wechat_tasks = db.query(WechatCrawlTask).filter(WechatCrawlTask.is_enabled == True).all()

        # 清空现有任务（保留调度器运行）
        scheduler.remove_all_jobs()

        # 添加普通定时任务
        for task in tasks:
            job_id = f"task_{task.id}"
            hour, minute = map(int, task.schedule_time.split(":"))

            scheduler.add_job(
                func=run_scheduled_task,
                trigger=CronTrigger(hour=hour, minute=minute, timezone="Asia/Shanghai"),
                id=job_id,
                args=[task.id],
                replace_existing=True,
                name=f"定时爬取: {task.name}",
            )
            logger.info(f"📅 [调度器] 添加任务: '{task.name}' → 每日 {task.schedule_time}")

        # 添加微信爬取任务
        for task in wechat_tasks:
            job_id = f"wechat_{task.id}"
            if task.schedule_time:
                hour, minute = map(int, task.schedule_time.split(":"))

                # 根据 schedule_type 设置触发器
                if task.schedule_type == "daily":
                    trigger = CronTrigger(hour=hour, minute=minute, timezone="Asia/Shanghai")
                elif task.schedule_type == "weekly":
                    trigger = CronTrigger(day_of_week="mon", hour=hour, minute=minute, timezone="Asia/Shanghai")
                elif task.schedule_type == "monthly":
                    trigger = CronTrigger(day=1, hour=hour, minute=minute, timezone="Asia/Shanghai")
                else:
                    trigger = CronTrigger(hour=hour, minute=minute, timezone="Asia/Shanghai")

                scheduler.add_job(
                    func=run_wechat_crawl_task,
                    trigger=trigger,
                    id=job_id,
                    args=[task.id],
                    replace_existing=True,
                    name=f"微信爬取: {task.account_id}",
                )
                logger.info(f"📅 [调度器] 添加微信任务: {task.account_id} → {task.schedule_type} {task.schedule_time}")

        logger.info(f"调度器同步完成，共 {len(tasks)} 个普通任务，{len(wechat_tasks)} 个微信任务")

    except Exception as e:
        logger.error(f"同步调度任务失败: {e}")
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    """启动调度器"""
    scheduler = create_scheduler()

    # 执行线程无法跨后端进程存活，启动时残留的 running 记录一定已经失去执行者。
    logger.info("启动时检查并清理未完成的历史任务...")
    cleanup_stuck_tasks(
        max_runtime_minutes=0,
        reason="后端服务重启，任务执行已中断",
    )

    # 初始同步
    sync_scheduler_tasks(scheduler)

    # 每10分钟检查并清理卡住的任务
    scheduler.add_job(
        func=cleanup_stuck_tasks,
        trigger=CronTrigger(minute="*/10", timezone="Asia/Shanghai"),
        id="cleanup_stuck_tasks",
        args=[60],  # 超时60分钟
        replace_existing=True,
        max_instances=1,
        name="清理卡住任务",
    )
    logger.info("卡住任务自动清理任务已注册（每10分钟检查，运行超过60分钟的任务将被清理）")

    # 每小时重新同步一次（处理新增/修改的任务）
    scheduler.add_job(
        func=sync_scheduler_tasks,
        trigger=CronTrigger(minute=0, timezone="Asia/Shanghai"),
        id="sync_tasks",
        args=[scheduler],
        replace_existing=True,
        name="同步定时任务",
    )

    # 启动时立即执行一次同步，确保任务状态正确
    logger.info("启动时立即同步任务状态...")
    _update_next_run_times()

    # 注册知识增强 Worker，定期处理 pending 任务
    register_knowledge_job_worker(scheduler)

    scheduler.start()
    logger.info("定时任务调度器已启动")

    return scheduler


def _update_next_run_times():
    """更新所有启用任务的下次执行时间"""
    from app.core.database import get_session_local
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        from datetime import datetime, timedelta
        tasks = db.query(ScheduledTask).filter(ScheduledTask.is_enabled == True).all()
        now = datetime.utcnow()
        local_now = beijing_now()
        updated_count = 0

        for task in tasks:
            # 如果下次执行时间已过或为空，重新计算
            if not task.next_run_at or task.next_run_at < now:
                hour, minute = map(int, task.schedule_time.split(":"))
                next_run = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if next_run <= local_now:
                    next_run += timedelta(days=1)
                task.next_run_at = local_to_utc_naive(next_run)
                updated_count += 1

        if updated_count > 0:
            db.commit()
            logger.info(f"已更新 {updated_count} 个任务的下次执行时间")

    except Exception as e:
        logger.error(f"更新下次执行时间失败: {e}")
    finally:
        db.close()


# ============ KG 对账定时任务(可选,默认关闭) ============

async def kg_reconcile_task():
    """定时对账任务:每 N 分钟跑一次,apply=False(只报告不修)"""
    from app.core.database import get_session_local
    from app.services.kg_sync import reconcile
    SessionLocal = get_session_local()
    session = SessionLocal()
    try:
        result = await reconcile(apply=False, db=session)
        logger.info(
            f"KG 对账报告: sqlite={result['sqlite_count']} "
            f"kg={result['kg_count']} "
            f"missing={len(result['missing_in_kg'])} "
            f"orphan={len(result['orphan_in_kg'])} "
            f"dirty={len(result['dirty_in_kg'])}"
        )
        if result['missing_in_kg'] or result['orphan_in_kg'] or result['dirty_in_kg']:
            logger.warning(f"KG 漂移检测: {result}")
    finally:
        session.close()


def register_kg_reconcile_job(scheduler, interval_minutes: int = 30):
    """注册定时对账任务(默认 30 分钟一次)

    当 interval_minutes <= 0 时不注册,等价于关闭。
    """
    if interval_minutes <= 0:
        logger.info("KG 定时对账未启用(interval_minutes <= 0)")
        return
    scheduler.add_job(
        kg_reconcile_task,
        "interval",
        minutes=interval_minutes,
        id="kg_reconcile",
        replace_existing=True,
        max_instances=1
    )
    logger.info(f"KG 定时对账任务已注册,间隔 {interval_minutes} 分钟")


def run_knowledge_job_worker():
    """在 APScheduler 线程中运行一批持久化知识增强任务。"""
    from app.services.knowledge_jobs import run_pending_knowledge_jobs
    run_pending_knowledge_jobs(limit=KNOWLEDGE_JOB_BATCH_SIZE)


def register_knowledge_job_worker(scheduler):
    """注册知识增强 Worker，间隔由 KNOWLEDGE_JOB_INTERVAL_SECONDS 控制。"""
    from app.services.knowledge_jobs import recover_interrupted_knowledge_jobs
    recovered = recover_interrupted_knowledge_jobs()
    if recovered:
        logger.warning("恢复 %s 个被中断的知识增强任务", recovered)
    scheduler.add_job(
        run_knowledge_job_worker,
        "interval",
        seconds=max(10, KNOWLEDGE_JOB_INTERVAL_SECONDS),
        id="knowledge_job_worker",
        replace_existing=True,
        max_instances=1,
    )
    logger.info("知识增强 Worker 已注册，间隔 %s 秒，每批 %s 个任务", KNOWLEDGE_JOB_INTERVAL_SECONDS, KNOWLEDGE_JOB_BATCH_SIZE)


def run_insight_alert_monitor():
    """Periodically persist new multi-source signals for human review."""
    from app.services.insight_alerts import scan_insight_alerts
    result = scan_insight_alerts()
    logger.info("洞察预警扫描完成: %s", result)


def register_insight_alert_monitor(scheduler):
    scheduler.add_job(
        run_insight_alert_monitor,
        "interval",
        minutes=max(5, INSIGHT_ALERT_INTERVAL_MINUTES),
        id="insight_alert_monitor",
        replace_existing=True,
        max_instances=1,
    )
    logger.info("洞察预警扫描器已注册，间隔 %s 分钟", INSIGHT_ALERT_INTERVAL_MINUTES)


# 全局调度器实例
_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler | None:
    """获取调度器实例"""
    return _scheduler


def init_scheduler() -> BackgroundScheduler:
    """初始化调度器（供应用启动时调用）"""
    global _scheduler
    if _scheduler is None:
        _scheduler = start_scheduler()
    return _scheduler


def shutdown_scheduler():
    """关闭调度器"""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown()
        _scheduler = None
        logger.info("定时任务调度器已关闭")
