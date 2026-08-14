"""
知识图谱同步服务

把"文档管理 → 知识图谱"的所有动作集中编排,确保:
- Article 节点 metadata 实时同步
- 实体抽取异步,失败不阻塞 CRUD
- 删除文章级联删 KG
- 提供对账工具
"""
import asyncio
import logging
import os
import threading
from datetime import datetime

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from app.core.database import get_session_local
from app.models.article import Article
from app.services.kg import Neo4jService, EntityExtractor

logger = logging.getLogger("ai-studio")

# 同步状态(供前端轮询)
_sync_state = {
    "in_progress": False,         # 是否有后台抽取任务正在跑
    "active_count": 0,            # 当前并发任务数
    "total_processed": 0,         # 本次启动以来已处理数
    "total_failed": 0,            # 本次启动以来失败数
    "batch_total": 0,             # 最近一批任务总数
    "batch_processed": 0,         # 最近一批已完成数
    "batch_failed": 0,            # 最近一批失败数
    "batch_started_at": None,
    "started_at": None,           # 本次启动时间
    "last_finished_at": None,     # 上次完成时间
}
_sync_state_lock = threading.Lock()
_kg_rebuild_lock = asyncio.Lock()


def _set_in_progress(delta: int, success: bool = True) -> None:
    """增减活跃任务计数;供前端 /api/kg/sync-status 读取"""
    with _sync_state_lock:
        _sync_state["active_count"] = max(0, _sync_state["active_count"] + delta)
        if _sync_state["active_count"] > 0:
            _sync_state["in_progress"] = True
        else:
            _sync_state["in_progress"] = False
        if delta < 0:
            if success:
                _sync_state["total_processed"] += 1
                _sync_state["batch_processed"] += 1
            else:
                _sync_state["total_failed"] += 1
                _sync_state["batch_failed"] += 1
            _sync_state["last_finished_at"] = datetime.utcnow().isoformat()


def get_sync_state() -> dict:
    """供前端轮询:取同步进度快照"""
    with _sync_state_lock:
        return dict(_sync_state)


def recover_interrupted_articles(db: Session) -> int:
    """后端重启后恢复失去执行协程的 processing 文章。"""
    count = db.query(Article).filter(Article.kg_status == "processing").update(
        {
            Article.kg_status: "pending",
            Article.kg_error_message: "后端重启，知识抽取任务已重新排队",
        },
        synchronize_session=False,
    )
    db.commit()
    return count


def build_neo4j() -> Neo4jService:
    """构建 Neo4jService(可被 patch 注入)"""
    return Neo4jService()


async def on_article_sync_only(article: Article) -> bool:
    """仅同步 Article metadata 到 Neo4j，不触发后台抽实体。

    用于 WechatPipeline 等非 FastAPI 请求上下文中，直接返回同步结果。
    """
    neo4j = build_neo4j()
    try:
        ok = await neo4j.upsert_article_metadata(
            article_id=article.id,
            title=article.title or "",
            url=article.url or "",
            summary=article.summary,
            content_hash=article.content_hash,
            kg_status=article.kg_status or "pending"
        )
        if not ok:
            logger.warning(f"新建文章 {article.id} 时 Neo4j metadata 同步失败,后续 reconcile 兜底")
        return ok
    finally:
        await neo4j.close()


def trigger_async_extraction(article_id: str) -> None:
    """在后台线程中安全地触发异步实体抽取。

    用于非异步上下文（如同步函数或 BackgroundTasks 不可用的场景），
    通过 asyncio.run() 在新事件循环中执行抽取任务。
    """
    def _run():
        try:
            asyncio.run(extract_and_link_entities(article_id))
        except Exception as e:
            logger.error(f"异步抽取任务失败 {article_id}: {e}")

    import threading
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


async def on_article_created(article: Article, background_tasks: BackgroundTasks) -> None:
    """文档管理新建文章后调用:同步 metadata + 排后台抽实体"""
    ok = await on_article_sync_only(article)
    if not ok:
        logger.warning(f"新建文章 {article.id} 时 Neo4j metadata 同步失败,后续 reconcile 兜底")
    # 后台抽实体(失败由 kg_status='failed' 标记)
    background_tasks.add_task(extract_and_link_entities, article.id)


async def on_article_updated(article: Article, background_tasks: BackgroundTasks) -> None:
    """
    文档管理更新文章后调用:
    - 始终同步 metadata
    - 若 content_hash 变了(意味着内容改了),把 kg_status 改回 'pending',但不自动重抽
    """
    new_status = article.kg_status or "pending"
    if (article.kg_content_hash
            and article.content_hash
            and article.kg_content_hash != article.content_hash):
        new_status = "pending"

    neo4j = build_neo4j()
    ok = await neo4j.upsert_article_metadata(
        article_id=article.id,
        title=article.title or "",
        url=article.url or "",
        summary=article.summary,
        content_hash=article.content_hash,
        kg_status=new_status
    )
    if not ok:
        logger.warning(f"更新文章 {article.id} 时 Neo4j metadata 同步失败")


async def on_article_deleted(article_id: str) -> None:
    """文档管理删除文章后调用:彻底删 KG Article 节点 + 边 + 孤儿实体"""
    neo4j = build_neo4j()
    ok = await neo4j.delete_article_full(article_id)
    if not ok:
        logger.error(f"删除文章 {article_id} 的 KG 数据失败,reconcile 会兜底")


async def extract_and_link_entities(article_id: str) -> None:
    """
    抽取文章实体并写入 Neo4j
    - 设置 kg_status='processing'
    - 调用 EntityExtractor
    - 成功 → kg_status='success', kg_content_hash=current hash
    - 失败 → kg_status='failed', kg_error_message=错误
    """
    _set_in_progress(1)
    success = False
    try:
        success = await _extract_and_link_entities_inner(article_id)
    finally:
        _set_in_progress(-1, success=success)


async def _extract_and_link_entities_inner(article_id: str) -> bool:
    """实际抽实体逻辑(被 extract_and_link_entities 包一层做状态计数)"""
    session = get_session_local()()
    try:
        article = session.query(Article).filter(Article.id == article_id).first()
        if not article:
            logger.warning(f"extract_and_link_entities: 文章 {article_id} 不存在")
            return False

        article.kg_status = "processing"
        session.commit()

        content = article.content or article.summary or ""
        if not content:
            article.kg_status = "skipped"
            article.kg_error_message = "内容为空,跳过抽取"
            session.commit()
            return False

        extractor = EntityExtractor()
        extraction_error = None
        # 增加超时到 600 秒，避免因 LLM 响应慢导致 partial 状态
        EXTRACTION_TIMEOUT = int(os.environ.get("KG_EXTRACTION_TIMEOUT_SECONDS", "600"))
        try:
            result = await asyncio.wait_for(
                extractor.extract(content, article_id=str(article.id)),
                timeout=EXTRACTION_TIMEOUT,
            )
        except asyncio.TimeoutError:
            result = None
            extraction_error = f"知识抽取超时（{EXTRACTION_TIMEOUT} 秒），可能是内容过长或 LLM 响应慢"
            logger.error(f"文章 {article_id} 实体抽取超时")
        if result and result.error:
            extraction_error = result.error
            logger.error(f"文章 {article_id} 实体抽取失败: {result.error}")

        is_partial = extraction_error is not None
        if is_partial:
            # Keywords are metadata, not verified graph entities. On extraction
            # failure keep the existing graph intact and expose a retryable state.
            entities = []
            relations = []
        else:
            entities = extractor.deduplicate_entities(result.entities)
            relations = result.relations

        # 仅在新结果解析成功后替换旧知识，避免失败重抽破坏可用图谱。
        async with _kg_rebuild_lock:
            neo4j = build_neo4j()
            try:
                await neo4j.upsert_article_metadata(
                    article_id=article.id,
                    title=article.title or "",
                    url=article.url or "",
                    summary=article.summary,
                    content_hash=article.content_hash,
                    kg_status="partial" if is_partial else "success"
                )

                if not is_partial:
                    if not await neo4j.clear_article_knowledge(article.id):
                        raise RuntimeError("清理文章旧知识失败")
                    # 图替换必须串行，避免并发清理把共享实体误判为孤立实体。
                    await neo4j.batch_create_entities_and_relations(
                        article_id=article.id,
                        entities=entities,
                        relations=relations
                    )
            finally:
                await neo4j.close()

        article.kg_status = "partial" if is_partial else "success"
        article.kg_processed_at = datetime.utcnow()
        article.kg_content_hash = article.content_hash
        article.kg_error_message = (
            f"模型抽取未完成，已保留原有图谱并等待重试：{extraction_error}"[:500]
            if is_partial else None
        )
        session.commit()
        logger.info(
            "文章 %s 实体抽取%s,entities=%s",
            article_id,
            "部分完成" if is_partial else "成功",
            len(entities),
        )

        # 只创建持久化增强任务，由统一 Worker 执行，避免重复触发和重启丢任务。
        from app.services.knowledge_jobs import enqueue_article_enhancement
        enqueue_article_enhancement(article_id, content)

        return True

    except Exception as e:
        logger.exception(f"extract_and_link_entities 异常 {article_id}: {e}")
        try:
            article = session.query(Article).filter(Article.id == article_id).first()
            if article:
                article.kg_status = "failed"
                article.kg_error_message = str(e)[:500]
                session.commit()
        except Exception:
            session.rollback()
        return False
    finally:
        session.close()


async def process_pending_articles(
    db: Session,
    max_concurrency: int = 3,
    rate_limit_seconds: float = 0.5,
    limit: int = 200,
    include_failed: bool = False,
    include_success: bool = False,
    include_partial: bool = False,
) -> dict:
    """
    启动时把 kg_status in (NULL, 'pending') 的文章批量抽实体。
    限流: 同时最多 max_concurrency 个抽取,每篇间隔 rate_limit_seconds 秒。
    防止 LLM 被瞬间打爆。
    """
    from app.models.article import Article

    retry_statuses = ["pending", "skipped"]
    if include_failed:
        retry_statuses.append("failed")
    if include_success:
        retry_statuses.append("success")
    if include_partial:
        retry_statuses.append("partial")
    pending = db.query(Article).filter(
        Article.status == "success",
        (Article.kg_status.is_(None)) | (Article.kg_status.in_(retry_statuses))
    ).limit(limit).all()

    if not pending:
        return {"scanned": 0, "scheduled": 0}

    # 创建协程前先认领任务，避免连续点击或启动钩子并发扫描到同一篇文章。
    pending_ids = [article.id for article in pending]
    db.query(Article).filter(Article.id.in_(pending_ids)).update(
        {
            Article.kg_status: "processing",
            Article.kg_error_message: None,
        },
        synchronize_session=False,
    )
    db.commit()

    sem = asyncio.Semaphore(max_concurrency)

    async def _run_one(article_id: str):
        async with sem:
            try:
                await extract_and_link_entities(article_id)
            except Exception as e:
                logger.error(f"process_pending_articles 抽 {article_id} 失败: {e}")
            await asyncio.sleep(rate_limit_seconds)

    tasks = [asyncio.create_task(_run_one(article_id)) for article_id in pending_ids]

    # 标记本次启动时间,供前端显示
    with _sync_state_lock:
        _sync_state["started_at"] = datetime.utcnow().isoformat()
        _sync_state["batch_started_at"] = _sync_state["started_at"]
        _sync_state["batch_total"] = len(pending_ids)
        _sync_state["batch_processed"] = 0
        _sync_state["batch_failed"] = 0

    logger.info(f"process_pending_articles: 扫描 {len(pending)} 篇,启动 {len(tasks)} 个抽取任务")

    # 不 await tasks,让它们在后台跑
    return {"scanned": len(pending), "scheduled": len(tasks)}


async def reconcile(apply: bool, db: Session) -> dict:
    """
    对账:对比 SQLite 与 Neo4j,发现漂移
    apply=False: 仅返回统计,不修改
    apply=True:  修复(missing → 异步抽; orphan → 删节点; dirty → 标 pending)
    """
    # 1. 取 SQLite 所有 success 文章
    sqlite_articles = db.query(Article).filter(Article.status == "success").all()
    sqlite_ids = {a.id for a in sqlite_articles}

    neo4j = build_neo4j()

    # 2. 找 Neo4j 中所有 Article.id
    kg_ids = await _get_kg_article_ids(neo4j)

    # 3. 计算三类漂移
    missing_in_kg = list(sqlite_ids - kg_ids)  # SQLite 有,KG 无
    orphan_in_kg = list(kg_ids - sqlite_ids)   # KG 有,SQLite 无

    # 4. 找脏数据
    kg_pairs = []
    for art in sqlite_articles:
        if art.id in missing_in_kg:
            continue
        kg_hash = await _get_kg_content_hash(neo4j, art.id)
        kg_pairs.append((art.id, art.content_hash or "", kg_hash or ""))
    dirty_in_kg = await neo4j.find_dirty_articles(kg_pairs)

    result = {
        "sqlite_count": len(sqlite_articles),
        "kg_count": len(kg_ids - set(orphan_in_kg)),
        "missing_in_kg": missing_in_kg,
        "orphan_in_kg": orphan_in_kg,
        "dirty_in_kg": dirty_in_kg,
        "failed_in_db": [
            art.id for art in sqlite_articles if art.kg_status == "failed"
        ],
    }

    if apply:
        fixed = {
            "missing_synced": 0,
            "orphans_deleted": 0,
            "dirty_marked": 0,
            "orphan_entities_deleted": 0,
            "article_entity_sources_backfilled": 0,
            "failed_retried": 0,
        }
        # 先补齐已有 Article-实体边的来源元数据；只追加来源，不重建实体/关系。
        fixed["article_entity_sources_backfilled"] = (
            await neo4j.backfill_article_entity_sources()
        )
        # 1) 删孤儿
        for aid in orphan_in_kg:
            ok = await neo4j.delete_article_full(aid)
            if ok:
                fixed["orphans_deleted"] += 1
        fixed["orphan_entities_deleted"] = await neo4j.cleanup_orphan_entities()
        # 2) 标脏数据为 pending(等用户重抽)
        for aid in dirty_in_kg:
            art = db.query(Article).filter(Article.id == aid).first()
            if art:
                art.kg_status = "pending"
                db.commit()
                fixed["dirty_marked"] += 1
        # 3) 异步补抽 missing
        for aid in missing_in_kg:
            art = db.query(Article).filter(Article.id == aid).first()
            if art:
                art.kg_status = "pending"
                db.commit()
                asyncio.create_task(extract_and_link_entities(aid))
                fixed["missing_synced"] += 1
        # 4) 失败文章不一定缺少 Article 节点，也要显式重试。
        for aid in result["failed_in_db"]:
            if aid in missing_in_kg:
                continue
            art = db.query(Article).filter(Article.id == aid).first()
            if art:
                art.kg_status = "pending"
                art.kg_error_message = None
                db.commit()
                asyncio.create_task(extract_and_link_entities(aid))
                fixed["failed_retried"] += 1
        result["fixed"] = fixed

    return result


async def _get_kg_article_ids(neo4j: Neo4jService) -> set:
    """取 Neo4j 中所有 Article.id"""
    await neo4j.connect()
    async with neo4j._driver.session() as session:
        result = await session.run("MATCH (a:Article) RETURN a.id AS id")
        records = await result.data()
    return {r["id"] for r in records}


async def _get_kg_content_hash(neo4j: Neo4jService, article_id: str) -> str:
    """取 Neo4j 中某 Article 的 content_hash"""
    await neo4j.connect()
    async with neo4j._driver.session() as session:
        r = await session.run(
            "MATCH (a:Article {id: $id}) RETURN a.content_hash AS h",
            id=article_id
        )
        rec = await r.single()
        return rec["h"] if rec else ""


async def trigger_self_enhancement(article_id: str, content: str):
    """
    触发知识自增强循环

    在文章实体抽取成功后调用，提取知识点、关联和总结。
    """
    from app.services.knowledge_jobs import enqueue_article_enhancement
    enqueue_article_enhancement(article_id, content)
