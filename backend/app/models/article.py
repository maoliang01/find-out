"""
文章相关的数据模型

包含：分类、爬取源、文章、关键词、链接等表的定义
"""
import hashlib
import uuid
from datetime import datetime, date
from typing import Optional, List, TYPE_CHECKING

from sqlalchemy import (
    Column, String, Text, Integer, Boolean, Date, DateTime,
    ForeignKey, Index, UniqueConstraint, CheckConstraint
)
from sqlalchemy.orm import relationship, Mapped, mapped_column

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.article import Category, ScrapeSource, Keyword, ArticleKeyword, ArticleLink
    from app.models.scheduled_task import ScheduledTask


class Category(Base):
    """分类模型"""

    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    color: Mapped[str] = mapped_column(String(20), default="#6B7280")
    description: Mapped[str] = mapped_column(Text, default="")
    folder_name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # 关联关系
    sources: Mapped[List["ScrapeSource"]] = relationship(
        "ScrapeSource", back_populates="category"
    )
    articles: Mapped[List["Article"]] = relationship("Article", back_populates="category")

    __table_args__ = (
        Index("idx_categories_name", "name"),
    )

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "description": self.description,
            "folder_name": self.folder_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ScrapeSource(Base):
    """爬取源模型"""

    __tablename__ = "scrape_sources"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[Optional[str]] = mapped_column(
        String(50), ForeignKey("categories.id")
    )
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # 关联关系
    category: Mapped[Optional["Category"]] = relationship(
        "Category", back_populates="sources"
    )
    articles: Mapped[List["Article"]] = relationship(
        "Article", back_populates="source"
    )
    scheduled_tasks: Mapped[List["ScheduledTask"]] = relationship(
        "ScheduledTask", back_populates="source"
    )

    __table_args__ = (
        Index("idx_sources_category", "category_id"),
        Index("idx_sources_enabled", "is_enabled"),
    )

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "category_id": self.category_id,
            "description": self.description,
            "is_enabled": self.is_enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Article(Base):
    """文章模型"""

    __tablename__ = "articles"

    # 主键
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # 内容字段
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    html: Mapped[str] = mapped_column(Text, default="")
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[Optional[str]] = mapped_column(String(200))

    # 去重字段
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    # Provenance signals: copies remain searchable, but are not counted as
    # independent evidence by event aggregation and alerting.
    content_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    duplicate_group_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    article_role: Mapped[str] = mapped_column(String(20), default="original", index=True)

    # 关联关系
    source_id: Mapped[Optional[str]] = mapped_column(
        String(50), ForeignKey("scrape_sources.id")
    )
    source_type: Mapped[Optional[str]] = mapped_column(
        String(50), default="web", index=True
    )  # 信源类型: web/wechat
    category_id: Mapped[Optional[str]] = mapped_column(
        String(50), ForeignKey("categories.id")
    )

    # 时间字段
    published_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # 文体（新闻、通知、纪要等）
    style: Mapped[Optional[str]] = mapped_column(String(50))

    # 状态
    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    # 知识图谱同步状态(kg_status: pending / processing / success / failed / skipped)
    kg_status: Mapped[str] = mapped_column(
        String(20), default="pending", index=True
    )
    kg_processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    kg_content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    kg_error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 版本控制
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # 全文搜索向量（由数据库自动维护）
    search_vector = Column(Text, nullable=True)

    # 关联关系
    source: Mapped[Optional["ScrapeSource"]] = relationship(
        "ScrapeSource", back_populates="articles"
    )
    category: Mapped[Optional["Category"]] = relationship(
        "Category", back_populates="articles"
    )
    keywords: Mapped[List["ArticleKeyword"]] = relationship(
        "ArticleKeyword", back_populates="article", cascade="all, delete-orphan"
    )
    links: Mapped[List["ArticleLink"]] = relationship(
        "ArticleLink", back_populates="article", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_articles_url", "url"),
        Index("idx_articles_status", "status"),
        Index("idx_articles_published_at", "published_at"),
        Index("idx_articles_category", "category_id"),
        Index("idx_articles_source", "source_id"),
        Index("idx_articles_source_type", "source_type"),
        Index("idx_articles_scraped_at", "scraped_at"),
        Index("idx_articles_kg_status", "kg_status"),
    )

    def calculate_content_hash(self) -> str:
        """计算内容哈希用于去重"""
        return hashlib.sha256(self.content.encode()).hexdigest()

    def to_dict(self, include_keywords: bool = True) -> dict:
        """转换为字典"""
        result = {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "content": self.content,
            "html": self.html,
            "word_count": self.word_count,
            "author": self.author,
            "summary": self.summary,
            "style": self.style,  # 文体
            "content_hash": self.content_hash,
            "source_id": self.source_id,
            "source_type": self.source_type,  # 信源类型
            "category_id": self.category_id,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "scraped_at": self.scraped_at.isoformat() if self.scraped_at else None,
            "status": self.status,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

        if include_keywords:
            result["keywords"] = [
                ak.keyword.name for ak in self.keywords if ak.keyword
            ]

        return result


class Keyword(Base):
    """关键词模型"""

    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # 关联关系
    articles: Mapped[List["ArticleKeyword"]] = relationship(
        "ArticleKeyword", back_populates="keyword"
    )

    __table_args__ = (
        Index("idx_keywords_name", "name"),
    )

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ArticleKeyword(Base):
    """文章关键词关联表"""

    __tablename__ = "article_keywords"

    article_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("articles.id", ondelete="CASCADE"),
        primary_key=True
    )
    keyword_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("keywords.id", ondelete="CASCADE"),
        primary_key=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=0)

    # 关联关系
    article: Mapped["Article"] = relationship("Article", back_populates="keywords")
    keyword: Mapped["Keyword"] = relationship("Keyword", back_populates="articles")


class ArticleLink(Base):
    """文章链接表"""

    __tablename__ = "article_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_article_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("articles.id", ondelete="CASCADE")
    )
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    link_text: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # 关联关系
    article: Mapped["Article"] = relationship("Article", back_populates="links")

    __table_args__ = (
        Index("idx_links_source", "source_article_id"),
        Index("idx_links_target", "target_url"),
    )

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "source_article_id": self.source_article_id,
            "target_url": self.target_url,
            "link_text": self.link_text,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
