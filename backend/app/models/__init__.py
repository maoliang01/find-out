"""
数据模型包

导出所有 SQLAlchemy 模型
"""

from app.models.article import (
    Article,
    Category,
    ScrapeSource,
    Keyword,
    ArticleKeyword,
    ArticleLink,
)
from app.models.scheduled_task import (
    ScheduledTask,
    ScrapeHistory,
    TaskStatus,
)
from app.models.wechat import (
    WechatAccount,
    WechatCookie,
    WechatCrawlTask,
)
from app.models.knowledge import KnowledgeJob
from app.models.synthesis import KnowledgeSynthesis
from app.models.prediction import PredictionRecord
from app.models.insight_alert import InsightAlert
from app.models.chat import ChatSession, ChatMessage

__all__ = [
    "Article",
    "Category",
    "ScrapeSource",
    "Keyword",
    "ArticleKeyword",
    "ArticleLink",
    "ScheduledTask",
    "ScrapeHistory",
    "TaskStatus",
    "WechatAccount",
    "WechatCookie",
    "WechatCrawlTask",
    "KnowledgeJob",
    "KnowledgeSynthesis",
    "PredictionRecord",
    "InsightAlert",
    "ChatSession",
    "ChatMessage",
]
