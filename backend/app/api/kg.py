"""
知识图谱 API

提供知识图谱的构建、查询和管理功能
"""
import logging
import asyncio
import hashlib
import uuid
from typing import Optional, List, Dict, Any, Literal
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.core.database import get_db
from app.models.article import Article
from app.models.knowledge import KnowledgeJob
from app.models.synthesis import KnowledgeSynthesis
from app.models.prediction import PredictionRecord
from app.models.insight_alert import InsightAlert
from app.services.kg import Neo4jService, EntityExtractor, EmbeddingService
from app.services.kg.graph import EntityNode, Relationship
from app.services.kg.embedding import VectorStore
from app.services.kg.qa import answer_question
from app.services.kg.mining import find_relation_evidence
from app.services import kg_sync
from app.services.knowledge_synthesis import KnowledgeSynthesisService
from app.core.llm import llm_service

logger = logging.getLogger("ai-studio")

router = APIRouter(prefix="/api/kg", tags=["知识图谱"])

# 批量处理进度存储
_batch_progress = {}


def _enhancement_job_id(article: Article) -> Optional[str]:
    """Return the enhancement job that corresponds to the article's current body.

    KG entity extraction and knowledge enhancement are separate pipelines.  The
    article's ``kg_status`` therefore cannot tell us whether knowledge points
    and cross-document candidates have actually been produced.
    """
    content = article.content or ""
    if not content:
        return None
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"enh_{article.id}_{content_hash[:16]}"


def _enhancement_statuses(db: Session, articles: List[Article]) -> Dict[str, str]:
    """Load current-version enhancement status for a group of articles in one query."""
    expected = {
        job_id: str(article.id)
        for article in articles
        if (job_id := _enhancement_job_id(article))
    }
    if not expected:
        return {}
    rows = db.query(KnowledgeJob.id, KnowledgeJob.status).filter(
        KnowledgeJob.id.in_(expected)
    ).all()
    return {expected[job_id]: job_status for job_id, job_status in rows}


# ============ 依赖项 ============

def get_neo4j_service() -> Neo4jService:
    """获取 Neo4j 服务实例"""
    return Neo4jService()


def get_embedding_service() -> EmbeddingService:
    """获取 Embedding 服务实例"""
    return EmbeddingService()


def get_causal_article_records(db: Session) -> List[Dict[str, Any]]:
    """Load the bounded historical corpus used for review-only causal mining."""
    articles = (
        db.query(Article)
        .filter(Article.status == "success")
        .order_by(Article.scraped_at.desc())
        .limit(500)
        .all()
    )
    return [
        {
            "id": str(article.id),
            "content": article.content or "",
            "summary": article.summary or "",
        }
        for article in articles
    ]


# ============ 健康检查 ============

@router.get("/health")
async def check_kg_health():
    """检查知识图谱服务状态"""
    try:
        neo4j = Neo4jService()
        is_connected = await neo4j.verify_connection()

        return {
            "status": "healthy" if is_connected else "unhealthy",
            "neo4j": {
                "connected": is_connected,
                "uri": neo4j.uri
            }
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e)
        }


@router.post("/init")
async def init_knowledge_graph():
    """初始化知识图谱（设置约束和索引）"""
    try:
        neo4j = Neo4jService()
        await neo4j.connect()
        await neo4j.init_schema()
        await neo4j.close()

        return {"status": "success", "message": "知识图谱初始化完成"}
    except Exception as e:
        logger.error(f"初始化知识图谱失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============ 图谱统计 ============

@router.get("/stats")
async def get_graph_stats():
    """获取图谱统计信息(含与 SQLite 的对账)"""
    try:
        neo4j = Neo4jService()
        await neo4j.connect()
        stats = await neo4j.get_graph_stats()
        await neo4j.close()

        # 加上 SQLite 计数与漂移检测(以文档管理为最终口径)
        from app.core.database import get_session_local
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            db_count = session.query(Article).filter(Article.status == "success").count()
        finally:
            session.close()

        stats["articles_in_db"] = db_count
        kg_articles = stats.get("articles", 0)
        stats["orphan_entities"] = stats.get("orphan_entities", 0)
        stats["drift_detected"] = (
            kg_articles != db_count
            or stats.get("articles_without_entities", 0) > 0
            or stats["orphan_entities"] > 0
        )

        return {
            "status": "success",
            "stats": stats
        }
    except Exception as e:
        logger.error(f"获取图谱统计失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============ 单篇文章处理 ============

@router.post("/process/{article_id}")
async def process_article(
    article_id: str,
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None
):
    """
    处理单篇文章，提取实体并构建图谱

    Args:
        article_id: 文章 ID
    """
    # 获取文章
    article = db.query(Article).filter(Article.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404, detail="文章不存在")

    try:
        # 初始化服务
        neo4j = Neo4jService()
        extractor = EntityExtractor()

        await neo4j.connect()

        # 1. 创建文章节点
        await neo4j.create_article_node(
            article_id=article.id,
            title=article.title or "",
            url=article.url or "",
            summary=article.summary
        )

        # 2. 提取实体和关系
        content = article.content or article.summary or ""
        if not content:
            await neo4j.close()
            return {"status": "skipped", "message": "文章内容为空"}

        result = await extractor.extract(content)

        if result.error:
            await neo4j.close()
            raise HTTPException(status_code=500, detail=f"实体抽取失败: {result.error}")

        # 3. 去重实体
        entities = extractor.deduplicate_entities(result.entities)
        relations = result.relations

        # 4. 批量创建实体和关系
        stats = await neo4j.batch_create_entities_and_relations(
            article_id=article.id,
            entities=entities,
            relations=relations
        )

        await neo4j.close()

        return {
            "status": "success",
            "article_id": article_id,
            "entities_count": len(entities),
            "relations_count": len(relations),
            "created": stats
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"处理文章失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============ 批量处理 ============

@router.post("/batch-process")
async def batch_process_articles(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db)
):
    """
    批量处理文章

    Args:
        limit: 最大处理数量
    """
    try:
        # 获取待处理文章（按时间排序）
        articles = db.query(Article).filter(
            Article.status == "success"
        ).order_by(Article.scraped_at.desc()).limit(limit).all()

        if not articles:
            return {"status": "success", "message": "没有待处理文章", "processed": 0}

        neo4j = Neo4jService()
        extractor = EntityExtractor()

        await neo4j.connect()
        await neo4j.init_schema()

        results = {
            "total": len(articles),
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "errors": []
        }

        for article in articles:
            try:
                # 检查是否已处理
                existing = await neo4j.get_article_entities(article.id)
                if existing:
                    results["skipped"] += 1
                    continue

                # 创建文章节点
                await neo4j.create_article_node(
                    article_id=article.id,
                    title=article.title or "",
                    url=article.url or "",
                    summary=article.summary
                )

                # 提取实体
                content = article.content or article.summary or ""
                if not content:
                    results["skipped"] += 1
                    continue

                result = await extractor.extract(content)

                if result.error:
                    results["failed"] += 1
                    results["errors"].append({
                        "article_id": article.id,
                        "error": result.error
                    })
                    continue

                # 去重并保存
                entities = extractor.deduplicate_entities(result.entities)
                relations = result.relations

                await neo4j.batch_create_entities_and_relations(
                    article_id=article.id,
                    entities=entities,
                    relations=relations
                )

                results["success"] += 1

                # 添加小延迟避免 API 限流
                await asyncio.sleep(0.5)

            except Exception as e:
                results["failed"] += 1
                results["errors"].append({
                    "article_id": article.id,
                    "error": str(e)
                })
                logger.error(f"处理文章 {article.id} 失败: {e}")

        await neo4j.close()

        return {
            "status": "success",
            "results": results
        }

    except Exception as e:
        logger.error(f"批量处理失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============ 查询功能 ============

@router.get("/search")
async def search_entities(
    query: str = Query(default="", description="搜索关键词"),
    entity_type: Optional[str] = Query(default=None, description="实体类型"),
    limit: int = Query(default=50, ge=1, le=200)
):
    """
    搜索实体

    Args:
        query: 搜索关键词
        entity_type: 实体类型过滤
        limit: 返回数量限制
    """
    try:
        neo4j = Neo4jService()
        await neo4j.connect()

        entities = await neo4j.search_entities(
            query=query if query else None,
            entity_type=entity_type,
            limit=limit
        )

        await neo4j.close()

        return {
            "status": "success",
            "count": len(entities),
            "entities": entities
        }

    except Exception as e:
        logger.error(f"搜索实体失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/article/{article_id}")
async def get_article_kg(article_id: str):
    """获取文章的图谱数据"""
    try:
        neo4j = Neo4jService()
        await neo4j.connect()

        # 获取文章信息
        article_neo4j = await neo4j.get_article_entities(article_id)

        await neo4j.close()

        return {
            "status": "success",
            "article_id": article_id,
            "entities": article_neo4j
        }

    except Exception as e:
        logger.error(f"获取文章图谱失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entity/{entity_name}")
async def get_entity_details(
    entity_name: str,
    depth: int = Query(default=1, ge=1, le=3)
):
    """
    获取实体详情和邻居

    Args:
        entity_name: 实体名称
        depth: 关系深度
    """
    try:
        neo4j = Neo4jService()
        await neo4j.connect()

        neighbors = await neo4j.get_entity_neighbors(entity_name, depth)

        await neo4j.close()

        return {
            "status": "success",
            "entity_name": entity_name,
            "data": neighbors
        }

    except Exception as e:
        logger.error(f"获取实体详情失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/graph")
async def get_graph_data(
    limit: int = Query(default=1000, ge=100, le=5000)
):
    """获取图谱可视化数据"""
    try:
        neo4j = Neo4jService()
        await neo4j.connect()

        graph_data = await neo4j.export_graph_data(limit=limit)

        await neo4j.close()

        return {
            "status": "success",
            "node_count": len(graph_data.get("nodes", [])),
            "edge_count": len(graph_data.get("edges", [])),
            "data": graph_data
        }

    except Exception as e:
        logger.error(f"获取图谱数据失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============ 向量检索 ============

@router.post("/vector-search")
async def vector_search(
    query: str = Query(..., description="搜索查询"),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db)
):
    """
    基于向量的语义搜索

    Args:
        query: 搜索查询
        limit: 返回数量
    """
    try:
        # 生成查询向量
        embedding = EmbeddingService()
        query_vector = await embedding.encode_single(query)

        if not query_vector:
            raise HTTPException(status_code=500, detail="向量生成失败")

        # 搜索相似文章
        vector_store = VectorStore(db)
        results = vector_store.search_by_vector(
            query_vector=query_vector,
            limit=limit,
            threshold=0.3
        )

        return {
            "status": "success",
            "query": query,
            "count": len(results),
            "results": results
        }

    except Exception as e:
        logger.error(f"向量搜索失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/generate-vectors")
async def generate_vectors(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None
):
    """
    为文章生成向量

    Args:
        limit: 最大处理数量
    """
    try:
        # 获取还没有向量的文章
        query = text("""
            SELECT a.id FROM articles a
            LEFT JOIN article_embeddings e ON a.id = e.article_id
            WHERE e.article_id IS NULL AND a.status = 'completed'
            ORDER BY a.scraped_at DESC
            LIMIT :limit
        """)
        from sqlalchemy import text
        result = db.execute(query, {"limit": limit})
        article_ids = [row[0] for row in result.fetchall()]

        if not article_ids:
            return {"status": "success", "message": "没有需要生成向量的文章", "count": 0}

        # 初始化向量服务
        embedding = EmbeddingService()
        vector_store = VectorStore(db)

        # 确保表存在
        vector_store.init_table()

        success = 0
        failed = 0
        errors = []

        for article_id in article_ids:
            try:
                article = db.query(Article).filter(Article.id == article_id).first()
                if not article:
                    continue

                # 生成向量
                title = article.title or ""
                content = article.content or article.summary or ""
                summary = article.summary or ""

                title_vec = await embedding.encode_single(title) if title else []
                content_vec = await embedding.encode_single(content[:5000]) if content else []
                summary_vec = await embedding.encode_single(summary) if summary else []

                if title_vec and content_vec:
                    vector_store.save_embeddings(
                        article_id=article_id,
                        title_vector=title_vec,
                        content_vector=content_vec,
                        summary_vector=summary_vec if summary_vec else None
                    )
                    success += 1
                else:
                    failed += 1

                # 添加小延迟
                await asyncio.sleep(0.2)

            except Exception as e:
                failed += 1
                errors.append({"article_id": article_id, "error": str(e)})
                logger.error(f"生成向量失败 {article_id}: {e}")

        return {
            "status": "success",
            "total": len(article_ids),
            "success": success,
            "failed": failed,
            "errors": errors[:10]  # 只返回前10个错误
        }

    except Exception as e:
        logger.error(f"批量生成向量失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============ 删除功能 ============

@router.delete("/article/{article_id}")
async def delete_article_kg(article_id: str):
    """删除文章的图谱数据(保留 Article 节点,清实体关联)"""
    try:
        neo4j = Neo4jService()
        await neo4j.connect()

        await neo4j.delete_article_kg(article_id)

        await neo4j.close()

        return {
            "status": "success",
            "message": f"已删除文章 {article_id} 的图谱数据"
        }

    except Exception as e:
        logger.error(f"删除文章的图谱数据失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============ KG ↔ 文档管理 一致性同步(新增) ============

@router.get("/sync-status")
async def get_sync_status(db: Session = Depends(get_db)):
    """
    同步状态总览(供前端轮询,建议每 5~10s 拉一次)
    - by_status: 各 kg_status 的文章计数
    - sync_state: 后台任务进度(in_progress、active_count、本次已处理 / 失败)
    - drift: 数量对比(SQLite vs Neo4j)
    """
    try:
        from sqlalchemy import func
        from app.services.kg_sync import get_sync_state

        # 1. 按 kg_status 统计(SQLite)
        rows = db.query(Article.kg_status, func.count(Article.id)) \
            .filter(Article.status == "success") \
            .group_by(Article.kg_status).all()
        by_status = {
            (r[0] or "pending"): r[1] for r in rows
        }
        # 缺位补 0,统一六个 key
        normalized = {
            "pending": by_status.get("pending", 0) + by_status.get(None, 0),
            "processing": by_status.get("processing", 0),
            "success": by_status.get("success", 0),
            "failed": by_status.get("failed", 0),
            "partial": by_status.get("partial", 0),
            "skipped": by_status.get("skipped", 0),
        }
        total_in_db = sum(normalized.values())
        failed_articles = [
            {
                "id": str(article.id),
                "title": article.title,
                "error": article.kg_error_message,
            }
            for article in db.query(Article)
            .filter(Article.status == "success", Article.kg_status == "failed")
            .order_by(Article.updated_at.desc())
            .limit(20)
            .all()
        ]
        partial_articles = [
            {
                "id": str(article.id),
                "title": article.title,
                "warning": article.kg_error_message,
            }
            for article in db.query(Article)
            .filter(Article.status == "success", Article.kg_status == "partial")
            .order_by(Article.updated_at.desc())
            .limit(20)
            .all()
        ]

        # 2. Neo4j Article 数 + 孤立实体数 + 漂移检测
        neo4j = Neo4jService()
        await neo4j.connect()
        async with neo4j._driver.session() as s:
            r = await s.run("MATCH (a:Article) RETURN count(a) AS c")
            rec = await r.single()
            total_in_kg = rec["c"] if rec else 0
            r_unlinked = await s.run(
                """
                MATCH (a:Article)
                WHERE NOT (a)-[:CONTAINS_ENTITY]->(:Entity)
                RETURN count(a) AS c
                """
            )
            rec_unlinked = await r_unlinked.single()
            articles_without_entities = rec_unlinked["c"] if rec_unlinked else 0
            r_orphan = await s.run("""
                MATCH (e:Entity)
                WHERE e.entity_type IN $article_entity_types
                  AND NOT (e)<-[:CONTAINS_ENTITY]-(:Article)
                RETURN count(e) AS c
            """, article_entity_types=[
                "PERSON", "ORGANIZATION", "LOCATION", "TECHNOLOGY",
                "EVENT", "CONCEPT", "DATE",
            ])
            rec_orphan = await r_orphan.single()
            orphan_entities = rec_orphan["c"] if rec_orphan else 0
        await neo4j.close()

        return {
            "status": "success",
            "by_status": normalized,
            "total_in_db": total_in_db,
            "total_in_kg": total_in_kg,
            "articles_without_entities": articles_without_entities,
            "orphan_entities": orphan_entities,
            "drift_detected": (
                total_in_db != total_in_kg
                or articles_without_entities > 0
                or orphan_entities > 0
            ),
            "failed_articles": failed_articles,
            "partial_articles": partial_articles,
            "sync_state": get_sync_state(),
        }
    except Exception as e:
        logger.error(f"获取同步状态失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reconcile")
async def reconcile_knowledge_graph(
    apply: bool = Query(default=False, description="是否自动修复"),
    db: Session = Depends(get_db)
):
    """
    对账:对比 SQLite 与 Neo4j
    - apply=false: 仅返回漂移报告
    - apply=true:  自动修复(missing → 异步抽; orphan → 删; dirty → 标 pending)
    """
    try:
        result = await kg_sync.reconcile(apply=apply, db=db)
        return {"status": "success", **result}
    except Exception as e:
        logger.error(f"对账失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cleanup-orphans")
async def cleanup_orphan_entities():
    """删除没有任何 Article 入边的孤立 Entity 节点及其关系。"""
    neo4j = Neo4jService()
    try:
        await neo4j.connect()
        deleted = await neo4j.cleanup_orphan_entities()
        stats = await neo4j.get_graph_stats()
        return {
            "status": "success",
            "deleted": deleted,
            "stats": stats,
        }
    except Exception as e:
        logger.error(f"清理孤立实体失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await neo4j.close()


@router.post("/process-pending")
async def process_pending_articles_now(
    limit: int = Query(default=200, ge=1, le=1000, description="最多处理多少篇"),
    max_concurrency: int = Query(default=3, ge=1, le=10, description="并发数"),
    include_failed: bool = Query(default=True, description="是否同时重试失败文章"),
    include_success: bool = Query(default=False, description="是否安全重建已成功文章的证据"),
    include_partial: bool = Query(default=False, description="是否重试抽取未完成文章"),
    db: Session = Depends(get_db)
):
    """
    立即触发:把 kg_status in (NULL, 'pending', 'skipped') 的文章批量抽实体。
    复用启动时的 process_pending_articles,带并发和限流,不会打爆 LLM。
    """
    try:
        result = await kg_sync.process_pending_articles(
            db=db,
            max_concurrency=max_concurrency,
            rate_limit_seconds=0.5,
            limit=limit,
            include_failed=include_failed,
            include_success=include_success,
            include_partial=include_partial,
        )
        return {
            "status": "success",
            **result,
            "results": {"success": result.get("scheduled", 0), "skipped": 0, "failed": 0},
        }
    except Exception as e:
        logger.error(f"立即抽取失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reprocess/{article_id}")
async def reprocess_article(article_id: str, db: Session = Depends(get_db)):
    """重抽单篇文章:清旧实体+关系,重新走 extract_and_link_entities"""
    article = db.query(Article).filter(Article.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404, detail="文章不存在")

    try:
        # 先抽取，解析成功后由同步服务原子式替换旧知识，避免失败重抽清空可用数据。
        article.kg_status = "pending"
        article.kg_error_message = None
        db.commit()

        await kg_sync.extract_and_link_entities(article_id)
        db.expire_all()
        refreshed = db.query(Article).filter(Article.id == article_id).first()
        final_status = refreshed.kg_status if refreshed else "failed"
        return {
            "status": "success" if final_status == "success" else "failed",
            "article_id": article_id,
            "kg_status": final_status,
            "error": refreshed.kg_error_message if refreshed else "文章不存在",
            "message": "重抽完成" if final_status == "success" else "重抽失败",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"重抽失败 {article_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/article/{article_id}/status")
async def get_article_kg_status(article_id: str, db: Session = Depends(get_db)):
    """获取文章在 KG 中的状态(用于前端轮询)"""
    article = db.query(Article).filter(Article.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404, detail="文章不存在")

    # 查询 Neo4j 实体 / 关系数
    entity_count = 0
    relation_count = 0
    try:
        neo4j = Neo4jService()
        await neo4j.connect()
        async with neo4j._driver.session() as session:
            r1 = await session.run("""
                MATCH (a:Article {id: $id})-[:CONTAINS_ENTITY]->(e:Entity)
                RETURN count(e) AS c
            """, id=article_id)
            rec1 = await r1.single()
            entity_count = rec1["c"] if rec1 else 0
            r2 = await session.run("""
                MATCH (a:Article {id: $id})-[:CONTAINS_ENTITY]->(e1:Entity)
                      (e1)-[r:RELATES_TO]->(e2:Entity)
                RETURN count(r) AS c
            """, id=article_id)
            rec2 = await r2.single()
            relation_count = rec2["c"] if rec2 else 0
        await neo4j.close()
    except Exception as e:
        logger.warning(f"查询 KG 实体/关系数失败 {article_id}: {e}")

    return {
        "status": "success",
        "article_id": article_id,
        "kg_status": article.kg_status,
        "kg_processed_at": article.kg_processed_at.isoformat() if article.kg_processed_at else None,
        "kg_error_message": article.kg_error_message,
        "entity_count": entity_count,
        "relation_count": relation_count
    }


# ==================== 对话页 KG 问答 (M1) ====================

class QARequest(BaseModel):
    question: str
    model_id: str = "default"
    session_id: Optional[str] = None


class PathExploreRequest(BaseModel):
    source: str
    target: str
    max_depth: int = 4
    limit: int = 10
    relation_types: List[str] = Field(default_factory=list)


@router.post("/explore/path")
async def explore_shortest_path(
    request: PathExploreRequest,
    db: Session = Depends(get_db),
):
    """查找实体间可解释最短路径，并补充路径所引用的文章元数据。"""
    source = request.source.strip()
    target = request.target.strip()
    if not source or not target:
        raise HTTPException(status_code=400, detail="起点和终点实体不能为空")
    if source == target:
        raise HTTPException(status_code=400, detail="起点和终点不能相同")

    neo4j = Neo4jService()
    try:
        paths = await neo4j.find_shortest_paths(
            source_name=source,
            target_name=target,
            max_depth=request.max_depth,
            limit=request.limit,
            relation_types=request.relation_types,
        )
    finally:
        await neo4j.close()

    article_ids = {
        article_id
        for path in paths
        for rel in path.get("relationships", [])
        for article_id in rel.get("source_articles", [])
    }
    articles = []
    if article_ids:
        rows = db.query(Article).filter(Article.id.in_(article_ids)).all()
        articles = [
            {
                "id": str(article.id),
                "title": article.title,
                "url": article.url,
                "published_at": article.published_at.isoformat()
                if article.published_at else None,
            }
            for article in rows
        ]

    return {
        "status": "success",
        "source": source,
        "target": target,
        "count": len(paths),
        "paths": paths,
        "articles": articles,
    }


@router.get("/explore/entity-profile/{entity_name}")
async def explore_entity_profile(
    entity_name: str,
    neighbor_limit: int = Query(default=20, ge=1, le=100),
    evidence_limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """返回实体跨文档档案、邻居关系和原文证据。"""
    neo4j = Neo4jService()
    try:
        profile = await neo4j.get_entity_profile(
            entity_name=entity_name,
            neighbor_limit=neighbor_limit,
            evidence_limit=evidence_limit,
        )
    finally:
        await neo4j.close()
    if not profile:
        raise HTTPException(status_code=404, detail="实体不存在")

    article_ids = profile.pop("article_ids", [])
    article_rows = db.query(Article).filter(Article.id.in_(article_ids)).all() \
        if article_ids else []
    articles_by_id = {str(article.id): article for article in article_rows}
    profile["articles"] = [
        {
            "id": article_id,
            "title": articles_by_id[article_id].title,
            "url": articles_by_id[article_id].url,
            "summary": articles_by_id[article_id].summary,
            "published_at": articles_by_id[article_id].published_at.isoformat()
            if articles_by_id[article_id].published_at else None,
        }
        for article_id in article_ids
        if article_id in articles_by_id
    ]
    return {"status": "success", **profile}


@router.get("/explore/entity-ranking")
async def explore_entity_ranking(
    limit: int = Query(default=50, ge=1, le=200),
    entity_type: Optional[str] = Query(default=None),
):
    """返回按出现次数（文章引用数）排序的实体排行。"""
    neo4j = Neo4jService()
    try:
        query = """
            MATCH (e:Entity)
            WHERE e.source_articles IS NOT NULL AND size(e.source_articles) > 0
        """
        params = {"limit": limit}

        if entity_type:
            query += " AND e.entity_type = $entity_type"
            params["entity_type"] = entity_type

        query += """
            WITH e, size(e.source_articles) AS occurrence_count
            ORDER BY occurrence_count DESC
            LIMIT $limit
            RETURN e.name AS name, e.entity_type AS entity_type,
                   e.subtype AS subtype, occurrence_count,
                   e.source_articles AS source_articles
        """

        records = await neo4j.execute(query, params)
        entities = [
            {
                "name": r["name"],
                "entity_type": r["entity_type"],
                "subtype": r.get("subtype"),
                "occurrence_count": r["occurrence_count"],
                "source_articles": r.get("source_articles", []),
            }
            for r in records
        ]
    finally:
        await neo4j.close()

    return {"status": "success", "count": len(entities), "entities": entities}


class AliasReviewRequest(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)
    decision: Literal["approved", "rejected"]
    canonical_name: Optional[str] = Field(default=None, max_length=500)
    note: str = Field(default="", max_length=500)


class CrossDocumentReviewRequest(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=500)


class LegacyRelationReviewRequest(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)
    rel_type: str = Field(min_length=1, max_length=100)
    decision: Literal["kept", "deleted"]
    note: str = Field(default="", max_length=500)


class InferenceReviewRequest(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)
    rel_type: Literal["part_of", "located_in", "precedes", "succeeds"]
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=500)


class LinkPredictionReviewRequest(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=500)


class CausalReviewRequest(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)
    rel_type: Literal["causes", "enables"]
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=500)


@router.get("/mining/reviews")
async def get_mining_reviews(
    review_type: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
):
    neo4j = Neo4jService()
    try:
        reviews = await neo4j.get_mining_reviews(review_type=review_type, limit=limit)
        return {"status": "success", "count": len(reviews), "reviews": reviews}
    finally:
        await neo4j.close()


@router.post("/mining/reviews/{review_id}/undo")
async def undo_mining_review(review_id: str):
    neo4j = Neo4jService()
    try:
        review = await neo4j.undo_mining_review(review_id)
        return {"status": "success", "review": review}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    finally:
        await neo4j.close()


@router.get("/mining/aliases")
async def get_alias_candidates(
    min_shared_articles: int = Query(default=2, ge=1, le=20),
    min_score: float = Query(default=0.32, ge=0.0, le=1.0),
    limit: int = Query(default=50, ge=1, le=200),
    include_reviewed: bool = Query(default=False),
):
    """返回待人工确认的实体别名候选，不自动合并。"""
    neo4j = Neo4jService()
    try:
        candidates = await neo4j.get_alias_candidates(
            min_shared_articles=min_shared_articles,
            min_score=min_score,
            limit=limit,
            include_reviewed=include_reviewed,
        )
        return {"status": "success", "count": len(candidates), "candidates": candidates}
    finally:
        await neo4j.close()


@router.post("/mining/aliases/review")
async def review_alias_candidate(request: AliasReviewRequest):
    neo4j = Neo4jService()
    try:
        review = await neo4j.review_alias_candidate(
            source=request.source.strip(),
            target=request.target.strip(),
            decision=request.decision,
            canonical_name=request.canonical_name.strip() if request.canonical_name else None,
            note=request.note.strip(),
        )
        return {"status": "success", "review": review}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    finally:
        await neo4j.close()


@router.post("/mining/backfill-legacy-evidence")
async def backfill_legacy_relation_evidence(
    apply: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    """从同时包含两端实体的原文句子中恢复旧关系证据。"""
    neo4j = Neo4jService()
    try:
        relations = await neo4j.get_legacy_relations(limit=limit)
        article_ids = {
            article_id
            for relation in relations
            for article_id in set(relation.get("source_article_ids") or [])
                & set(relation.get("target_article_ids") or [])
        }
        articles = db.query(Article).filter(Article.id.in_(article_ids)).all() \
            if article_ids else []
        article_by_id = {str(article.id): article for article in articles}
        recovered = []

        for relation in relations:
            shared_ids = sorted(
                set(relation.get("source_article_ids") or [])
                & set(relation.get("target_article_ids") or [])
            )
            for article_id in shared_ids:
                article = article_by_id.get(str(article_id))
                if not article:
                    continue
                evidence = find_relation_evidence(
                    article.content or "",
                    relation["source"],
                    relation["target"],
                )
                if not evidence:
                    continue
                item = {
                    "source": relation["source"],
                    "target": relation["target"],
                    "rel_type": relation["rel_type"],
                    "article_id": str(article_id),
                    "article_title": article.title,
                    "evidence": evidence,
                }
                if apply:
                    item["applied"] = await neo4j.add_recovered_relation_evidence(
                        source=relation["source"],
                        target=relation["target"],
                        rel_type=relation["rel_type"],
                        article_id=str(article_id),
                        evidence=evidence,
                        confidence=relation.get("confidence") or 0.6,
                    )
                recovered.append(item)
                break

        return {
            "status": "success",
            "applied": apply,
            "legacy_scanned": len(relations),
            "recoverable": len(recovered),
            "remaining": len(relations) - len(recovered),
            "recovered": recovered,
        }
    finally:
        await neo4j.close()


@router.get("/mining/cross-document")
async def get_cross_document_candidates(
    min_shared_articles: int = Query(default=2, ge=2, le=20),
    limit: int = Query(default=50, ge=1, le=200),
    include_reviewed: bool = Query(default=False),
):
    """返回跨文档稳定共现但尚无显式关系的实体对。"""
    neo4j = Neo4jService()
    try:
        candidates = await neo4j.get_cross_document_candidates(
            min_shared_articles=min_shared_articles,
            limit=limit,
            include_reviewed=include_reviewed,
        )
        return {"status": "success", "count": len(candidates), "candidates": candidates}
    finally:
        await neo4j.close()


@router.post("/mining/cross-document/review")
async def review_cross_document_candidate(request: CrossDocumentReviewRequest):
    neo4j = Neo4jService()
    try:
        review = await neo4j.review_cross_document_candidate(
            source=request.source.strip(),
            target=request.target.strip(),
            decision=request.decision,
            note=request.note.strip(),
        )
        return {"status": "success", "review": review}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    finally:
        await neo4j.close()


@router.get("/mining/legacy-relations")
async def get_legacy_relations(limit: int = Query(default=100, ge=1, le=1000)):
    neo4j = Neo4jService()
    try:
        relations = await neo4j.get_legacy_relations(limit=limit)
        return {"status": "success", "count": len(relations), "relations": relations}
    finally:
        await neo4j.close()


@router.post("/mining/legacy-relations/review")
async def review_legacy_relation(request: LegacyRelationReviewRequest):
    neo4j = Neo4jService()
    try:
        review = await neo4j.review_legacy_relation(
            source=request.source.strip(),
            target=request.target.strip(),
            rel_type=request.rel_type.strip(),
            decision=request.decision,
            note=request.note.strip(),
        )
        return {"status": "success", "review": review}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    finally:
        await neo4j.close()


@router.get("/mining/communities")
async def get_communities(
    min_size: int = Query(default=3, ge=2, le=100),
    limit: int = Query(default=20, ge=1, le=100),
):
    """执行轻量标签传播，返回实体社区。"""
    neo4j = Neo4jService()
    try:
        communities = await neo4j.get_communities(min_size=min_size, limit=limit)
        return {"status": "success", "count": len(communities), "communities": communities}
    finally:
        await neo4j.close()


@router.get("/mining/inferences")
async def get_transitive_inferences(
    relation_types: List[str] = Query(default=[]),
    max_hops: int = Query(default=3, ge=2, le=5),
    limit: int = Query(default=100, ge=1, le=500),
):
    """返回带完整关系链和来源文章的传递规则候选，不自动写图。"""
    allowed = {"part_of", "located_in", "precedes", "succeeds"}
    requested = relation_types or sorted(allowed)
    invalid = sorted(set(requested) - allowed)
    if invalid:
        raise HTTPException(status_code=400, detail=f"不支持的传递关系: {', '.join(invalid)}")
    neo4j = Neo4jService()
    try:
        inferences = await neo4j.get_transitive_inferences(
            relation_types=requested,
            max_hops=max_hops,
            limit=limit,
        )
        return {"status": "success", "count": len(inferences), "inferences": inferences}
    finally:
        await neo4j.close()


@router.post("/mining/inferences/review")
async def review_transitive_inference(request: InferenceReviewRequest):
    neo4j = Neo4jService()
    try:
        review = await neo4j.review_inference_candidate(
            source=request.source.strip(),
            target=request.target.strip(),
            rel_type=request.rel_type,
            decision=request.decision,
            note=request.note.strip(),
        )
        return {"status": "success", "review": review}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    finally:
        await neo4j.close()


@router.get("/mining/link-predictions")
async def get_link_predictions(
    min_common_neighbors: int = Query(default=2, ge=1, le=20),
    min_score: float = Query(default=0.2, ge=0.0, le=1.0),
    limit: int = Query(default=100, ge=1, le=500),
):
    neo4j = Neo4jService()
    try:
        predictions = await neo4j.get_link_predictions(
            min_common_neighbors=min_common_neighbors,
            min_score=min_score,
            limit=limit,
        )
        return {"status": "success", "count": len(predictions), "predictions": predictions}
    finally:
        await neo4j.close()


@router.post("/mining/link-predictions/review")
async def review_link_prediction(request: LinkPredictionReviewRequest):
    neo4j = Neo4jService()
    try:
        review = await neo4j.review_link_prediction(
            source=request.source.strip(),
            target=request.target.strip(),
            decision=request.decision,
            note=request.note.strip(),
        )
        return {"status": "success", "review": review}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    finally:
        await neo4j.close()


@router.post("/mining/embeddings/generate")
async def generate_graph_embeddings(
    dimensions: int = Query(default=16, ge=2, le=64),
):
    neo4j = Neo4jService()
    try:
        result = await neo4j.generate_graph_embeddings(dimensions=dimensions)
        return {"status": "success", **result}
    finally:
        await neo4j.close()


@router.get("/mining/embeddings/status")
async def get_graph_embedding_status():
    neo4j = Neo4jService()
    try:
        result = await neo4j.get_graph_embedding_status()
        return {"status": "success", **result}
    finally:
        await neo4j.close()


@router.get("/mining/embeddings/evaluate")
async def evaluate_graph_embeddings(k: int = Query(default=5, ge=1, le=20)):
    neo4j = Neo4jService()
    try:
        result = await neo4j.evaluate_graph_embeddings(k=k)
        return {"status": "success", **result}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    finally:
        await neo4j.close()


@router.delete("/mining/embeddings")
async def clear_graph_embeddings():
    neo4j = Neo4jService()
    try:
        cleared = await neo4j.clear_graph_embeddings()
        return {"status": "success", "cleared": cleared}
    finally:
        await neo4j.close()


@router.get("/mining/similar/{entity_name}")
async def get_similar_entities(
    entity_name: str,
    limit: int = Query(default=20, ge=1, le=100),
    same_type: bool = Query(default=True),
    min_score: float = Query(default=0.0, ge=-1.0, le=1.0),
):
    neo4j = Neo4jService()
    try:
        result = await neo4j.get_similar_entities(
            entity_name=entity_name,
            limit=limit,
            same_type=same_type,
            min_score=min_score,
        )
        return {"status": "success", **result}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    finally:
        await neo4j.close()


@router.get("/mining/causal-candidates")
async def get_causal_candidates(
    limit: int = Query(default=100, ge=1, le=500),
    include_history: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    neo4j = Neo4jService()
    try:
        articles = get_causal_article_records(db) if include_history else None
        candidates = await neo4j.get_causal_candidates(limit=limit, articles=articles)
        return {"status": "success", "count": len(candidates), "candidates": candidates}
    finally:
        await neo4j.close()


@router.post("/mining/causal-candidates/review")
async def review_causal_candidate(
    request: CausalReviewRequest,
    db: Session = Depends(get_db),
):
    neo4j = Neo4jService()
    try:
        review = await neo4j.review_causal_candidate(
            source=request.source.strip(),
            target=request.target.strip(),
            rel_type=request.rel_type,
            decision=request.decision,
            note=request.note.strip(),
            articles=get_causal_article_records(db),
        )
        return {"status": "success", "review": review}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    finally:
        await neo4j.close()


@router.get("/mining/causal-chains")
async def get_causal_chains(
    source: Optional[str] = Query(default=None, max_length=500),
    target: Optional[str] = Query(default=None, max_length=500),
    max_hops: int = Query(default=4, ge=1, le=6),
    limit: int = Query(default=100, ge=1, le=500),
):
    neo4j = Neo4jService()
    try:
        chains = await neo4j.get_causal_chains(
            source=source,
            target=target,
            max_hops=max_hops,
            limit=limit,
        )
        return {"status": "success", "count": len(chains), "chains": chains}
    finally:
        await neo4j.close()


@router.get("/mining/timeline")
async def get_event_timeline(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    query: Optional[str] = Query(default=None, max_length=200),
    limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")
    neo4j = Neo4jService()
    try:
        events = await neo4j.get_temporal_events(limit=limit)
    finally:
        await neo4j.close()

    article_ids = {
        str(article_id)
        for event in events
        for article_id in event.get("article_ids", [])
        if article_id
    }
    articles = db.query(Article).filter(Article.id.in_(article_ids)).all() if article_ids else []
    article_by_id = {str(article.id): article for article in articles}
    timeline = []
    normalized_query = (query or "").strip().casefold()
    for event in events:
        if normalized_query and normalized_query not in (
            f"{event.get('name', '')} {event.get('description', '')}".casefold()
        ):
            continue
        event_articles = [
            article_by_id[str(article_id)]
            for article_id in event.get("article_ids", [])
            if str(article_id) in article_by_id
        ]
        published_dates = sorted({
            article.published_at for article in event_articles if article.published_at
        })
        observed_at = published_dates[0] if published_dates else None
        if start_date and (not observed_at or observed_at < start_date):
            continue
        if end_date and (not observed_at or observed_at > end_date):
            continue
        timeline.append({
            **event,
            "observed_at": observed_at.isoformat() if observed_at else None,
            "articles": [
                {
                    "id": str(article.id),
                    "title": article.title,
                    "url": article.url,
                    "published_at": article.published_at.isoformat() if article.published_at else None,
                }
                for article in event_articles
            ],
        })
    timeline.sort(key=lambda item: (item["observed_at"] is None, item["observed_at"] or "", item["name"]))
    return {"status": "success", "count": len(timeline), "events": timeline[:limit]}


@router.post("/qa/answer")
async def kg_qa_answer(req: QARequest, db: Session = Depends(get_db)):
    """对话页 KG 问答入口"""
    try:
        result = await answer_question(
            question=req.question,
            model_id=req.model_id,
            session_id=req.session_id,
            db=db,
        )
        return result
    except Exception as e:
        logger.error(f"qa/answer 失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entity-context/{entity_name}")
async def get_entity_context(
    entity_name: str,
    limit: int = Query(default=5, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """获取实体的原文出处(出现的文章 + 原文片段)"""
    neo4j = Neo4jService()
    await neo4j.connect()
    try:
        async with neo4j._driver.session() as session:
            r = await session.run(
                "MATCH (e:Entity {name:$n}) "
                "RETURN e.name AS name, e.entity_type AS type, "
                "e.subtype AS subtype, e.description AS description, "
                "e.source_articles AS source_articles",
                n=entity_name,
            )
            row = await r.single()
        if not row:
            return {"entity": {"name": entity_name, "type": None, "subtype": None, "description": None}, "articles": []}

        article_ids = (row["source_articles"] or [])[:limit]
        articles_out = []
        if article_ids:
            arts = db.query(Article).filter(Article.id.in_(article_ids)).all()
            for a in arts:
                text = (a.content or "") + " " + (a.summary or "")
                positions = []
                if entity_name and entity_name in text:
                    start = 0
                    while True:
                        idx = text.find(entity_name, start)
                        if idx < 0:
                            break
                        positions.append([idx, idx + len(entity_name)])
                        start = idx + len(entity_name)
                snippet = ""
                if positions:
                    s, e = positions[0]
                    snippet = text[max(0, s - 60):min(len(text), e + 60)]
                articles_out.append({
                    "article_id": str(a.id),
                    "title": a.title or "(无标题)",
                    "snippet": snippet,
                    "highlight_positions": positions[:5],
                })

        return {
            "entity": {
                "name": row["name"], "type": row["type"],
                "subtype": row["subtype"], "description": row["description"],
            },
            "articles": articles_out,
        }
    finally:
        await neo4j.close()


# ==================== 知识自增强循环 API ====================

from app.services.kg.self_enhancement import KnowledgeSelfEnhancement
from app.schemas.kg import (
    ProcessArticleRequest,
    SelfEnhancementResultSchema,
    EnhancementStatsSchema,
    KnowledgePointListResponse,
    AssociationListResponse,
    TrendPredictionRequest,
    TrendPredictionResponse,
)

# 全局自增强循环服务实例
_self_enhancement_service: Optional[KnowledgeSelfEnhancement] = None


async def _get_self_enhancement_service() -> KnowledgeSelfEnhancement:
    """获取自增强循环服务实例"""
    global _self_enhancement_service
    if _self_enhancement_service is None:
        neo4j = Neo4jService()
        # 使用LLMService作为LLM客户端
        from app.core.llm import llm_service
        _self_enhancement_service = KnowledgeSelfEnhancement(
            kg_service=neo4j,
            llm_client=llm_service
        )
    return _self_enhancement_service


@router.post("/self-enhancement/process-article")
async def process_article_for_enhancement(
    request: ProcessArticleRequest,
    db: Session = Depends(get_db)
):
    """
    处理单篇文章，启动自增强循环

    流程：
    1. 文章预处理
    2. LLM 提取知识点
    3. 发现知识关联
    4. 生成知识总结
    5. 存储到图数据库

    请求体：
    - article_id: 文章 ID
    - article_content: 文章内容（可选，不提供则从数据库获取）

    返回：
    - enhancement_id: 增强任务 ID
    - status: 处理状态
    - knowledge_points_count: 提取的知识点数量
    - associations_count: 发现的关联数量
    - summary: 生成的总结
    """
    service = await _get_self_enhancement_service()

    # 如果没有提供内容，从数据库获取
    article_content = request.article_content
    article = None
    if not article_content:
        article = db.query(Article).filter(Article.id == request.article_id).first()
        if not article:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"文章不存在: {request.article_id}"
            )
        if not article.content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="文章内容为空"
            )
        article_content = article.content

    input_hash = hashlib.sha256((article_content or "").encode("utf-8")).hexdigest()
    job_id = f"enh_{request.article_id}_{input_hash[:16]}"
    job = db.query(KnowledgeJob).filter(KnowledgeJob.id == job_id).first()
    if job is None:
        job = KnowledgeJob(
            id=job_id,
            target_id=request.article_id,
            input_hash=input_hash,
            status="processing",
            progress=0,
        )
        db.add(job)
    else:
        job.status = "processing"
        job.progress = 0
        job.error_message = None
        job.completed_at = None
    job.started_at = datetime.utcnow()
    db.commit()

    result = await service.process_new_article(
        article_id=request.article_id,
        article_content=article_content,
        source_url=article.url if article else None,
        source_published_at=article.published_at.isoformat() if article and article.published_at else None,
    )

    job.status = result.status
    job.progress = result.progress
    job.result_summary = result.summary
    job.error_message = result.error_message
    job.completed_at = datetime.utcnow() if result.status in {"completed", "failed"} else None
    db.commit()

    return {
        "enhancement_id": result.enhancement_id,
        "article_id": result.article_id,
        "status": result.status,
        "progress": result.progress,
        "knowledge_points_count": result.knowledge_points_count,
        "associations_count": result.associations_count,
        "summary": result.summary,
        "error_message": result.error_message,
        "created_at": result.created_at.isoformat() if result.created_at else None,
        "completed_at": result.completed_at.isoformat() if result.completed_at else None,
    }


@router.get("/self-enhancement/status/{enhancement_id}")
async def get_enhancement_status(enhancement_id: str):
    """
    获取增强任务状态

    返回：
    - status: 处理状态 (pending/processing/completed/failed)
    - progress: 处理进度 (0-100)
    - result: 处理结果
    """
    service = await _get_self_enhancement_service()
    result = service.get_processing_status(enhancement_id)

    if result is None:
        from app.core.database import get_session_local
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            job = session.query(KnowledgeJob).filter(KnowledgeJob.id == enhancement_id).first()
            if job is not None:
                return {
                    "enhancement_id": job.id,
                    "article_id": job.target_id,
                    "status": job.status,
                    "progress": job.progress,
                    "knowledge_points_count": 0,
                    "associations_count": 0,
                    "summary": job.result_summary,
                    "error_message": job.error_message,
                    "created_at": job.created_at.isoformat() if job.created_at else None,
                    "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                }
        finally:
            session.close()

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Enhancement task not found: {enhancement_id}"
        )

    return {
        "enhancement_id": result.enhancement_id,
        "article_id": result.article_id,
        "status": result.status,
        "progress": result.progress,
        "knowledge_points_count": result.knowledge_points_count,
        "associations_count": result.associations_count,
        "summary": result.summary,
        "error_message": result.error_message,
        "created_at": result.created_at.isoformat() if result.created_at else None,
        "completed_at": result.completed_at.isoformat() if result.completed_at else None,
    }


@router.get("/self-enhancement/stats")
async def get_enhancement_stats(db: Session = Depends(get_db)):
    """
    获取增强统计信息

    返回：
    - total_articles_processed: 已处理文章数
    - total_knowledge_points: 知识点总数
    - total_associations: 关联总数
    - average_points_per_article: 平均知识点/文章
    - average_associations_per_point: 平均关联/知识点
    - last_processed_at: 最后处理时间
    """
    from app.models.article import Article

    # ``kg_status`` only records entity extraction.  Insight statistics must
    # instead be based on the enhancement job for the article's current body.
    articles = db.query(Article).filter(
        Article.status.in_(["completed", "success"])
    ).all()
    enhancement_status = _enhancement_statuses(db, articles)
    processed_count = sum(
        1 for article in articles
        if enhancement_status.get(str(article.id)) == "completed"
    )
    pending_count = sum(
        1 for article in articles
        if enhancement_status.get(str(article.id)) in {None, "pending", "processing"}
    )
    failed_count = sum(
        1 for article in articles
        if enhancement_status.get(str(article.id)) in {"failed", "rejected"}
    )
    last_completed_job = db.query(KnowledgeJob).filter(
        KnowledgeJob.job_type == "article_enhancement",
        KnowledgeJob.status == "completed",
    ).order_by(KnowledgeJob.completed_at.desc()).first()
    last_processed_at = last_completed_job.completed_at if last_completed_job else None

    # 从Neo4j查询知识点和关联数量
    total_points = 0
    total_associations = 0
    quality_metrics = {}

    try:
        from app.services.kg.graph import Neo4jService

        neo4j = Neo4jService()
        await neo4j.connect()
        try:
            # 查询知识点数量
            result = await neo4j.execute(
                'MATCH (n:Entity) WHERE n.entity_type = "KnowledgePoint" RETURN count(n) as count'
            )
            total_points = result[0]['count'] if result else 0

            # 查询关联数量
            result = await neo4j.execute(
                'MATCH (a:Entity)-[r]->(b:Entity) WHERE a.entity_type = "KnowledgePoint" AND b.entity_type = "KnowledgePoint" RETURN count(r) as count'
            )
            total_associations = result[0]['count'] if result else 0
            quality_metrics = await neo4j.get_knowledge_quality_metrics()
        finally:
            await neo4j.close()
    except Exception as neo4j_error:
        logger.warning(f"从Neo4j获取统计数据失败: {neo4j_error}")

    # 计算平均值
    average_points_per_article = (
        total_points / processed_count if processed_count > 0 else 0
    )
    average_associations_per_point = (
        total_associations / total_points if total_points > 0 else 0
    )

    return {
        "total_articles_processed": processed_count,
        "enhancement_pending_articles": pending_count,
        "enhancement_failed_articles": failed_count,
        "total_knowledge_points": total_points,
        "total_associations": total_associations,
        "average_points_per_article": average_points_per_article,
        "average_associations_per_point": average_associations_per_point,
        "last_processed_at": (
            last_processed_at.isoformat()
            if last_processed_at
            else None
        ),
        "quality_metrics": quality_metrics,
    }


@router.get("/self-enhancement/knowledge-points")
async def list_knowledge_points(
    article_id: Optional[str] = Query(None, description="按文章 ID 筛选"),
    limit: int = Query(50, ge=1, le=200, description="返回数量限制")
):
    """
    获取知识点列表

    参数：
    - article_id: 可选，按文章筛选
    - limit: 返回数量限制

    返回：
    - knowledge_points: 知识点列表
    - total: 总数
    """
    neo4j = Neo4jService()
    try:
        # 查询知识点 (存储为 Entity 节点，entity_type='KnowledgePoint')
        query = """
        MATCH (n:Entity)
        WHERE n.entity_type = 'KnowledgePoint'
        AND ($article_id IS NULL OR n.article_id = $article_id)
        RETURN n
        ORDER BY n.created_at DESC
        LIMIT $limit
        """
        result = await neo4j.execute(query, {"article_id": article_id, "limit": limit})

        knowledge_points = []
        for record in result:
            node = record["n"]
            knowledge_points.append({
                "id": node.get("id", ""),
                "article_id": node.get("article_id", ""),
                "title": node.get("title") or node.get("name", ""),
                "content": node.get("content", ""),
                "category": node.get("category", "concept"),
                "confidence": node.get("confidence", 0.5),
                "keywords": node.get("keywords", []),
                "evidence": node.get("evidence", []),
                "source_span": node.get("source_span") or None,
                "source_url": node.get("source_url") or None,
                "status": node.get("status", "candidate"),
                "model_name": node.get("model_name") or None,
                "prompt_version": node.get("prompt_version") or None,
                "created_at": node.get("created_at", ""),
            })

        # 获取总数
        count_query = """
        MATCH (n:Entity)
        WHERE n.entity_type = 'KnowledgePoint'
        AND ($article_id IS NULL OR n.article_id = $article_id)
        RETURN count(n) as total
        """
        count_result = await neo4j.execute(count_query, {"article_id": article_id})
        total = count_result[0]["total"] if count_result else 0

        return {
            "knowledge_points": knowledge_points,
            "total": total,
        }
    finally:
        await neo4j.close()


@router.get("/self-enhancement/associations")
async def list_associations(
    knowledge_point_id: Optional[str] = Query(None, description="按知识点 ID 筛选"),
    min_strength: float = Query(0.3, ge=0, le=1, description="最小关联强度")
):
    """
    获取知识点关联列表

    参数：
    - knowledge_point_id: 可选，按知识点筛选
    - min_strength: 最小关联强度

    返回：
    - associations: 关联列表
    - total: 总数
    """
    neo4j = Neo4jService()
    try:
        # 查询关联 (知识点存储为 Entity 节点，entity_type='KnowledgePoint')
        query = """
        MATCH (a:Entity)-[r]->(b:Entity)
        WHERE a.entity_type = 'KnowledgePoint'
        AND b.entity_type = 'KnowledgePoint'
        AND ($kp_id IS NULL OR a.id = $kp_id OR b.id = $kp_id)
        AND r.strength >= $min_strength
        RETURN a, b, r
        LIMIT 100
        """
        result = await neo4j.execute(query, {
            "kp_id": knowledge_point_id,
            "min_strength": min_strength
        })

        associations = []
        for record in result:
            source = record["a"]
            target = record["b"]
            rel = record["r"]

            associations.append({
                "id": rel.get("id", ""),
                "source_id": source.get("id", ""),
                "source_title": source.get("title") or source.get("name", ""),
                "target_id": target.get("id", ""),
                "target_title": target.get("title") or target.get("name", ""),
                "relation_type": rel.get("relation_type") or rel.get("candidate_type") or (
                    rel.type if hasattr(rel, 'type') else "related_to"
                ),
                "strength": rel.get("strength", 0.5),
                "evidence": rel.get("evidence", ""),
                "status": rel.get("status", "approved"),
                "candidate_type": rel.get("candidate_type", ""),
                "created_at": rel.get("created_at", ""),
            })

        return {
            "associations": associations,
            "total": len(associations),
        }
    finally:
        await neo4j.close()


class KnowledgeCandidateReviewRequest(BaseModel):
    """跨文档知识候选审核请求。"""
    decision: Literal["approved", "rejected"]
    relation_type: Optional[Literal[
        "same_as", "related_to", "supports", "contradicts", "extends"
    ]] = None


class KnowledgeSynthesisCreateRequest(BaseModel):
    """创建知识综合文档请求。"""
    topic: str = Field(..., min_length=1, max_length=300)
    article_ids: List[str] = Field(..., min_length=1, max_length=50)
    parent_synthesis_id: Optional[str] = None
    async_mode: bool = False


class KnowledgeSynthesisAutoRequest(BaseModel):
    topic: Optional[str] = Field(None, max_length=300)
    limit: int = Field(5, ge=1, le=20)


class KnowledgeSynthesisReviewRequest(BaseModel):
    decision: Literal["approved", "rejected"]


@router.post("/self-enhancement/associations/{association_id}/review")
async def review_knowledge_candidate(
    association_id: str,
    request: KnowledgeCandidateReviewRequest,
):
    """批准或驳回跨文档知识候选关系。"""
    neo4j = Neo4jService()
    try:
        reviewed = await neo4j.review_knowledge_candidate(
            candidate_id=association_id,
            decision=request.decision,
            relation_type=request.relation_type,
        )
        if not reviewed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="知识候选关系不存在或已被处理",
            )
        return {
            "id": association_id,
            "status": request.decision,
            "relation_type": request.relation_type,
        }
    finally:
        await neo4j.close()


@router.get("/self-enhancement/association-candidates")
async def list_knowledge_candidates(
    review_status: Optional[str] = Query("pending", description="审核状态"),
    limit: int = Query(100, ge=1, le=500),
):
    """读取跨文档知识候选审核记录。"""
    neo4j = Neo4jService()
    try:
        candidates = await neo4j.get_knowledge_candidates(
            review_status=review_status,
            limit=limit,
        )
        return {"candidates": candidates, "total": len(candidates)}
    finally:
        await neo4j.close()


@router.post("/self-enhancement/syntheses")
async def create_knowledge_synthesis(
    request: KnowledgeSynthesisCreateRequest,
    db: Session = Depends(get_db),
):
    """基于多篇文章生成一份待审核的知识综合文档。"""
    from app.core.llm import llm_service

    if request.async_mode:
        from app.services.knowledge_jobs import enqueue_synthesis
        job_id = enqueue_synthesis(
            topic=request.topic,
            article_ids=request.article_ids,
            parent_synthesis_id=request.parent_synthesis_id,
        )
        return {"job_id": job_id, "status": "queued"}

    neo4j = Neo4jService()
    try:
        service = KnowledgeSynthesisService(neo4j, llm_service)
        synthesis = await service.create_draft(
            db=db,
            topic=request.topic,
            article_ids=request.article_ids,
            parent_synthesis_id=request.parent_synthesis_id,
        )
        return _serialize_synthesis(synthesis)
    finally:
        await neo4j.close()


@router.post("/self-enhancement/syntheses/auto")
async def create_auto_knowledge_synthesis(
    request: KnowledgeSynthesisAutoRequest,
    db: Session = Depends(get_db),
):
    """自动发现候选事件并选择证据文档生成知识综合草稿。"""
    from app.core.llm import llm_service

    neo4j = Neo4jService()
    try:
        service = KnowledgeSynthesisService(neo4j, llm_service)
        synthesis = await service.create_auto_draft(db, topic=request.topic, limit=request.limit)
        return _serialize_synthesis(synthesis)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    finally:
        await neo4j.close()


@router.get("/self-enhancement/jobs/{job_id}")
async def get_knowledge_job(job_id: str, db: Session = Depends(get_db)):
    """查询持久化知识任务状态。"""
    job = db.query(KnowledgeJob).filter(KnowledgeJob.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识任务不存在")
    return {
        "id": job.id,
        "job_type": job.job_type,
        "target_id": job.target_id,
        "status": job.status,
        "progress": job.progress,
        "retry_count": job.retry_count,
        "next_retry_at": job.next_retry_at.isoformat() if job.next_retry_at else None,
        "result_summary": job.result_summary,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


@router.get("/self-enhancement/jobs")
async def list_knowledge_jobs(
    job_status: Optional[str] = Query(None, alias="status"),
    job_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """查询知识增强与知识综合的持久化任务。"""
    query = db.query(KnowledgeJob)
    if job_status:
        query = query.filter(KnowledgeJob.status == job_status)
    if job_type:
        query = query.filter(KnowledgeJob.job_type == job_type)
    jobs = query.order_by(KnowledgeJob.updated_at.desc()).limit(limit).all()
    return {
        "jobs": [
            {
                "id": job.id,
                "job_type": job.job_type,
                "target_id": job.target_id,
                "status": job.status,
                "progress": job.progress,
                "retry_count": job.retry_count,
                "next_retry_at": job.next_retry_at.isoformat() if job.next_retry_at else None,
                "error_message": job.error_message,
                "result_summary": job.result_summary,
                "created_at": job.created_at.isoformat() if job.created_at else None,
                "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            }
            for job in jobs
        ],
        "total": len(jobs),
    }


@router.get("/self-enhancement/syntheses")
async def list_knowledge_syntheses(
    topic: Optional[str] = Query(None),
    synthesis_status: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = db.query(KnowledgeSynthesis)
    if topic:
        query = query.filter(KnowledgeSynthesis.topic.ilike(f"%{topic}%"))
    if synthesis_status:
        query = query.filter(KnowledgeSynthesis.status == synthesis_status)
    items = query.order_by(KnowledgeSynthesis.updated_at.desc()).limit(limit).all()
    return {"syntheses": [_serialize_synthesis(item) for item in items], "total": len(items)}


@router.get("/self-enhancement/syntheses/search")
async def search_published_knowledge_syntheses(
    q: Optional[str] = Query(None, description="主题、标题或内容关键词"),
    limit: int = Query(50, ge=1, le=200),
):
    """检索已发布的综合文档。"""
    neo4j = Neo4jService()
    try:
        items = await neo4j.search_knowledge_syntheses(query=q, limit=limit)
        return {"syntheses": items, "total": len(items)}
    finally:
        await neo4j.close()


@router.get("/self-enhancement/syntheses/{synthesis_id}")
async def get_knowledge_synthesis(
    synthesis_id: str,
    db: Session = Depends(get_db),
):
    synthesis = db.query(KnowledgeSynthesis).filter(KnowledgeSynthesis.id == synthesis_id).first()
    if synthesis is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识综合文档不存在")
    return _serialize_synthesis(synthesis)


@router.post("/self-enhancement/syntheses/{synthesis_id}/review")
async def review_knowledge_synthesis(
    synthesis_id: str,
    request: KnowledgeSynthesisReviewRequest,
    db: Session = Depends(get_db),
):
    from app.core.llm import llm_service

    neo4j = Neo4jService()
    try:
        service = KnowledgeSynthesisService(neo4j, llm_service)
        synthesis = db.query(KnowledgeSynthesis).filter(KnowledgeSynthesis.id == synthesis_id).first()
        if synthesis is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识综合文档不存在")
        if request.decision == "approved":
            synthesis = await service.publish(db, synthesis_id)
        else:
            synthesis.status = "rejected"
            synthesis.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(synthesis)
        return _serialize_synthesis(synthesis)
    finally:
        await neo4j.close()


def _serialize_synthesis(synthesis: KnowledgeSynthesis) -> Dict[str, Any]:
    return {
        "id": synthesis.id,
        "topic": synthesis.topic,
        "title": synthesis.title,
        "content": synthesis.content,
        "summary": synthesis.summary,
        "source_document_ids": synthesis.source_document_ids or [],
        "source_claim_ids": synthesis.source_claim_ids or [],
        "iteration": synthesis.iteration,
        "parent_synthesis_id": synthesis.parent_synthesis_id,
        "model_name": synthesis.model_name,
        "prompt_version": synthesis.prompt_version,
        "quality_score": synthesis.quality_score,
        "status": synthesis.status,
        "created_at": synthesis.created_at.isoformat() if synthesis.created_at else None,
        "updated_at": synthesis.updated_at.isoformat() if synthesis.updated_at else None,
    }


# ==================== 文章选择 API ====================

@router.get("/self-enhancement/articles")
async def list_articles_for_enhancement(
    q: Optional[str] = Query(None, description="搜索关键词"),
    kg_status: Optional[str] = Query(None, description="知识图谱状态筛选"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    db: Session = Depends(get_db)
):
    """
    获取可用于自增强的文章列表

    参数：
    - q: 可选搜索关键词（匹配标题、摘要）
    - kg_status: 可选知识图谱状态筛选 (pending/success/failed)
    - page: 页码
    - page_size: 每页数量

    返回：
    - articles: 文章列表
    - total: 总数
    - page: 当前页
    - page_size: 每页数量
    """
    from app.models.article import Article, Category

    query = db.query(Article).filter(Article.status.in_(["completed", "success"]))

    # 搜索过滤
    if q:
        keyword_pattern = f"%{q}%"
        query = query.filter(
            or_(
                Article.title.ilike(keyword_pattern),
                Article.summary.ilike(keyword_pattern),
            )
        )

    # 知识图谱状态过滤
    if kg_status:
        query = query.filter(Article.kg_status == kg_status)

    # 获取总数
    total = query.count()

    # 分页查询
    offset = (page - 1) * page_size
    articles = query.order_by(
        Article.scraped_at.desc()
    ).offset(offset).limit(page_size).all()

    # 转换为响应格式
    items = []
    for a in articles:
        category_name = a.category.name if a.category else None
        items.append({
            "id": a.id,
            "title": a.title,
            "summary": a.summary[:200] if a.summary else "",
            "word_count": a.word_count,
            "published_at": a.published_at.isoformat() if a.published_at else None,
            "scraped_at": a.scraped_at.isoformat() if a.scraped_at else None,
            "kg_status": a.kg_status,
            "category_name": category_name,
            "source_type": a.source_type,
        })

    return {
        "articles": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


class BatchProcessRequest(BaseModel):
    article_ids: List[str] = Field(..., description="要处理的文章ID列表")
    force_reprocess: bool = Field(default=False, description="是否强制重新处理已处理的文章")


@router.post("/self-enhancement/batch-process")
async def batch_process_articles_enhancement(
    request: BatchProcessRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    批量处理文章（异步后台任务）

    参数：
    - article_ids: 要处理的文章ID列表
    - force_reprocess: 是否强制重新处理已处理的文章

    返回：
    - task_id: 任务ID，用于查询进度
    - total: 总文章数
    - status: 任务状态
    """
    import uuid

    # 验证文章存在
    articles = db.query(Article).filter(
        Article.id.in_(request.article_ids),
        Article.status.in_(["completed", "success"])
    ).all()

    if not articles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="没有找到有效的文章"
        )

    # Entity extraction success does not mean enhancement success.  Use the
    # current-content job state so migrated/failed documents can be processed.
    enhancement_status = _enhancement_statuses(db, articles)
    articles_to_process = []
    for article in articles:
        if request.force_reprocess or enhancement_status.get(str(article.id)) != "completed":
            articles_to_process.append(article)

    if not articles_to_process:
        return {
            "status": "success",
            "message": "所有文章都已处理完成",
            "total": 0,
            "skipped": len(articles)
        }

    # 创建任务ID
    task_id = str(uuid.uuid4())

    # 启动后台任务
    background_tasks.add_task(
        _process_articles_batch,
        task_id=task_id,
        article_ids=[a.id for a in articles_to_process]
    )

    return {
        "task_id": task_id,
        "total": len(articles_to_process),
        "skipped": len(articles) - len(articles_to_process),
        "status": "started"
    }


async def _process_articles_batch(task_id: str, article_ids: List[str]):
    """后台批量创建持久化知识增强任务。"""
    from app.core.database import get_session_local
    from app.services.knowledge_jobs import enqueue_article_enhancement

    # 初始化进度
    _batch_progress[task_id] = {
        "total": len(article_ids),
        "article_ids": [str(article_id) for article_id in article_ids],
        "processed": 0,
        "queued": 0,
        "failed": 0,
        "skipped": 0,
        "current_article": None,
        "status": "running",
        "start_time": datetime.now().isoformat(),
        "errors": []
    }

    SessionLocal = get_session_local()
    db = SessionLocal()

    try:
        for idx, article_id in enumerate(article_ids):
            try:
                article = db.query(Article).filter(Article.id == article_id).first()
                if not article:
                    _batch_progress[task_id]["skipped"] += 1
                    continue

                # 更新当前处理的文章
                _batch_progress[task_id]["current_article"] = {
                    "id": str(article_id),
                    "title": article.title or f"Article {article_id}",
                    "index": idx + 1
                }

                job_id = enqueue_article_enhancement(
                    article_id=article_id,
                    content=article.content,
                    source_url=article.url,
                    source_published_at=(
                        article.published_at.isoformat() if article.published_at else None
                    ),
                )
                if job_id:
                    _batch_progress[task_id]["queued"] += 1
                else:
                    _batch_progress[task_id]["failed"] += 1
                    _batch_progress[task_id]["errors"].append({
                        "article_id": str(article_id),
                        "error": "知识增强任务入队失败",
                    })

            except Exception as e:
                logger.error(f"处理文章 {article_id} 失败: {e}")
                _batch_progress[task_id]["failed"] += 1
                _batch_progress[task_id]["errors"].append({
                    "article_id": str(article_id),
                    "error": str(e)
                })

        # 入队完成，但实际知识增强仍由持久化 Worker 继续执行。
        _batch_progress[task_id]["status"] = "queued"
        _batch_progress[task_id]["end_time"] = datetime.now().isoformat()
        _batch_progress[task_id]["current_article"] = None

    except Exception as e:
        logger.error(f"批量处理任务 {task_id} 失败: {e}")
        _batch_progress[task_id]["status"] = "failed"
        _batch_progress[task_id]["error"] = str(e)
    finally:
        db.close()


@router.get("/self-enhancement/batch-status/{task_id}")
async def get_batch_process_status(task_id: str, db: Session = Depends(get_db)):
    """
    获取批量处理任务状态

    参数：
    - task_id: 任务ID

    返回：
    - task_id: 任务ID
    - status: 任务状态
    - total: 总文章数
    - processed: 已处理数
    - failed: 失败数
    - current_article: 当前处理的文章
    - errors: 错误列表
    """
    if task_id not in _batch_progress:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在"
        )

    progress = _batch_progress[task_id]

    # 查询持久化知识增强任务，返回真实处理进度，而不是仅统计入队进度。
    total = progress["total"]
    article_ids = progress.get("article_ids", [])
    processed = 0
    queued = 0
    failed = progress.get("failed", 0)
    if article_ids:
        job_rows = db.query(KnowledgeJob.status, func.count(KnowledgeJob.id)).filter(
            KnowledgeJob.job_type == "article_enhancement",
            KnowledgeJob.target_id.in_(article_ids),
        ).group_by(KnowledgeJob.status).all()
        status_counts = {job_status: count for job_status, count in job_rows}
        processed = status_counts.get("completed", 0)
        queued = status_counts.get("pending", 0) + status_counts.get("processing", 0)
        failed += status_counts.get("failed", 0) + status_counts.get("rejected", 0)

    completed = min(total, processed + failed)
    percentage = round((completed / total) * 100, 1) if total > 0 else 100
    current_status = "completed" if completed >= total else "running"

    return {
        "task_id": task_id,
        "status": current_status,
        "total": total,
        "processed": processed,
        "queued": queued,
        "failed": failed,
        "skipped": progress["skipped"],
        "percentage": percentage,
        "current_article": progress["current_article"],
        "start_time": progress["start_time"],
        "end_time": progress.get("end_time"),
        "errors": progress["errors"][:10],  # 只返回前10个错误
        "error_count": len(progress["errors"])
    }


@router.post("/self-enhancement/auto-detect-pending")
async def auto_detect_pending_articles(db: Session = Depends(get_db)):
    """
    自动检测并返回待处理的文章列表

    返回：
    - pending_articles: 待处理文章列表
    - total: 待处理总数
    - already_processed: 已处理数
    """
    from app.models.article import Article, Category

    # 查询所有完成的文章
    all_articles = db.query(Article).filter(
        Article.status.in_(["completed", "success"])
    ).order_by(Article.scraped_at.desc()).all()

    pending_articles = []
    already_processed = []

    enhancement_status = _enhancement_statuses(db, all_articles)
    for article in all_articles:
        # A document is ready for insight only after its current-version
        # enhancement job completed, not merely after generic KG extraction.
        current_enhancement_status = enhancement_status.get(str(article.id), "not_started")
        is_processed = current_enhancement_status == "completed"

        article_data = {
            "id": article.id,
            "title": article.title,
            "summary": article.summary[:200] if article.summary else "",
            "word_count": article.word_count,
            "kg_status": article.kg_status,
            "enhancement_status": current_enhancement_status,
            "category_name": article.category.name if article.category else None,
            "is_processed": is_processed,
        }

        if is_processed:
            already_processed.append(article_data)
        else:
            pending_articles.append(article_data)

    return {
        "pending_articles": pending_articles,
        "total": len(all_articles),
        "pending_count": len(pending_articles),
        "processed_count": len(already_processed),
    }


# ==================== 提示词模板 API ====================

@router.get("/self-enhancement/templates")
async def list_prompt_templates(
    category: Optional[str] = Query(None, description="按分类筛选")
):
    """
    获取可用的提示词模板列表

    参数：
    - category: 可选，按分类筛选 (knowledge_mining/prediction)

    返回：
    - templates: 模板列表
    - total: 总数
    """
    service = await _get_self_enhancement_service()
    templates = service.list_prompt_templates(category)

    return {
        "templates": templates,
        "total": len(templates),
    }


@router.get("/self-enhancement/templates/{template_id}")
async def get_prompt_template(template_id: str):
    """
    获取指定的提示词模板

    参数：
    - template_id: 模板 ID

    返回：
    - 模板详细信息
    """
    service = await _get_self_enhancement_service()
    template = service.get_prompt_template(template_id)

    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}"
        )

    return template


# ==================== 趋势预测 API ====================

@router.get("/prediction/discover-events")
async def discover_prediction_events(
    limit: int = Query(20, ge=1, le=100),
    days: int = Query(90, ge=1, le=3650),
    db: Session = Depends(get_db),
):
    """从近期文章主动发现候选事件；证据不足时如实返回单源信号。"""
    from app.services.kg.event_discovery import EventDiscoveryService

    candidates = EventDiscoveryService().discover(db, limit=max(5, limit), days=days)
    return {"events": candidates, "total": len(candidates)}

class TrendPredictionRequest(BaseModel):
    """趋势预测请求"""
    topic: str = Field(..., description="预测主题")
    time_range: int = Field(30, description="预测天数")
    prediction_type: str = Field("general", description="预测类型")


class DiscoveredEventPredictionRequest(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=100)
    time_range: int = Field(30, ge=1, le=365)
    prediction_type: str = Field("general")
    model_id: Optional[str] = Field(None, min_length=1, max_length=200)
    article_ids: Optional[List[str]] = Field(None, min_length=2, max_length=50)


class PredictionFeedbackRequest(BaseModel):
    actual_trend: Literal["up", "down", "stable"]
    feedback: Optional[str] = None
    accuracy_score: Optional[float] = Field(None, ge=0, le=1)


class InsightAlertReviewRequest(BaseModel):
    status: Literal["acknowledged", "dismissed"]
    review_note: Optional[str] = Field(None, max_length=2000)


def _serialize_insight_alert(alert: InsightAlert) -> Dict[str, Any]:
    return {
        "id": alert.id,
        "event_id": alert.event_id,
        "topic": alert.topic,
        "title": alert.title,
        "severity": alert.severity,
        "status": alert.status,
        "confidence": alert.confidence,
        "independent_source_count": alert.independent_source_count,
        "evidence_article_ids": alert.evidence_article_ids or [],
        "signal_reasons": alert.signal_reasons or [],
        "risk_assessment": alert.risk_assessment or {},
        "match_evidence": alert.match_evidence or {},
        "review_note": alert.review_note,
        "first_seen_at": alert.first_seen_at.isoformat() if alert.first_seen_at else None,
        "last_seen_at": alert.last_seen_at.isoformat() if alert.last_seen_at else None,
        "reviewed_at": alert.reviewed_at.isoformat() if alert.reviewed_at else None,
    }


@router.get("/insight-alerts")
async def list_insight_alerts(
    alert_status: Optional[str] = Query("open", alias="status"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = db.query(InsightAlert)
    if alert_status:
        query = query.filter(InsightAlert.status == alert_status)
    alerts = query.order_by(InsightAlert.last_seen_at.desc()).limit(limit).all()
    return {"alerts": [_serialize_insight_alert(alert) for alert in alerts], "total": len(alerts)}


@router.post("/insight-alerts/scan")
async def scan_insight_alerts_now():
    from app.services.insight_alerts import scan_insight_alerts
    return scan_insight_alerts()


@router.post("/insight-alerts/{alert_id}/review")
async def review_insight_alert(
    alert_id: str,
    request: InsightAlertReviewRequest,
    db: Session = Depends(get_db),
):
    alert = db.query(InsightAlert).filter(InsightAlert.id == alert_id).first()
    if alert is None:
        raise HTTPException(status_code=404, detail="预警不存在")
    alert.status = request.status
    alert.review_note = request.review_note
    alert.reviewed_at = datetime.utcnow()
    db.commit()
    db.refresh(alert)
    return _serialize_insight_alert(alert)


def _save_prediction_record(db: Session, request: TrendPredictionRequest, result) -> str:
    event_id = (result.knowledge_basis or {}).get("event_id")
    if event_id:
        recent_records = (
            db.query(PredictionRecord)
            .filter(PredictionRecord.status == "pending")
            .order_by(PredictionRecord.created_at.desc())
            .limit(100)
            .all()
        )
        for existing in recent_records:
            if (existing.knowledge_basis or {}).get("event_id") == event_id:
                existing.topic = result.topic
                existing.trend = result.trend
                existing.confidence = result.confidence
                existing.factors = result.factors
                existing.timeline = result.timeline
                existing.knowledge_basis = result.knowledge_basis
                existing.interpretation = result.interpretation
                existing.time_range = request.time_range
                db.commit()
                return existing.id
    prediction_id = f"pred-{uuid.uuid4().hex}"
    db.add(PredictionRecord(
        id=prediction_id,
        topic=result.topic,
        prediction_type=result.prediction_type,
        time_range=request.time_range,
        trend=result.trend,
        confidence=result.confidence,
        factors=result.factors,
        timeline=result.timeline,
        knowledge_basis=result.knowledge_basis,
        interpretation=result.interpretation,
    ))
    db.commit()
    return prediction_id


@router.post("/prediction/trend")
async def predict_trend(request: TrendPredictionRequest, db: Session = Depends(get_db)):
    """
    预测趋势

    参数：
    - topic: 预测主题
    - time_range: 预测天数 (默认 30)
    - prediction_type: 预测类型 (general/technology/sentiment)

    返回：
    - trend: 趋势方向 (up/down/stable)
    - confidence: 置信度
    - factors: 影响因素
    - timeline: 时间线预测
    """
    from app.services.kg.prediction import TrendPredictionEngine

    neo4j = get_neo4j_service()
    engine = TrendPredictionEngine(kg_service=neo4j)

    result = await engine.predict_trend(
        topic=request.topic,
        time_range=request.time_range,
        prediction_type=request.prediction_type
    )
    prediction_id = _save_prediction_record(db, request, result)

    return {
        "topic": result.topic,
        "trend": result.trend,
        "confidence": result.confidence,
        "factors": result.factors,
        "timeline": result.timeline,
        "prediction_type": result.prediction_type,
        "generated_at": result.generated_at,
        "knowledge_basis": result.knowledge_basis,
        "prediction_id": prediction_id,
    }


@router.post("/prediction/sentiment")
async def predict_sentiment(request: TrendPredictionRequest, db: Session = Depends(get_db)):
    """预测舆情趋势"""
    from app.services.kg.prediction import TrendPredictionEngine

    neo4j = get_neo4j_service()
    engine = TrendPredictionEngine(kg_service=neo4j)

    result = await engine.predict_sentiment(topic=request.topic)
    prediction_id = _save_prediction_record(db, request, result)

    return {
        "topic": result.topic,
        "trend": result.trend,
        "confidence": result.confidence,
        "factors": result.factors,
        "timeline": result.timeline,
        "prediction_type": result.prediction_type,
        "generated_at": result.generated_at,
        "knowledge_basis": result.knowledge_basis,
        "prediction_id": prediction_id,
    }


@router.post("/prediction/technology")
async def predict_technology(request: TrendPredictionRequest, db: Session = Depends(get_db)):
    """预测技术趋势"""
    from app.services.kg.prediction import TrendPredictionEngine

    neo4j = get_neo4j_service()
    engine = TrendPredictionEngine(kg_service=neo4j)

    result = await engine.predict_technology(topic=request.topic)
    prediction_id = _save_prediction_record(db, request, result)

    return {
        "topic": result.topic,
        "trend": result.trend,
        "confidence": result.confidence,
        "factors": result.factors,
        "timeline": result.timeline,
        "prediction_type": result.prediction_type,
        "generated_at": result.generated_at,
        "knowledge_basis": result.knowledge_basis,
        "prediction_id": prediction_id,
        "scenarios": result.scenarios,
    }


@router.post("/prediction/discovered-event")
async def predict_discovered_event(
    request: DiscoveredEventPredictionRequest,
    db: Session = Depends(get_db),
):
    """对系统发现的候选事件执行多文档交叉预测。"""
    from app.services.kg.event_discovery import EventDiscoveryService
    from app.services.kg.prediction import TrendPredictionEngine
    from app.core.llm import llm_service

    # Keep the lookup window aligned with the discovery page so the event id
    # and evidence set reviewed by the user remain reproducible.
    events = EventDiscoveryService().discover(db, limit=100, days=90)
    event = next((item for item in events if item["id"] == request.event_id), None)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="候选事件已过期，请刷新候选事件")
    original_evidence = event.get("evidence_articles") or []
    original_ids = [str(item.get("id")) for item in original_evidence if item.get("id")]
    selected_ids = list(dict.fromkeys(request.article_ids or original_ids))
    unknown_ids = sorted(set(selected_ids).difference(original_ids))
    if unknown_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="审核材料中包含不属于当前候选事件的文章",
        )
    syndicated_ids = {
        str(item.get("id")) for item in original_evidence
        if item.get("id") and item.get("article_role") == "syndication"
    }
    if syndicated_ids.intersection(selected_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="转载材料仅用于传播溯源，不能作为交叉推演证据",
        )
    selected_evidence = [item for item in original_evidence if str(item.get("id")) in selected_ids]
    if len(selected_evidence) < 2:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="该候选事件只有单篇证据，暂不进行交叉预测")
    selected_sources = {str(item.get("source_domain") or "") for item in selected_evidence}
    selected_sources.discard("")
    if len(selected_sources) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="交叉推演至少需要两篇来自不同独立来源的材料",
        )
    reviewed_event = {**event, "evidence_articles": selected_evidence}
    evidence_ids = [item.get("id") for item in selected_evidence if item.get("id")]
    article_rows = db.query(Article).filter(Article.id.in_(evidence_ids)).all() if evidence_ids else []
    articles_by_id = {str(article.id): article for article in article_rows}
    analysis_event = {
        **reviewed_event,
        "evidence_articles": [
            {
                **item,
                "analysis_text": (
                    articles_by_id[str(item.get("id"))].content
                    or articles_by_id[str(item.get("id"))].summary
                    or ""
                )[:4000] if str(item.get("id")) in articles_by_id else item.get("summary", ""),
            }
            for item in selected_evidence
        ],
    }
    neo4j = get_neo4j_service()
    try:
        result = await TrendPredictionEngine(neo4j, llm_client=llm_service).predict_discovered_event(
            event=analysis_event,
            time_range=request.time_range,
            prediction_type=request.prediction_type,
            model_id=request.model_id,
        )
        result.knowledge_basis = result.knowledge_basis or {}
        result.knowledge_basis.update({
            "evidence_article_ids": selected_ids,
            "excluded_article_ids": [item_id for item_id in original_ids if item_id not in selected_ids],
            "user_reviewed": request.article_ids is not None,
            "reviewed_at": datetime.utcnow().isoformat(),
            "time_range": request.time_range,
        })
        record_request = TrendPredictionRequest(
            topic=result.topic,
            time_range=request.time_range,
            prediction_type=request.prediction_type,
        )
        prediction_id = _save_prediction_record(db, record_request, result)
        return {
            "topic": result.topic,
            "trend": result.trend,
            "confidence": result.confidence,
            "factors": result.factors,
            "timeline": result.timeline,
            "scenarios": result.scenarios,
            "prediction_type": result.prediction_type,
            "generated_at": result.generated_at,
            "knowledge_basis": result.knowledge_basis,
            "interpretation": result.interpretation,
            "prediction_id": prediction_id,
            "event": reviewed_event,
        }
    finally:
        await neo4j.close()


@router.get("/prediction/history")
async def list_prediction_history(
    topic: Optional[str] = Query(None),
    prediction_status: Optional[str] = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """查询预测快照，供回放和命中评估使用。"""
    query = db.query(PredictionRecord)
    if topic:
        query = query.filter(PredictionRecord.topic.ilike(f"%{topic}%"))
    if prediction_status:
        query = query.filter(PredictionRecord.status == prediction_status)
    records = query.order_by(PredictionRecord.created_at.desc()).limit(limit).all()
    return {"predictions": [_serialize_prediction_record(record) for record in records], "total": len(records)}


def _serialize_prediction_record(record: PredictionRecord) -> Dict[str, Any]:
    return {
        "id": record.id,
        "topic": record.topic,
        "prediction_type": record.prediction_type,
        "time_range": record.time_range,
        "trend": record.trend,
        "confidence": record.confidence,
        "factors": record.factors or [],
        "timeline": record.timeline or [],
        "knowledge_basis": record.knowledge_basis or {},
        "interpretation": record.interpretation or {},
        "status": record.status,
        "feedback": record.feedback,
        "actual_trend": record.actual_trend,
        "accuracy_score": record.accuracy_score,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "evaluated_at": record.evaluated_at.isoformat() if record.evaluated_at else None,
    }


@router.post("/prediction/history/{prediction_id}/feedback")
async def review_prediction(
    prediction_id: str,
    request: PredictionFeedbackRequest,
    db: Session = Depends(get_db),
):
    """记录人工观察结果，形成预测反馈闭环。"""
    record = db.query(PredictionRecord).filter(PredictionRecord.id == prediction_id).first()
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="预测记录不存在")
    record.actual_trend = request.actual_trend
    record.feedback = request.feedback
    record.accuracy_score = request.accuracy_score if request.accuracy_score is not None else (
        1.0 if record.trend == request.actual_trend else 0.0
    )
    record.status = "evaluated"
    record.evaluated_at = datetime.utcnow()
    db.commit()
    db.refresh(record)
    return _serialize_prediction_record(record)


@router.get("/prediction/metrics")
async def get_prediction_metrics(
    topic: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """汇总预测反馈，用于置信度校准和主题决策。"""
    query = db.query(PredictionRecord).filter(PredictionRecord.status == "evaluated")
    if topic:
        query = query.filter(PredictionRecord.topic.ilike(f"%{topic}%"))
    records = query.order_by(PredictionRecord.evaluated_at.desc()).limit(1000).all()

    def summarize(items):
        if not items:
            return {"evaluated": 0, "accuracy": None, "average_confidence": None}
        return {
            "evaluated": len(items),
            "accuracy": round(sum((item.accuracy_score or 0) for item in items) / len(items), 4),
            "average_confidence": round(sum(item.confidence or 0 for item in items) / len(items), 4),
        }

    by_topic = {}
    by_type = {}
    for record in records:
        by_topic.setdefault(record.topic, []).append(record)
        by_type.setdefault(record.prediction_type, []).append(record)
    return {
        "overall": summarize(records),
        "by_topic": {key: summarize(value) for key, value in by_topic.items()},
        "by_type": {key: summarize(value) for key, value in by_type.items()},
    }
