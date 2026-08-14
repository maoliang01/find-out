# -*- coding: utf-8 -*-
"""
微信公众号内容处理管道

将爬取的内容与 LLM 提取、文章保存、知识图谱抽取集成
"""

import logging
import re
from typing import Optional, List, Dict, Any
from datetime import datetime, date
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.article import Article, Keyword, ArticleKeyword
from app.services.wechat.crawler import WechatCrawler, WechatArticle
from app.services.wechat.cookie_manager import CookieManager
from app.services.kg_sync import on_article_sync_only, trigger_async_extraction
from app.services.scraper import get_scraper

logger = logging.getLogger(__name__)


class WechatPipeline:
    """微信公众号内容处理管道"""

    def __init__(self, db: Session):
        self.db = db
        self.cookie_manager = CookieManager(db)
        self.crawler = WechatCrawler(self.cookie_manager)

    async def process_article(
        self,
        url: str,
        extract_tags: bool = True,
        extract_summary: bool = True,
        category_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        处理单篇文章

        流程: 爬取内容 → LLM提取标签/摘要 → 保存文章 → 触发KG抽取

        Args:
            url: 文章 URL
            extract_tags: 是否提取标签
            extract_summary: 是否提取摘要
            category_id: 分类 ID

        Returns:
            处理结果字典
        """
        try:
            # 1. 启动爬虫
            await self.crawler.start()

            # 2. 爬取文章内容
            article = await self.crawler.fetch_article(url)
            if not article:
                return {"success": False, "error": "文章爬取失败"}

            await self._enrich_article(article, extract_tags, extract_summary)

            # 3. 保存文章到数据库
            db_article = await self._save_article(
                article=article,
                category_id=category_id
            )

            if not db_article:
                return {"success": False, "error": "文章保存失败"}

            # 4. 同步 Article metadata 到 Neo4j，并后台触发实体抽取
            try:
                await on_article_sync_only(db_article)
                trigger_async_extraction(db_article.id)
            except Exception as e:
                logger.warning(f"知识图谱同步触发失败: {e}")

            return {
                "success": True,
                "article_id": db_article.id,
                "title": db_article.title,
                "message": "文章处理成功"
            }

        except Exception as e:
            logger.error(f"处理文章失败: {url}, 错误: {e}", exc_info=True)
            error_msg = str(e) or repr(e) or "未知错误"
            return {"success": False, "error": error_msg}
        finally:
            await self.crawler.stop()

    async def process_batch(
        self,
        urls: List[str],
        delay: float = 1.0,
        category_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        批量处理文章

        Args:
            urls: 文章 URL 列表
            delay: 每篇文章之间的延迟
            category_id: 分类 ID

        Returns:
            处理结果列表
        """
        results = []

        try:
            existing_articles = self.db.query(Article).filter(Article.url.in_(urls)).all() if urls else []
            existing_by_url = {item.url: item for item in existing_articles}
            for url, item in existing_by_url.items():
                results.append({
                    "success": True,
                    "article_id": item.id,
                    "title": item.title,
                    "url": url,
                    "cached": True,
                })
            pending_urls = [url for url in urls if url not in existing_by_url]
            if not pending_urls:
                return results

            # 启动爬虫
            await self.crawler.start()

            # 批量爬取
            articles = await self.crawler.fetch_articles_batch(pending_urls, delay)
            fetched_urls = {article.url for article in articles}

            # 逐个处理
            for article in articles:
                try:
                    await self._enrich_article(article, True, True)
                    # 保存文章
                    db_article = await self._save_article(
                        article=article,
                        category_id=category_id
                    )

                    if db_article:
                        results.append({
                            "success": True,
                            "article_id": db_article.id,
                            "title": db_article.title,
                            "url": article.url
                        })
                    else:
                        results.append({
                            "success": False,
                            "error": "文章保存失败",
                            "url": article.url
                        })

                except Exception as e:
                    results.append({
                        "success": False,
                        "error": str(e),
                        "url": article.url
                    })

            # fetch_articles_batch 只返回成功对象；把抓取阶段失败的 URL 也纳入结果，
            # 让调用端能展示真实错误，而不是只有失败数量。
            for url in pending_urls:
                if url not in fetched_urls:
                    results.append({
                        "success": False,
                        "error": self.crawler.errors.get(url, "文章未抓取，未返回具体错误"),
                        "url": url,
                    })

        except Exception as e:
            logger.error(f"批量处理失败: {e}")
        finally:
            await self.crawler.stop()

        return results

    async def _enrich_article(
        self,
        article: WechatArticle,
        extract_tags: bool = True,
        extract_summary: bool = True,
    ) -> None:
        """复用网页爬取的 LLM 元数据提取，生成摘要与关键词。"""
        try:
            metadata = await get_scraper()._extract_metadata_with_llm(
                article.title,
                article.content,
                article.url,
            )
            if not article.author:
                article.author = metadata.get("author", "") or article.source_name
            if not article.publish_time:
                article.publish_time = metadata.get("published_at", "")
            if extract_summary:
                article.summary = metadata.get("summary", "")
            if extract_tags:
                article.tags = metadata.get("keywords", [])[:10]
        except Exception as exc:
            logger.warning("公众号文章元数据增强失败，保留原始元数据: %s", exc)

        if extract_summary and not article.summary:
            plain = re.sub(r"[#*`>\[\]()!]", "", article.content)
            article.summary = re.sub(r"\s+", " ", plain).strip()[:200]

    async def _save_article(
        self,
        article: WechatArticle,
        category_id: Optional[str] = None
    ) -> Optional[Article]:
        """
        保存文章到数据库

        Args:
            article: WechatArticle 对象
            category_id: 分类 ID

        Returns:
            Article 对象
        """
        try:
            # 检查文章是否已存在
            existing = self.db.query(Article).filter(Article.url == article.url).first()
            if existing:
                logger.info(f"文章已存在: {article.title}")
                return existing

            # 计算内容哈希
            import hashlib
            content_hash = hashlib.sha256(article.content.encode()).hexdigest()

            # 解析发布日期
            published_at = None
            if article.publish_time:
                try:
                    # 尝试多种日期格式
                    for fmt in ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y年%m月%d日"]:
                        try:
                            published_at = datetime.strptime(article.publish_time, fmt).date()
                            break
                        except ValueError:
                            continue
                except Exception:
                    pass

            # 创建文章记录
            db_article = Article(
                url=article.url,
                title=article.title,
                content=article.content,
                html=article.html,
                author=article.author,
                summary=article.summary,
                content_hash=content_hash,
                published_at=published_at,
                source_type="wechat",  # 标记为公众号来源
                category_id=category_id,
                status="success",
                kg_status="pending"
            )

            self.db.add(db_article)
            self.db.commit()
            self.db.refresh(db_article)

            # 处理标签
            if article.tags:
                await self._process_keywords(db_article, article.tags)

            logger.info(f"文章保存成功: {article.title} (ID: {db_article.id})")
            return db_article

        except Exception as e:
            logger.error(f"保存文章失败: {e}")
            self.db.rollback()
            return None

    async def _process_keywords(
        self,
        article: Article,
        tags: List[str]
    ) -> None:
        """
        处理文章标签

        Args:
            article: Article 对象
            tags: 标签列表
        """
        try:
            for i, tag_name in enumerate(tags[:10]):  # 最多10个标签
                # 获取或创建关键词
                keyword = self.db.query(Keyword).filter(Keyword.name == tag_name).first()
                if not keyword:
                    keyword = Keyword(name=tag_name)
                    self.db.add(keyword)
                    self.db.flush()

                # 创建关联
                article_keyword = ArticleKeyword(
                    article_id=article.id,
                    keyword_id=keyword.id,
                    priority=i
                )
                self.db.add(article_keyword)

            self.db.commit()
        except Exception as e:
            logger.error(f"处理标签失败: {e}")
            self.db.rollback()


def get_wechat_pipeline(db: Session = None) -> WechatPipeline:
    """
    获取微信公众号处理管道实例

    Args:
        db: 数据库会话

    Returns:
        WechatPipeline 实例
    """
    if db is None:
        db = next(get_db())
    return WechatPipeline(db)
