"""
数据库配置模块

支持 PostgreSQL 和 SQLite 数据库连接管理
当 PostgreSQL 不可用时自动回退到 SQLite
"""
import os
import json
import logging
from pathlib import Path
from typing import Generator, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session, declarative_base
from sqlalchemy.pool import QueuePool

logger = logging.getLogger("ai-studio")

# 配置数据文件路径
CONFIG_DIR = Path(__file__).parent.parent.parent
SETTINGS_FILE = CONFIG_DIR / "data" / "settings.json"
SQLITE_DB_PATH = CONFIG_DIR / "data" / "ai_studio.db"

# 无论从 Windows、Linux、Docker 还是 IDE 启动，都使用 backend/.env。
# 已由部署平台注入的环境变量优先，不会被文件覆盖。
load_dotenv(CONFIG_DIR / ".env", override=False)


class DatabaseConfig:
    """数据库配置类"""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        database: str = "ai_studio",
        user: str = "postgres",
        password: str = "",
    ):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password

    @property
    def url(self) -> str:
        """获取数据库连接 URL"""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        """从环境变量加载配置"""
        return cls(
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "5432")),
            database=os.getenv("DB_NAME", "ai_studio"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", ""),
        )

    @classmethod
    def from_settings(cls) -> "DatabaseConfig":
        """从 settings.json 加载配置"""
        config = cls()
        if SETTINGS_FILE.exists():
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    db_config = data.get("settings", {}).get("database", {})
                    if db_config:
                        config.host = db_config.get("host", config.host)
                        config.port = db_config.get("port", config.port)
                        config.database = db_config.get("database", config.database)
                        config.user = db_config.get("user", config.user)
                        config.password = db_config.get("password", config.password)
            except Exception as e:
                logger.warning(f"加载数据库配置失败: {e}")
        return config


def get_database_config() -> DatabaseConfig:
    """
    获取数据库配置
    优先级：环境变量 > settings.json > 默认值
    """
    # 优先使用环境变量
    if os.getenv("DATABASE_URL"):
        url = os.getenv("DATABASE_URL")
        parsed = urlparse(url)
        return DatabaseConfig(
            host=parsed.hostname or "localhost",
            port=parsed.port or 5432,
            database=parsed.path.lstrip("/") if parsed.path else "ai_studio",
            user=parsed.username or "postgres",
            password=parsed.password or "",
        )

    # 使用环境变量 (DB_HOST, DB_PORT 等)
    if os.getenv("DB_HOST") or os.getenv("DB_USER") or os.getenv("DB_NAME"):
        return DatabaseConfig.from_env()

    # 从 settings.json 加载
    return DatabaseConfig.from_settings()


# SQLAlchemy 基类
Base = declarative_base()


# 全局引擎和会话工厂
_engine = None
_SessionLocal = None


def _test_postgresql_connection(url: str) -> bool:
    """测试 PostgreSQL 连接是否可用"""
    try:
        test_engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
        with test_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        test_engine.dispose()
        return True
    except Exception:
        return False


def get_engine():
    """获取数据库引擎（延迟初始化）

    DB_TYPE 环境变量行为:
      - "postgresql" : 强制使用 PostgreSQL，连不上直接报错，绝不回退 SQLite
      - "sqlite"     : 强制使用 SQLite
      - 未设置       : 优先 PostgreSQL，连接失败回退 SQLite（向后兼容）
    """
    global _engine
    if _engine is None:
        db_type = os.getenv("DB_TYPE", "").lower()

        if db_type != "sqlite":
            config = get_database_config()
            if _test_postgresql_connection(config.url):
                _engine = create_engine(
                    config.url,
                    poolclass=QueuePool,
                    pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
                    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
                    pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "60")),
                    pool_pre_ping=True,
                    echo=os.getenv("SQL_ECHO", "false").lower() == "true",
                )
                logger.info(f"✅ 使用 PostgreSQL: {config.host}:{config.port}/{config.database}")
                return _engine
            elif db_type == "postgresql":
                # 明确要求 PostgreSQL 但连不上 → 直接报错，不静默回退
                raise RuntimeError(
                    f"DB_TYPE=postgresql 但无法连接 PostgreSQL ({config.host}:{config.port}/{config.database})。"
                    "请检查 PostgreSQL 是否启动、端口和密码是否正确。"
                )
            else:
                logger.warning("⚠️ PostgreSQL 不可用，回退到 SQLite")

        # 使用 SQLite
        SQLITE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        sqlite_url = f"sqlite:///{SQLITE_DB_PATH}"
        _engine = create_engine(
            sqlite_url,
            echo=os.getenv("SQL_ECHO", "false").lower() == "true",
            connect_args={"check_same_thread": False},  # SQLite 需要
        )
        logger.info(f"✅ 使用 SQLite: {SQLITE_DB_PATH}")
    return _engine


def get_session_local():
    """获取会话工厂（延迟初始化）"""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=get_engine()
        )
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    """
    获取数据库会话的生成器
    用作 FastAPI 依赖注入
    """
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    初始化数据库（创建所有表）
    """
    # 导入所有模型以确保它们被注册
    from app.models import (
        Article, Category, ScrapeSource, Keyword, ArticleKeyword, ArticleLink,
        ScheduledTask, ScrapeHistory, KnowledgeJob, KnowledgeSynthesis, PredictionRecord, InsightAlert,
        ChatSession, ChatMessage
    )

    engine = get_engine()

    # 创建所有表
    Base.metadata.create_all(bind=engine)
    _ensure_wechat_account_columns(engine)
    _ensure_knowledge_job_columns(engine)
    _ensure_prediction_columns(engine)
    logger.info("数据库表已创建/更新")

    # 创建全文搜索索引（如果不存在）
    _create_fts_index(engine)


def _ensure_knowledge_job_columns(engine) -> None:
    """为已存在的任务表补齐异步任务载荷字段。"""
    if engine.url.drivername == "sqlite":
        return  # SQLite 表结构由 create_all 处理
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE knowledge_jobs ADD COLUMN IF NOT EXISTS payload JSONB"
        ))
        connection.execute(text(
            "ALTER TABLE knowledge_jobs ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMP NULL"
        ))


def _ensure_prediction_columns(engine) -> None:
    """Keep prediction snapshots compatible with databases created before interpretation existed."""
    if engine.url.drivername == "sqlite":
        return
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE prediction_records ADD COLUMN IF NOT EXISTS interpretation JSONB"
        ))


def _ensure_wechat_account_columns(engine) -> None:
    """为已有数据库补齐公众号低频发现所需字段。"""
    if engine.url.drivername == "sqlite":
        return  # SQLite 表结构由 create_all 处理
    statements = (
        "ALTER TABLE wechat_accounts ADD COLUMN IF NOT EXISTS fakeid VARCHAR(200)",
        "ALTER TABLE wechat_accounts ADD COLUMN IF NOT EXISTS min_crawl_interval_minutes INTEGER DEFAULT 60",
        "ALTER TABLE wechat_accounts ADD COLUMN IF NOT EXISTS last_discovery_at TIMESTAMP NULL",
        "ALTER TABLE wechat_accounts ADD COLUMN IF NOT EXISTS next_discovery_at TIMESTAMP NULL",
        "ALTER TABLE wechat_accounts ADD COLUMN IF NOT EXISTS last_discovery_status VARCHAR(50) NULL",
        "ALTER TABLE wechat_accounts ADD COLUMN IF NOT EXISTS rate_limit_count INTEGER DEFAULT 0",
        "ALTER TABLE wechat_accounts ADD COLUMN IF NOT EXISTS discovery_cache TEXT NULL",
        "ALTER TABLE wechat_cookies ADD COLUMN IF NOT EXISTS last_discovery_at TIMESTAMP NULL",
        "ALTER TABLE wechat_cookies ADD COLUMN IF NOT EXISTS next_discovery_at TIMESTAMP NULL",
        "ALTER TABLE wechat_cookies ADD COLUMN IF NOT EXISTS last_discovery_status VARCHAR(50) NULL",
        "ALTER TABLE wechat_cookies ADD COLUMN IF NOT EXISTS rate_limit_count INTEGER DEFAULT 0",
        """UPDATE wechat_cookies
           SET last_discovery_at = (SELECT MAX(last_discovery_at) FROM wechat_accounts),
               next_discovery_at = (SELECT MAX(next_discovery_at) FROM wechat_accounts),
               last_discovery_status = CASE
                   WHEN EXISTS (SELECT 1 FROM wechat_accounts WHERE last_discovery_status = 'rate_limited')
                   THEN 'rate_limited' ELSE last_discovery_status END,
               rate_limit_count = GREATEST(
                   COALESCE(rate_limit_count, 0),
                   COALESCE((SELECT MAX(rate_limit_count) FROM wechat_accounts), 0)
               )
           WHERE is_active = TRUE AND next_discovery_at IS NULL""",
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def sync_settings_to_database(categories: dict, scrape_sources: dict) -> dict:
    """将文件配置幂等同步到关系数据库，供启动和配置变更时调用。"""
    from app.models.article import Category, ScrapeSource

    SessionLocal = get_session_local()
    db = SessionLocal()
    synced_categories = 0
    synced_sources = 0
    try:
        for category_id, data in categories.items():
            category = db.query(Category).filter(Category.id == category_id).first()
            if category is None:
                category = Category(id=category_id)
                db.add(category)
                synced_categories += 1
            category.name = data.get("name") or category_id
            category.color = data.get("color") or "#6B7280"
            category.description = data.get("description") or ""
            category.folder_name = data.get("folder_name") or category_id

        # 来源外键依赖分类，必须先 flush 分类。
        db.flush()

        for source_id, data in scrape_sources.items():
            source = db.query(ScrapeSource).filter(ScrapeSource.id == source_id).first()
            if source is None:
                source = ScrapeSource(id=source_id)
                db.add(source)
                synced_sources += 1
            source.name = data.get("name") or source_id
            source.url = data.get("url") or ""
            category_id = data.get("category")
            source.category_id = category_id if category_id in categories else None
            source.description = data.get("description")
            source.is_enabled = data.get("is_enabled", True)

        db.commit()
        result = {"categories": synced_categories, "scrape_sources": synced_sources}
        logger.info(f"配置同步数据库完成: {result}")
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def load_scrape_sources_from_database() -> dict:
    """从数据库读取所有爬取源，返回 {name: {...}}。

    数据库持久化了用户界面添加的爬取源；当 settings.json 被覆盖导致数据缺失时，
    启动阶段以数据库为权威来源恢复用户配置。同名记录按名称去重，优先保留启用、
    其次保留创建更早的一条，避免内置示例重复累积。
    """
    from app.models.article import ScrapeSource

    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        sources = db.query(ScrapeSource).order_by(ScrapeSource.created_at.asc()).all()
        by_name: dict = {}
        for s in sources:
            entry = {
                "id": s.id,
                "name": s.name,
                "url": s.url,
                "category": s.category_id or "business",
                "description": s.description,
                "is_enabled": s.is_enabled,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            if s.name not in by_name:
                by_name[s.name] = entry
                continue
            existing = by_name[s.name]
            # 已启用则保留，否则用当前启用的一条替换
            if not existing["is_enabled"] and entry["is_enabled"]:
                by_name[s.name] = entry
        return by_name
    except Exception as e:
        logger.warning(f"从数据库读取爬取源失败: {e}")
        return {}
    finally:
        db.close()


def _create_fts_index(engine):
    """创建全文搜索索引（仅 PostgreSQL）"""
    from sqlalchemy import text

    # SQLite 不支持 PostgreSQL 的全文搜索功能
    if engine.url.drivername == "sqlite":
        logger.info("SQLite 数据库，跳过全文搜索索引创建")
        return

    with engine.connect() as conn:
        # 检查 search_vector 列是否存在
        result = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'articles' AND column_name = 'search_vector'
        """))

        if not result.fetchone():
            logger.info("正在创建全文搜索列和索引...")
            # 新 PostgreSQL 实例通常没有项目自定义的 chinese 配置。
            conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'chinese') THEN
                        CREATE TEXT SEARCH CONFIGURATION chinese (COPY = simple);
                    END IF;
                END
                $$;
            """))
            # 添加 search_vector 列
            conn.execute(text("""
                ALTER TABLE articles
                ADD COLUMN IF NOT EXISTS search_vector tsvector
            """))

            # 创建 GIN 索引
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_articles_search
                ON articles USING GIN(search_vector)
            """))

            # 创建触发器函数
            conn.execute(text("""
                CREATE OR REPLACE FUNCTION articles_search_trigger()
                RETURNS TRIGGER AS $$
                BEGIN
                    NEW.search_vector :=
                        setweight(to_tsvector('chinese', COALESCE(NEW.title, '')), 'A') ||
                        setweight(to_tsvector('chinese', COALESCE(NEW.summary, '')), 'B') ||
                        setweight(to_tsvector('chinese', COALESCE(NEW.content, '')), 'C');
                    RETURN NEW;
                END
                $$ LANGUAGE plpgsql;
            """))

            # 创建触发器
            conn.execute(text("""
                DROP TRIGGER IF EXISTS articles_search_update ON articles;
                CREATE TRIGGER articles_search_update
                BEFORE INSERT OR UPDATE ON articles
                FOR EACH ROW EXECUTE FUNCTION articles_search_trigger()
            """))

            # 更新现有记录
            conn.execute(text("""
                UPDATE articles SET search_vector =
                    setweight(to_tsvector('chinese', COALESCE(title, '')), 'A') ||
                    setweight(to_tsvector('chinese', COALESCE(summary, '')), 'B') ||
                    setweight(to_tsvector('chinese', COALESCE(content, '')), 'C')
            """))

            conn.commit()
            logger.info("全文搜索索引创建完成")


def check_db_connection() -> bool:
    """检查数据库连接是否正常"""
    from sqlalchemy import text
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"数据库连接失败: {e}")
        return False
