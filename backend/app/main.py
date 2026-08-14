from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging

from app.api import chat, models, settings, scrape, firecrawl, articles, scheduled, kg, wechat

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-studio")

# 调度器实例
_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global _scheduler

    # 启动时
    logger.info("应用启动中...")

    # ============ 配置一致性检测 ============
    from app.core.config import check_config_consistency
    config_ok = check_config_consistency()
    if not config_ok:
        logger.warning("⚠️ 配置检测发现问题，请检查日志")

    try:
        # 每次启动都幂等创建表并同步文件配置，避免新设备/容器漏跑迁移脚本。
        from app.core.database import init_db, sync_settings_to_database
        init_db()
        sync_settings_to_database(
            settings.settings_store.categories,
            settings.settings_store.scrape_sources,
        )

        from app.core.scheduler import (
            init_scheduler,
            register_kg_reconcile_job,
            register_knowledge_job_worker,
            register_insight_alert_monitor,
        )
        _scheduler = init_scheduler()
        logger.info("定时任务调度器已启动")

        # 注册 KG 定时对账(默认关闭,从环境变量 KG_RECONCILE_INTERVAL_MINUTES 读取)
        import os
        # 默认每 10 分钟对账，迁移设备后 Neo4j 数据卷缺失也能自动补抽。
        interval = int(os.getenv("KG_RECONCILE_INTERVAL_MINUTES", "10"))
        register_kg_reconcile_job(_scheduler, interval_minutes=interval)
        register_knowledge_job_worker(_scheduler)
        register_insight_alert_monitor(_scheduler)

        # 幂等初始化图谱约束、Claim 索引和现有实体的稳定标识。
        from app.services.kg import Neo4jService
        neo4j = Neo4jService()
        try:
            await neo4j.init_schema()
        except Exception as e:
            logger.error(f"启动时初始化知识图谱模式失败: {e}")
        finally:
            await neo4j.close()

        # 启动时自动处理 kg_status in (NULL, 'pending') 的老文章
        from app.core.database import get_session_local
        from app.services.kg_sync import (
            process_pending_articles,
            recover_interrupted_articles,
        )
        SessionLocal = get_session_local()
        session = SessionLocal()
        try:
            recovered = recover_interrupted_articles(session)
            if recovered:
                logger.warning(f"恢复 {recovered} 篇被后端重启中断的KG任务")
            result = await process_pending_articles(session)
            if result["scanned"] > 0:
                logger.warning(
                    f"⚠️  启动时发现 {result['scanned']} 篇待抽取文章,已排入后台队列({result['scheduled']} 个并发任务)"
                )
        except Exception as e:
            logger.error(f"启动时 process_pending_articles 失败: {e}")
        finally:
            session.close()
    except Exception as e:
        logger.error(f"启动调度器失败: {e}")

    yield

    # 关闭时
    logger.info("应用关闭中...")
    if _scheduler:
        try:
            from app.core.scheduler import shutdown_scheduler
            shutdown_scheduler()
        except Exception as e:
            logger.error(f"关闭调度器失败: {e}")


app = FastAPI(
    title="AI Studio API",
    description="AI Studio 工作台后端 API",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS 配置 - 允许所有前端访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(chat.router)
app.include_router(models.router)
app.include_router(settings.router)
app.include_router(scrape.router)
app.include_router(firecrawl.router)
app.include_router(articles.router)
app.include_router(scheduled.router)
app.include_router(kg.router)
app.include_router(wechat.router)


@app.get("/")
async def root():
    """API 根路径"""
    return {"message": "AI Studio API", "version": "1.0.0"}


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "healthy"}


@app.get("/health/db")
async def health_db():
    """数据库健康检查"""
    from app.core.database import check_db_connection, get_engine, SQLITE_DB_PATH

    # 获取引擎（会触发初始化）
    engine = get_engine()
    is_connected = check_db_connection()

    # 判断数据库类型
    if engine.url.drivername == "sqlite":
        db_info = {
            "type": "SQLite",
            "path": str(SQLITE_DB_PATH),
            "exists": SQLITE_DB_PATH.exists(),
        }
    else:
        from app.core.database import get_database_config
        config = get_database_config()
        db_info = {
            "type": "PostgreSQL",
            "host": config.host,
            "port": config.port,
            "database": config.database,
        }

    return {
        "status": "healthy" if is_connected else "unhealthy",
        "database": {
            "connected": is_connected,
            **db_info
        }
    }


@app.post("/init-db")
async def init_database():
    """初始化数据库（创建表和全文搜索索引）"""
    from app.core.database import init_db

    try:
        init_db()
        return {"status": "success", "message": "数据库初始化完成"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/scheduler/sync")
async def sync_scheduler():
    """手动同步调度器任务"""
    global _scheduler
    if _scheduler:
        try:
            from app.core.scheduler import sync_scheduler_tasks
            sync_scheduler_tasks(_scheduler)
            return {"status": "success", "message": "调度器已同步"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "error", "message": "调度器未运行"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
