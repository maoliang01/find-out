"""
网页爬取服务
使用 Firecrawl 爬取 + URL 日期提取 + LLM 摘要

核心特点：
1. 日期提取：URL 日期优先，严格验证
2. 爬取：Firecrawl（支持 JS 渲染）
3. 摘要：大模型辅助提取
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import Counter
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from urllib.parse import parse_qs, urlparse, urljoin, urlunparse
from zoneinfo import ZoneInfo
import httpx

from app.services.alternate_scraper import strip_semantic_noise_blocks
from app.services.site_router import RouteDecision, get_site_strategy_router

APP_TIMEZONE = ZoneInfo("Asia/Shanghai")


def current_local_date() -> date:
    """Return the application date in the user's configured China timezone."""
    return datetime.now(APP_TIMEZONE).date()


def canonicalize_article_url(url: str) -> str:
    """Remove tracking query parameters from The Paper article URLs only."""
    parsed = urlparse(url or "")
    if parsed.netloc.lower().endswith("thepaper.cn") and re.search(
        r"/newsDetail_forward_\d+", parsed.path, re.IGNORECASE
    ):
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    return url


def extract_date_from_content(content: str, base_url: str = "") -> Tuple[Optional[str], Optional[str]]:
    """
    从文章内容中提取发布日期

    Returns:
        Tuple[str, Optional[str]]: (published_at, author)
        例如: ("2026-06-23", "张三")
    """
    if not content:
        return None, None

    lines = [l.strip() for l in content.split('\n') if l.strip()]

    # 日期和作者提取模式（中文网站常用格式）
    # 模式1: "来源：光明日报 作者：张三 2026-06-24"
    # 模式2: "发布时间：2026-06-23  作者：李四"
    # 模式3: "发布日期：2026年6月23日"
    # 模式4: "2026-06-23 作者：王五"

    date_patterns = [
        # YYYY-MM-DD 格式
        r'(\d{4}-\d{2}-\d{2})',
        # YYYY/MM/DD 格式
        r'(\d{4}/\d{2}/\d{2})',
        # 中文格式：YYYY年MM月DD日
        r'(\d{4}年\d{1,2}月\d{1,2}日)',
    ]

    author_patterns = [
        # "作者：张三" 或 "作者:张三"
        r'作者[：:]\s*([^\s\d]+)',
        # "文/张三" 或 "文：张三"
        r'文[／/][图]?\s*([^\s\d]+)',
        # "摄影：张三"
        r'摄影[：:]\s*([^\s\d]+)',
    ]

    published_at = None
    author = None

    # 在前20行中搜索日期和作者（通常在文章开头）
    search_range = min(20, len(lines))
    for i, line in enumerate(lines[:search_range]):
        # 提取日期
        if published_at is None:
            for pattern in date_patterns:
                match = re.search(pattern, line)
                if match:
                    date_str = match.group(1)
                    # 转换为标准格式
                    if '-' in date_str:
                        normalized = date_str
                    elif '/' in date_str:
                        normalized = date_str.replace('/', '-')
                    else:
                        # 中文格式
                        normalized = date_str.replace('年', '-').replace('月', '-').replace('日', '')
                    # 验证日期
                    try:
                        parsed = datetime.strptime(normalized, "%Y-%m-%d")
                        d = parsed.date()
                        if d.year >= 2000 and d <= current_local_date() + timedelta(days=1):
                            published_at = normalized
                            break
                    except:
                        pass

        # 提取作者
        if author is None:
            for pattern in author_patterns:
                match = re.search(pattern, line)
                if match:
                    author = match.group(1).strip()
                    break

        if published_at and author:
            break

    return published_at, author


logger = logging.getLogger(__name__)

# ================================================
# 内容清理工具
# ================================================

def clean_content_light(content: str) -> str:
    """
    轻度清理文章内容（用于已经过正文提取的内容）
    只去除明显的干扰项，保留正文完整性
    """
    if not content:
        return content

    lines = content.split('\n')

    # 过滤规则
    skip_patterns = [
        # 只包含图片链接的行（或多个链接）
        r'^\s*!\[?\s*\]\(https?://[^\)]+\)\s*$',
        r'^\s*!\[\]\([^)]+\)(\s*!\[?\s*\]\([^)]+\)\s*)*$',
        # 纯图片链接 + 少量文字的行（如 "![logo](url) [logo](url)")
        r'^\s*(!\[?\s*\]\(https?://[^\)]+\)\s*){1,3}$',
        # 导航类标题
        r'^#{1,3}\s*(?:工作动态|党群园地|首页|科普园地|新闻中心|通知公告)',
        # 附件标记（单独一行或后面只有图片）
        r'^#{0,3}\s*附件[：:]?\s*$',
        # 纯粹的地址/邮编行
        r'^地址[：:]\s*[^\n]+邮编[：:]\s*\d+\s*$',
        # 备案/版权信息
        r'^备案序号[：:]',
        r'^京ICP备\d+号',
        # 工具栏/分享类
        r'^\s*\*\s*!\[\]\(https?://[^\)]+toolbar',
        r'^\s*\*\s*!\[\]\(https?://[^\)]+wx[^\)]*\)\s*!\[\]\(https?://[^\)]+\)\s*$',
        # 无意义的分隔行
        r'^#+$',
        r'^-+$',
        # 无障碍工具条残留
        r'^\s*(?:声音开关|显示屏|帮助|返回|退出)',
        r'^\s*请按F11切换大界面模式',
        r'^\s*提示：该链接属站外链接',
        r'^\s*请注意，该操作',
        r'^\s*该网站无法启动',
        r'^\s*当前访问页面超出',
        r'^\s*无障碍辅助工具',
        r'^\s*ALT\+\d+',
        r'^\s*语音播报',
        r'^\s*【字体：大 中 小】',
        r'^\s*浏览量[：:]\s*\d+',
        r'^\s*javascript:void',
        r'^\s*是否继续访问',
        r'^\s*快捷方式',
        r'^\s*(?:视窗区|交互区|服务区|列表区|正文区|导航区)',
        r'^\s*更多分享',
        r'^\s*打印',
        r'^\s*分享到',
        # 面包屑导航
        r'^\[首页\]\(https?://[^\)]+\)\s*>',
        r'^\[.+?\]\([^)]+\)\s*$',  # 单个链接行（通常是面包屑）
    ]

    filtered = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        is_skip = False
        for pattern in skip_patterns:
            if re.match(pattern, stripped, re.IGNORECASE):
                is_skip = True
                break

        if not is_skip:
            filtered.append(line)

    # 从后往前扫描，移除末尾的图片链接块
    while filtered:
        last = filtered[-1].strip()
        # 如果最后一行是纯图片链接或附件标题，移除
        if re.match(r'^\s*!\[?\s*\]\(https?://[^\)]+\)\s*$', last):
            filtered.pop()
        elif re.match(r'^\s*!\[\]\([^)]+\)\s*$', last):
            filtered.pop()
        elif last in ['###### 附件：', '#### 附件：', '### 附件：', '## 附件：', '# 附件：', '附件：', '附件']:
            filtered.pop()
        else:
            break

    # 规范化换行
    result = '\n'.join(filtered)

    # 去除行首行尾空白
    lines = [l.strip() for l in result.split('\n')]
    result = '\n'.join(line for line in lines if line)

    return result.strip()


def clean_content(content: str) -> str:
    """
    清理文章内容，去除无语义符号和导航元素

    清理规则：
    1. 去除 Markdown 图片链接: ![alt](url) -> 空
    2. 去除 Markdown 链接: [text](url) -> text
    3. 去除纯链接行: http://xxx.com -> 空
    4. 去除 javascript:void(0) 等无意义链接
    5. 去除零宽字符和不可见字符
    6. 去除网站导航元素（ENGLISH、网站地图、当前位置等）
    7. 规范化空白字符
    """
    if not content:
        return content

    # 0. 预处理：去除 SVG/XML 代码残留（优先处理，避免干扰后续规则）
    # 这些是用户反馈的无意义字符
    svg_patterns = [
        # SVG 路径数据残留（全模式匹配）
        r"id='Path'\s*fill='[^']*'\s*stroke='none'\s*/>",
        r"id='[^']*'\s*fill='[^']*'\s*stroke='[^']*'\s*/\s*>",
        r'%3e\s*%3cpath\s*d=',
        r"fill='%23[0-9A-Fa-f]+'",
        r'fill="%23[0-9A-Fa-f]+"',
        r"stroke='none'",
        r'stroke="none"',
        r"transform='translate\([^)]*\)'",
        r'transform="translate\([^)]*\)"',
        r'd="M[\d.,\s\-Zz]+"',
        r"<svg[^>]*>",
        r"</svg>",
        r"<g[^>]*>",
        r"</g>",
    ]
    for pattern in svg_patterns:
        content = re.sub(pattern, '', content, flags=re.IGNORECASE)

    # 去除所有 HTML/XML 标签（提前全部清理，避免残留）
    content = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', content, flags=re.IGNORECASE)
    content = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', content, flags=re.IGNORECASE)
    content = re.sub(r'<[^>]+>', '', content)
    content = re.sub(r'</[^>]+>', '', content)

    # 1. 去除 Markdown 图片链接: ![alt](url) -> 空
    content = re.sub(r'!\[([^\]]*)\]\([^)]+\)', '', content)

    # 2. 去除空图片链接行: [](url) -> 空
    content = re.sub(r'^\[\]\([^)]+\)\s*$', '', content, flags=re.MULTILINE)

    # 3. 去除空括号行: [] 或 []()
    content = re.sub(r'^\[\]\s*$', '', content, flags=re.MULTILINE)

    # 4. 去除 Markdown 链接，保留文字: [text](url) -> text
    content = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', content)

    # 5. 清理残留的空链接: []( 或 [])
    content = re.sub(r'\[\]$', '', content)
    content = re.sub(r'\[\]\([^)]*\)$', '', content, flags=re.MULTILINE)

    # 6. 去除纯URL链接行
    content = re.sub(r'^https?://[^\s]+$', '', content, flags=re.MULTILINE)

    # 7. 清理所有剩余的空链接格式: [](...) 或 []
    content = re.sub(r'\[\]', '', content)
    content = re.sub(r'\[\]\([^)]*\)', '', content)

    # 8. 去除 javascript:void(0) 和类似的无意义链接
    content = re.sub(r'javascript:void\s*\(0\)', '', content)
    content = re.sub(r'javascript:;', '', content)

    # 9. 去除零宽字符
    zero_width_chars = [
        '​', '‌', '‍', '﻿', '­', '᠎', '​', '‌', '‍', '﻿',
        '​', '‌', '‍', '﻿', '­',
    ]
    for char in zero_width_chars:
        content = content.replace(char, '')

    # 10. 去除 URL 编码残留（如 %20、%23 等）
    content = re.sub(r'%[0-9A-Fa-f]{2}', ' ', content)

    # 11. 去除控制字符（保留换行和回车）
    content = ''.join(char for char in content if ord(char) >= 32 or char in '\n\r\t')

    # 12. 规范化空白字符
    content = re.sub(r'[ \t]+', ' ', content)
    content = re.sub(r'\n{3,}', '\n\n', content)

    # 13. 去除行首行尾空白，移除空行
    lines = [line.strip() for line in content.split('\n')]
    content = '\n'.join(line for line in lines if line)

    # 14. 最终清理：移除孤立的空括号
    content = re.sub(r'^\s*\(\)\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^\s*\(\s*\)\s*$', '', content, flags=re.MULTILINE)

    # 15. 去除开头的导航菜单元素（1. ENGLISH 2. 网站地图 等）
    # 匹配类似 "1. ENGLISH\n2. 网站地图\n..." 的模式
    nav_pattern = r'^(?:\d+\.\s*(?:ENGLISH|网站地图|中国科学院|邮箱登录|联系我们)[\s\n]*)+'
    content = re.sub(nav_pattern, '', content, flags=re.IGNORECASE)

    # 16. 去除"当前位置"导航行和栏目导航
    content = re.sub(r'^当前位置[：:]?\s*>>?\s*首页\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^>>\s*首页\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^>>\s*[一-龥A-Za-z]+(?:\s*>>\s*[一-龥A-Za-z]+)*\s*$', '', content, flags=re.MULTILINE)  # >> 栏目名

    # 17. 去除顶部的栏目名（单独一行的"工作动态"、"党群园地"等）
    content = re.sub(r'^(?:工作动态|党群园地|首页|科普园地|组织机构|新闻中心)[^\n]*\n?', '', content, flags=re.MULTILINE)

    # 18. 去除分隔线（----、====、****）
    content = re.sub(r'^[-=*]{3,}\s*$', '', content, flags=re.MULTILINE)

    # 19. 去除底部的版权和备案信息
    footer_patterns = [
        r'版权所有\s*[©©]\s*[^\n]+',
        r'备案序号[：:]\s*[^\n]+',
        r'京ICP备\d+号[^\n]*',
        r'京公网安备\d+号[^\n]*',
        r'地址[：:]\s*[^\n]+',
        r'邮编[：:]\s*\d+[^\n]*',
    ]
    for pattern in footer_patterns:
        content = re.sub(pattern, '', content)

    # 20. 去除底部"版权所有"等整行
    content = re.sub(r'^[^\n]*版权所有[^\n]*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^[^\n]*备案序号[^\n]*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^[^\n]*京ICP备[^\n]*$', '', content, flags=re.MULTILINE)

    # 21. 再清理"加载更多"等按钮文字
    content = re.sub(r'^加载更多[^\n]*$', '', content, flags=re.MULTILINE)

    # 22. 去除网站导航元素（手机版、PC版本、网站无障碍等）- 包括带下划线格式
    nav_element_patterns = [
        r'^_手机版_\s*$',
        r'^_PC版本_\s*$',
        r'^手机版\s*$',
        r'^PC版本\s*$',
        r'^网站无障碍\)\s*$',
        r'^学习进行时\s*$',
        r'^多语种频道\s*$',
        r'^地方频道\s*$',
        r'^网站地图\s*$',
        r'^学习进行时[^\n]*$',
        r'^[_\-]*(?:news|home|index|about|contact|login)[_\-]*\s*$',  # 英文导航残留
    ]
    for pattern in nav_element_patterns:
        content = re.sub(pattern, '', content, flags=re.MULTILINE)

    # 23. 去除栏目列表导航（如：* 高层、* 时政、* 人事 等 Markdown 列表格式）
    # 先处理列表项格式，再处理纯文字格式
    category_list_patterns = [
        # Markdown 列表格式: * 高层
        r'^\*\s*(?:学习进行时|高层|时政|人事|国际|财经|网评|港澳|台湾|思客智库|全球连线|教育|科技|科创|量子|体育|文化|书画|健康|军事|访谈|视频|图片|政务|法律|中央文件|金融|汽车|食品|人居|信息化|数字经济|学术中国|乡村振兴|银龄|溯源中国|城市|旅游|能源|会展|彩票|娱乐|时尚|悦读|公益|一带一路|亚太网|上市公司|文化产业)\s*$',
        # Markdown 列表格式: * 北京、天津、河北 等地方
        r'^\*\s*(?:北京|天津|河北|山西|辽宁|吉林|上海|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|广东|广西|海南|重庆|四川|贵州|云南|西藏|陕西|甘肃|青海|宁夏|新疆|内蒙古|黑龙江)\s*$',
        # Markdown 列表格式: * English、Español 等多语种
        r'^\*\s*(?:English|Español|Français|عربى|Русский\s*язык|日本語|한국어|Deutsch|Português)\s*$',
        # 纯文字栏目名（不带星号）
        r'^(?:学习进行时|高层|时政|人事|国际|财经|网评|港澳|台湾|思客智库|全球连线|教育|科技|科创|量子|体育|文化|书画|健康|军事|访谈|视频|图片|政务|法律|中央文件)\s*$',
        r'^(?:北京|天津|河北|山西|辽宁|吉林|上海|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|广东|广西|海南|重庆|四川|贵州|云南|西藏|陕西|甘肃|青海|宁夏|新疆|内蒙古|黑龙江)\s*$',
    ]
    for pattern in category_list_patterns:
        content = re.sub(pattern, '', content, flags=re.MULTILINE | re.IGNORECASE)

    # 24. 去除 Play Video 等无意义文字残留
    content = re.sub(r"Play\s*Video\s*", '', content, flags=re.IGNORECASE)

    # 25. 去除无意义的占位符和提示文字
    placeholder_patterns = [
        r'^点击播放[^\n]*$',
        r'^视频播放[^\n]*$',
        r'^请输入关键字[^\n]*$',
        r'^搜索[^\n]*$',
        r'^分享到[^\n]*$',
        r'^\d+:\d+\s*$',  # 纯时间格式行
        r'^/0:\d+\s*$',  # /0:00 格式
        r'^0:/0:\d+/0:\d+\s*$',  # 媒体时间显示残留
    ]
    for pattern in placeholder_patterns:
        content = re.sub(pattern, '', content, flags=re.MULTILINE)

    # 26. 去除 SVG/Path 等残留元素（更彻底）
    svg_residual_patterns = [
        # 整行匹配：id='Path' 或 d='M...' 等
        r"^[^'\n]*'[^'\n]*$",  # 整行只有引号包裹的内容（可疑的 SVG 残留）
        # 匹配 d='M数字字母...' 这样的 SVG 路径行
        r"^\s*d\s*=\s*['\"][M\d.,\s\-Zz]+['\"]",
        r"^\s*fill\s*=\s*['\"][^'\"]*['\"]",
        r"^\s*stroke\s*=\s*['\"][^'\"]*['\"]",
        r"^\s*transform\s*=\s*['\"][^'\"]*['\"]",
        # 匹配 path d='...' 格式
        r"path\s+d\s*=\s*['\"][^'\"]+['\"]",
        r"^\s*path\s+d\s*=\s*['\"]",
        # 匹配类似 "M1.67473 0.487896C1.53133..." 的 SVG 路径数据行
        r"^\s*[Mm]\d+[\d.,\s\-Zz]+",
        # URL 编码的 HTML 标签残留
        r"%3c[^>]+>%s*",  # <...>
        r"%3e\s*",  # >
        r"\s*%3c",  # <
        r"&lt;[^&]+&gt;",  # &lt;...&gt;
    ]
    for pattern in svg_residual_patterns:
        content = re.sub(pattern, '', content, flags=re.MULTILINE | re.IGNORECASE)

    # 26. 再次清理空行
    lines = [line.strip() for line in content.split('\n')]
    content = '\n'.join(line for line in lines if line)

    # 27. 去除连续的无意义符号（如 _ _ _ _ 多个下划线）
    content = re.sub(r'^[_]{3,}\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^[_\-]{3,}\s*$', '', content, flags=re.MULTILINE)

    # 28. 去除行首行尾空白，移除空行（最终清理）
    lines = [line.strip() for line in content.split('\n')]
    content = '\n'.join(line for line in lines if line)

    # 29. 清理 CAS 特有的无障碍工具残留（如 "PC ;)"、") (url)" 等）
    content = re.sub(r'^\s*PC\s*\)?\s*;+\s*\)?\s*$', '', content, flags=re.MULTILINE | re.IGNORECASE)
    content = re.sub(r'^\s*PC\s*/\s*English\s*$', '', content, flags=re.MULTILINE | re.IGNORECASE)
    content = re.sub(r'^\s*;\)\s*\)?\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^\s*;\)\s*\(https?://[^\)]+\)\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^\s*\(https?://[^\)]+\)\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^\s*/\s*无障碍.*$', '', content, flags=re.MULTILINE | re.IGNORECASE)
    content = re.sub(r'^\s*/\s*关怀版.*$', '', content, flags=re.MULTILINE | re.IGNORECASE)
    content = re.sub(r'^\s*/\s*联系我们.*$', '', content, flags=re.MULTILINE | re.IGNORECASE)
    content = re.sub(r'^\s*/\s*网站地图.*$', '', content, flags=re.MULTILINE | re.IGNORECASE)
    content = re.sub(r'^\s*/\s*邮箱\s*$', '', content, flags=re.MULTILINE | re.IGNORECASE)

    # 30. 检测并清理文章末尾的导航块（连续 "* xxx" 列表）
    # 同时检测开头的导航块
    nav_block_start = None
    star_lines = []
    lines = content.split('\n')

    # 先检查开头是否有导航块（前 1/3 部分）
    head_nav_end = None
    head_star_count = 0
    for i, line in enumerate(lines[:len(lines)//3]):
        stripped = line.strip()
        if stripped.startswith('* ') and len(stripped) > 2:
            head_star_count += 1
            if head_star_count >= 5:  # 开头有 5 个以上的 * 列表项，视为导航块
                head_nav_end = i + 1
                break
        elif stripped and not stripped.startswith('*'):
            if head_star_count < 5:
                head_star_count = 0  # 重置，因为被非星号行打断了

    if head_nav_end:
        lines = lines[head_nav_end:]

    # 再检查末尾的导航块（后 2/3 部分）
    start_idx = len(lines) // 3
    for i, line in enumerate(lines[start_idx:], start_idx):
        stripped = line.strip()
        if stripped.startswith('* ') and len(stripped) > 2:
            star_lines.append((i, stripped))
            if len(star_lines) >= 3:
                nav_block_start = star_lines[0][0]
                break
        else:
            if len(star_lines) < 3:
                star_lines = []

    if nav_block_start is not None:
        content = '\n'.join(lines[:nav_block_start])
        lines = content.split('\n')

    # 31. 清理 CAS 特有导航项
    cas_nav_items = [
        '成果转化', '知识产权', '工作动态', '人才教育', '教育简介',
        '主要职责', '办院方针', '院况简介', '机构设置',
        '科技奖励', '科技期刊', '科技专项', '科研进展',
    ]
    for item in cas_nav_items:
        content = re.sub(rf'^\*\s*{re.escape(item)}[^\n]*\n?', '', content, flags=re.MULTILINE)

    # 32. 清理首页/机构介绍等残留（标题 + URL 模式）
    # 匹配 "标题" + 换行 + "更多+" 模式
    content = re.sub(r'^.*更多\+\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^.*\. 更多\+\s*$', '', content, flags=re.MULTILINE)

    # 清理"【首页】"、"首页 (https://..."这类行
    content = re.sub(r'^【首页】.*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^首页\s*\(https?://[^\)]*\)\s*$', '', content, flags=re.MULTILINE)

    # 清理机构介绍块：**简介**：内容...
    content = re.sub(r'^\*{0,2}简介\*{0,2}：.*$', '', content, flags=re.MULTILINE)

    # 33. 清理残留的符号行和单字符行
    content = re.sub(r'^\s*\(\s*\;\s*\)\s*$', '', content, flags=re.MULTILINE)  # (;)
    content = re.sub(r'^\s*\(\s*\)\s*$', '', content, flags=re.MULTILINE)  # ()
    content = re.sub(r'^\s*\*\s*$', '', content, flags=re.MULTILINE)  # 单独的 *

    # 33. 最终清理：去除空行
    lines = [line.strip() for line in content.split('\n')]
    content = '\n'.join(line for line in lines if line)

    # 34. 检测是否为纯导航页：如果清理后内容仍然很少且包含大量导航项，返回空
    final_lines = [l for l in content.split('\n') if l.strip()]
    if len(final_lines) > 5:
        nav_only_lines = []
        for line in final_lines:
            if line.startswith('* ') or line.startswith('- '):
                nav_only_lines.append(line)
        # 如果超过 60% 的行是导航列表项，认为是纯导航页
        if len(nav_only_lines) / len(final_lines) > 0.6:
            logger.info(f"内容清理后仍为导航页，清空内容：{len(nav_only_lines)}/{len(final_lines)}")
            return ""

    return content.strip()


def extract_title_from_content(content: str) -> str:
    """
    从文章内容中提取标题

    优先顺序：
    1. Markdown 图片链接格式中的文字: ![标题](url)
    2. Markdown 链接格式中的文字: [标题](url)
    3. 第一个非空行如果看起来像标题（10-50字符，无标点结尾）
    4. 从来源/作者行之前的内容中提取
    """
    if not content:
        return ""
    
    # 常见的无意义标题，跳过
    skip_titles = {
        '回到顶部', '返回', '首页', '上一页', '下一页',
        '更多', '查看全文', '点击查看', '展开', '收起',
        'javascript:void(0)', 'javascript:;', '#', '',
    }

    lines = [l.strip() for l in content.split('\n') if l.strip()]

    # 1. 尝试从 Markdown 图片链接提取标题: ![标题](url)
    for line in lines:
        img_match = re.match(r'^!\[([^\]]*)\]\([^)]+\)$', line)
        if img_match:
            title = img_match.group(1).strip()
            if title and title not in skip_titles and len(title) >= 4:
                # 进一步检查：不能是纯数字或纯符号
                if not title.isdigit() and not re.match(r'^[\W_]+$', title):
                    return title

    # 2. 尝试从 Markdown 链接提取标题: [标题](url)
    for line in lines:
        link_match = re.match(r'^\[([^\]]+)\]\([^)]+\)$', line)
        if link_match:
            title = link_match.group(1).strip()
            if (title and title not in skip_titles and len(title) >= 4 
                and not title.startswith('!') and not title.isdigit()
                and not re.match(r'^[\W_]+$', title)):
                return title

    # 3. 查找 "来源：" 或 "作者：" 行之前的标题行
    for i, line in enumerate(lines):
        if '来源：' in line or '作者：' in line:
            # 向前查找第一个可能是标题的行
            for j in range(i-1, -1, -1):
                prev = lines[j]
                # 跳过链接行和空行
                if prev.startswith('[') or prev.startswith('![') or prev.startswith('#'):
                    continue
                # 跳过纯分隔符行: === 或 --- 或 *** 等
                if prev and re.match(r'^[\-\=\*]{3,}$', prev):
                    continue
                if prev in skip_titles or len(prev) < 4:
                    continue
                if 10 <= len(prev) <= 60:
                    not_title_endings = ('。', '！', '？', '.', '!', '?', '，', '；', ',', '-')
                    if not prev.endswith(not_title_endings):
                        return prev

    # 4. 第一行如果看起来像标题
    for line in lines[:5]:
        if line.startswith('![') or line.startswith('[') or line.startswith('-') or line.startswith('#'):
            continue
        if line in skip_titles or len(line) < 4:
            continue
        not_title_endings = ('。', '！', '？', '.', '!', '?', '，', '；', ',', '-')
        if 10 <= len(line) <= 50 and not line.endswith(not_title_endings):
            chinese_ratio = sum(1 for c in line if '\u4e00' <= c <= '\u9fff') / len(line)
            if chinese_ratio >= 0.5:
                return line

    return ""


def format_content_with_summary(content: str, summary: str) -> str:
    """
    将摘要和原文组合成最终内容

    格式：
    【摘要】
    摘要内容

    【正文】
    原文内容
    """
    if summary:
        # 清理内容中的底部附件块
        content = _strip_attachments(content)
        return f"""【摘要】
{summary}

【正文】
{content}"""
    return content


def _strip_attachments(content: str) -> str:
    """
    去掉文章底部的附件块

    附件块通常是：
    - ###### 附件：
    - ![logo](url) 类型的图片链接
    - [![](url1)](url2) 类型的 Logo 链接
    """
    if not content:
        return content

    lines = content.split('\n')
    filtered = []

    for line in lines:
        stripped = line.strip()
        # 跳过附件标题行
        if re.match(r'^#{0,6}\s*附件[：:]?\s*$', stripped):
            continue
        # 跳过纯图片链接行（如 ![](url) 或 ![alt](url)）
        if re.match(r'^\s*!\[?\s*\]\(https?://[^\)]+\)\s*$', stripped):
            continue
        # 跳过行内只有多个图片链接的行
        if re.match(r'^(\s*!\[?\s*\]\(https?://[^\)]+\)\s*)+$', stripped):
            continue
        # 跳过无意义的分隔行
        if re.match(r'^#+$', stripped) or re.match(r'^_+$', stripped):
            continue
        # 跳过只包含图片链接的行（包括 [![](url)](link) 格式）
        # 这种行通常有 Logo + 链接
        if re.match(r'^\[!?\s*\]\(https?://[^\)]+\)\s*(\s*\[!?\s*\]\(https?://[^\)]+\)\s*)*(\[?\s*\]\(https?://[^\)]+\)\s*)?$', stripped):
            continue
        # 跳过类似 "[ ![](url) ![](url) ](url)" 的多 Logo 行
        if re.match(r'^\[\s*(!\[?\s*\]\([^)]+\)\s*)+\]\([^)]+\)\s*$', stripped):
            continue
        filtered.append(line)

    # 从后往前清理残留的图片链接
    while filtered:
        last = filtered[-1].strip()
        # 检查是否是附件相关的行
        is_attachment = False
        # 纯图片链接
        if re.match(r'^\s*!?\s*\[\s*\]\([^\)]+\)', last):
            # 匹配 ![](url) 或 [![](url)](link) 格式
            is_attachment = True
        elif re.match(r'^(\s*!?\s*\[\s*\]\([^\)]+\)\s*)+$', last):
            is_attachment = True
        elif last in ['###### 附件：', '#### 附件：', '### 附件：', '## 附件：', '# 附件：', '附件：', '附件']:
            is_attachment = True
        elif re.match(r'^!\[?\s*\[\s*\]\([^)]+\)\s*!\[?\s*\[\s*\]\([^)]+\)\s*$', last):
            is_attachment = True

        # 检查最后一行是否主要是图片链接（长度很短且包含多个图片URL）
        if not is_attachment:
            # 计算图片链接的数量
            img_count = len(re.findall(r'!\[?\s*\[\s*\]\(', last))
            link_count = len(re.findall(r'\]\([^)]+\)', last))
            if img_count >= 2 or (img_count >= 1 and len(last) < 150):
                # 如果有多个图片链接或只有一个但行很短，认为是附件
                is_attachment = True

        if is_attachment:
            filtered.pop()
        else:
            break

    return '\n'.join(filtered).strip()


# ================================================
# 进度事件管理器（用于SSE实时推送）
# ================================================
class ScrapeProgress:
    """爬取进度事件管理器"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._callbacks = []
            cls._instance._current_progress = {}
        return cls._instance

    def subscribe(self, callback):
        self._callbacks.append(callback)
        return lambda: self._callbacks.remove(callback)

    def emit(self, scrape_id: str, event: str, data: dict):
        event_data = {
            "scrape_id": scrape_id,
            "event": event,
            "data": data,
            "timestamp": datetime.now().isoformat(),
        }
        self._current_progress[scrape_id] = event_data
        for callback in self._callbacks:
            try:
                callback(event_data)
            except Exception:
                pass

    def set_progress(self, scrape_id: str, progress: dict):
        self._current_progress[scrape_id] = {
            **progress,
            "scrape_id": scrape_id,
            "timestamp": datetime.now().isoformat(),
        }

    def get_progress(self, scrape_id: str) -> dict:
        return self._current_progress.get(scrape_id, {})

    def clear_progress(self, scrape_id: str):
        self._current_progress.pop(scrape_id, None)


progress_manager = ScrapeProgress()


# ================================================
# 爬取专用日志记录器
# ================================================
class ScrapeLogger:
    """爬取日志记录器"""

    def __init__(self, log_dir: str = "logs", log_file: str = "scrape.log"):
        from pathlib import Path
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        self.log_file = self.log_dir / log_file
        self._setup_logger()

    def _setup_logger(self):
        self.logger = logging.getLogger("scrape_logger")
        self.logger.setLevel(logging.DEBUG)
        if not self.logger.handlers:
            handler = logging.FileHandler(self.log_file, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            self.logger.addHandler(handler)

    def info(self, msg: str):
        self.logger.info(msg)

    def log_scrape_result(self, url: str, status: str, word_count: int, title: str = ""):
        title_preview = title[:40] + "..." if len(title) > 40 else title
        self.info(f"爬取结果 | 状态: {status} | 字数: {word_count} | 标题: {title_preview}")

    def log_article_links(self, url: str, links: List[str]):
        self.info(f"文章链接识别 | 来源: {url} | 数量: {len(links)}")
        for i, link in enumerate(links[:10], 1):
            self.info(f"  {i}. {link}")


scrape_logger = ScrapeLogger()

# ================================================
# 日期验证常量
# ================================================
MIN_VALID_YEAR = 2000
MAX_FUTURE_DAYS = 1  # 允许未来1天（时区误差）
MAX_AGE_YEARS = 10   # 文章最长10年

# ================================================
# 内容质量阈值
# ================================================
MIN_CONTENT_WORDS = 50  # 内容最少字数

KEYWORD_STOPWORDS = {
    "我们", "你们", "他们", "进行", "通过", "有关", "相关", "工作", "活动", "表示",
    "指出", "强调", "推进", "开展", "进一步", "不断", "持续", "加强", "提升",
    "实现", "建设", "发展", "研究", "创新", "信息", "中心", "单位", "文章",
    "内容", "发布", "来源", "记者", "编辑", "责任编辑", "中国", "有限公司",
}


def _normalize_keywords(keywords_raw: Any, limit: int = 5) -> List[str]:
    """Normalize LLM or local keyword output into a short unique list."""
    values: List[str] = []
    if isinstance(keywords_raw, str):
        values = re.split(r"[,，、;；\n]+", keywords_raw)
    elif isinstance(keywords_raw, list):
        for item in keywords_raw:
            if isinstance(item, str):
                values.extend(re.split(r"[,，、;；\n]+", item))
            elif item:
                values.append(str(item))

    normalized: List[str] = []
    seen = set()
    for value in values:
        keyword = re.sub(r"^[#\-\s]+|[#\-\s]+$", "", str(value).strip())
        keyword = keyword.strip("：:。.!！?？\"'“”‘’（）()[]【】")
        if not keyword or keyword in seen or keyword in KEYWORD_STOPWORDS:
            continue
        if len(keyword) < 2 or len(keyword) > 24:
            continue
        seen.add(keyword)
        normalized.append(keyword)
        if len(normalized) >= limit:
            break
    return normalized


def extract_keywords_locally(title: str, content: str, limit: int = 5) -> List[str]:
    """Extract stable keywords without an LLM as a persistence fallback."""
    text = "\n".join([title or "", content or ""])
    if not text.strip():
        return []

    candidates: List[str] = []
    candidates.extend(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,24}(?:院|所|中心|大学|公司|集团|平台|系统|项目|工程|大赛|会议|论坛|计划|研究院|实验室)", text))
    candidates.extend(re.findall(r"[\u4e00-\u9fff]{3,10}", text))
    candidates.extend(re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,24}", text))

    scored = Counter()
    title_text = title or ""
    for candidate in candidates:
        candidate = candidate.strip()
        if candidate in KEYWORD_STOPWORDS or len(candidate) < 2 or len(candidate) > 24:
            continue
        if re.fullmatch(r"\d+", candidate):
            continue
        score = 3 if candidate in title_text else 1
        if len(candidate) >= 4:
            score += 1
        scored[candidate] += score

    return [kw for kw, _ in scored.most_common(limit)]


def summarize_locally(content: str, limit: int = 180) -> str:
    """Use the first meaningful sentences when the LLM metadata step is unavailable."""
    if not content:
        return ""
    text = re.sub(r"\s+", " ", content).strip()
    sentences = re.split(r"(?<=[。！？!?])\s*", text)
    summary = ""
    for sentence in sentences:
        if len(sentence.strip()) < 8:
            continue
        if len(summary) + len(sentence) > limit and summary:
            break
        summary += sentence
    return (summary or text[:limit]).strip()


@dataclass
class ScrapeOptions:
    """爬取选项"""
    extract_content: bool = True
    fetch_html: bool = False
    preserve_format: bool = False
    max_depth: int = 0
    timeout: int = 30
    extract_metadata: bool = True
    cookies: Optional[str] = None  # Cookie 字符串，用于绕过反爬

    @classmethod
    def for_background_task(cls, timeout: int) -> "ScrapeOptions":
        """Background crawls with metadata extraction enabled for style classification."""
        return cls(timeout=timeout, extract_metadata=True)


@dataclass
class ScrapedResult:
    """爬取结果"""
    url: str
    title: str = ""
    content: str = ""
    html: str = ""
    word_count: int = 0
    links: List[str] = field(default_factory=list)
    status: str = "pending"
    error_message: Optional[str] = None
    scraped_at: Optional[str] = None
    published_at: Optional[str] = None
    author: Optional[str] = None
    summary: Optional[str] = None
    keywords: List[str] = field(default_factory=list)
    style: Optional[str] = None  # 文体（新闻、通知、纪要等）
    metadata: Optional[Dict[str, Any]] = None  # 元数据（如 is_final_content 标记）

    def __post_init__(self):
        if self.links is None:
            self.links = []
        if self.keywords is None:
            self.keywords = []
        if self.scraped_at is None:
            self.scraped_at = datetime.now().isoformat()


LIST_METADATA_PLACEHOLDER = "详情页受访问限制；以上为来源网站栏目列表公开信息"
LIST_METADATA_ONLY_ERROR = "该详情页不允许公开爬取或未提供公开正文，未保存到文档管理"


def mark_result_as_metadata_only(result: ScrapedResult, list_title: str) -> ScrapedResult:
    """Keep verifiable list metadata without fabricating article content."""
    result.status = "metadata_only"
    result.title = list_title
    result.content = ""
    result.html = ""
    result.word_count = 0
    result.summary = None
    result.keywords = []
    result.style = None
    result.error_message = LIST_METADATA_ONLY_ERROR
    return result


def merge_scraped_result_into_article(
    article: Any,
    result: ScrapedResult,
    category_id: Optional[str] = None,
    source_id: Optional[str] = None,
) -> bool:
    """Update an existing article without erasing established provenance."""
    previous_hash = article.content_hash or article.calculate_content_hash()
    article.title = result.title or article.title
    article.content = result.content or article.content
    article.html = result.html or article.html
    article.word_count = result.word_count or article.word_count
    article.author = result.author or article.author
    article.summary = result.summary or article.summary
    article.style = result.style or article.style
    article.status = result.status
    article.error_message = result.error_message

    if category_id:
        article.category_id = category_id
    if source_id:
        article.source_id = source_id

    if result.published_at:
        try:
            article.published_at = datetime.strptime(result.published_at, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            pass

    article.content_hash = article.calculate_content_hash()
    content_changed = previous_hash != article.content_hash
    if content_changed:
        article.kg_status = "pending"
        article.kg_processed_at = None
        article.kg_error_message = None
    return content_changed


class DateExtractor:
    """
    日期提取器（核心组件）
    严格按优先级提取和验证日期
    """

    # URL 日期正则模式（按优先级）
    # 每个元组: (正则模式, 处理函数)
    # 处理函数接收匹配的组，返回格式化的日期字符串
    URL_DATE_PATTERNS = [
        # 1. /YYYYMMDD/ 目录格式（如 /20260630/）- 最精确
        (r'/(\d{4})(\d{2})(\d{2})/', lambda g: f"{g[0]}-{g[1]}-{g[2]}"),
        # 2. /YYYY/MM/DD/ 格式（如 /2026/06/30/）
        (r'/(\d{4})/(\d{2})/(\d{2})/', lambda g: f"{g[0]}-{g[1]}-{g[2]}"),
        # 3. /YYYYMM/tYYYYMMDD 格式（如 /202606/t20260630_xxx.shtml）
        (r'/t(\d{4})(\d{2})(\d{2})[_\.]', lambda g: f"{g[0]}-{g[1]}-{g[2]}"),
        # 4. 文件名开始8位日期 + 字母 /20260630abc.html
        (r'/(\d{8})[a-zA-Z0-9]+\.[a-z]+', lambda g: f"{g[0][:4]}-{g[0][4:6]}-{g[0][6:8]}"),
        # 5. 文件名8位日期 /20260630.html 等
        (r'/(\d{8})\.[a-z]+', lambda g: f"{g[0][:4]}-{g[0][4:6]}-{g[0][6:8]}"),
    ]

    # URL 参数日期格式
    URL_PARAM_PATTERNS = [
        r'[?&](?:date|time|publish|created?|updated?)=(\d{8})',
        r'[?&](?:date|time|publish|created?|updated?)=(\d{4}-\d{2}-\d{2})',
    ]

    @classmethod
    def extract_from_url(cls, url: str) -> Optional[str]:
        """
        从 URL 提取日期（最可靠）

        支持的格式：
        - /20260630/ - 直接8位日期
        - /2026/06/30/ - 斜杠分隔
        - /202606/t20260630_xxx.html - t+8位日期
        - /xxx/20260630.html - 文件名8位日期
        - ?date=20260630 - URL参数
        """
        # 1. 优先匹配目录日期模式
        for pattern, fmt in cls.URL_DATE_PATTERNS:
            match = re.search(pattern, url)
            if match:
                try:
                    groups = match.groups()
                    if callable(fmt):
                        date_str = fmt(groups)
                    else:
                        date_str = fmt % groups
                    if cls._validate_date(date_str):
                        return date_str
                except:
                    continue

        # 2. URL 参数日期
        for pattern in cls.URL_PARAM_PATTERNS:
            match = re.search(pattern, url)
            if match:
                date_str = match.group(1)
                if len(date_str) == 8:  # YYYYMMDD
                    date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                if cls._validate_date(date_str):
                    return date_str

        return None

    @classmethod
    def _validate_date(cls, date_str: str) -> bool:
        """
        严格验证日期是否合理
        """
        try:
            parsed = datetime.strptime(date_str, "%Y-%m-%d")
            d = parsed.date()
            today = current_local_date()

            # 1. 不能是未来日期（允许1天误差）
            if d > today + timedelta(days=MAX_FUTURE_DAYS):
                return False

            # 2. 不能太早（早于2000年）
            if d.year < MIN_VALID_YEAR:
                return False

            # 3. 不能太老（超过10年）
            age = (today - d).days / 365
            if age > MAX_AGE_YEARS:
                return False

            return True
        except (ValueError, TypeError):
            return False

    @classmethod
    def extract_from_html(cls, html: str) -> Optional[str]:
        """
        从 HTML 提取日期（次优选择）
        """
        # 1. <time> 元素
        time_match = re.search(r'<time[^>]+datetime=["\']?(\d{4}-\d{2}-\d{2})', html)
        if time_match and cls._validate_date(time_match.group(1)):
            return time_match.group(1)

        # 2. JSON-LD
        json_match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html)
        if json_match:
            date_str = json_match.group(1).split('T')[0]
            if cls._validate_date(date_str):
                return date_str

        # 3. 专用发布日期属性（中文网站常用）
        date_patterns = [
            r'<[^>]+class=["\'][^"\']*time[^"\']*["\'][^>]*>(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)',
            r'<[^>]+class=["\'][^"\']*date[^"\']*["\'][^>]*>(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)',
            r'<[^>]+class=["\'][^"\']*pub[^"\']*["\'][^>]*>(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)',
        ]
        for pattern in date_patterns:
            match = re.search(pattern, html)
            if match:
                date_str = cls._normalize_chinese_date(match.group(1))
                if date_str and cls._validate_date(date_str):
                    return date_str

        # 4. Meta 标签（谨慎使用，部分网站的 Meta 日期不准确）
        meta_patterns = [
            r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']article:published_time["\']',
            r'<meta[^>]+(?:name|itemprop)=["\'](?:publishdate|pubdate|datepublished|date)["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|itemprop)=["\'](?:publishdate|pubdate|datepublished)["\']',
            # 政府网站常用: firstpublishedtime, lastmodifiedtime
            r'<meta[^>]+name=["\']firstpublishedtime["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']firstpublishedtime["\']',
            r'<meta[^>]+name=["\']lastmodifiedtime["\'][^>]+content=["\']([^"\']+)',
        ]
        for pattern in meta_patterns:
            match = re.search(pattern, html)
            if match:
                date_str = match.group(1).split('T')[0]
                # 处理非标准日期格式: 2026-07-31-23:02:00 -> 2026-07-31
                if re.match(r'\d{4}-\d{2}-\d{2}-\d{2}:\d{2}:\d{2}', date_str):
                    date_str = date_str[:10]
                if cls._validate_date(date_str):
                    return date_str

        # 5. 相对发布时间（例如“14小时前”“30分钟前”）。不少动态新闻站
        # 不输出绝对日期；这仍是页面明确给出的发布时间，不能因此被日期筛选
        # 误判为范围外。仅接受有限的、常见的相对时间单位，避免将正文中的
        # 任意时间描述当作发布时间。
        relative_patterns = (
            (r'(?<!\d)(\d{1,3})\s*(?:分钟前|分钟之前|minutes?\s+ago)', "minutes"),
            (r'(?<!\d)(\d{1,3})\s*(?:小时前|小时之前|hours?\s+ago)', "hours"),
            (r'(?:昨天|昨日)\s*(?:\d{1,2}[:：]\d{2})?', "days"),
        )
        for pattern, unit in relative_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if not match:
                continue
            try:
                if unit == "minutes":
                    published = (datetime.now(APP_TIMEZONE) - timedelta(minutes=int(match.group(1)))).date()
                elif unit == "hours":
                    published = (datetime.now(APP_TIMEZONE) - timedelta(hours=int(match.group(1)))).date()
                else:
                    published = current_local_date() - timedelta(days=1)
                date_str = published.isoformat()
                if cls._validate_date(date_str):
                    return date_str
            except (TypeError, ValueError):
                continue

        return None

    @classmethod
    def extract_list_item_dates(cls, html: str, base_url: str) -> Dict[str, str]:
        """提取列表页中与文章链接位于同一条目内的站点显示日期。"""
        if not html:
            return {}
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            return {}

        result: Dict[str, str] = {}
        date_pattern = re.compile(
            r'(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?'
        )
        for anchor in soup.find_all("a", href=True):
            url = urljoin(base_url, anchor.get("href", "")).split("#", 1)[0]
            if not url:
                continue
            container = anchor.find_parent(["li", "tr", "article"])
            if container is None:
                container = anchor.parent
            text = container.get_text(" ", strip=True) if container else ""
            match = date_pattern.search(text)
            if not match:
                continue
            date_str = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
            if cls._validate_date(date_str):
                result[url] = date_str
        return result

    @classmethod
    def extract_thepaper_list_dates(cls, html: str, base_url: str) -> Dict[str, str]:
        """读取澎湃列表页 Next.js 数据中的文章发布时间。"""
        if not html or 'thepaper.cn' not in base_url.lower():
            return {}
        result: Dict[str, str] = {}
        pattern = re.compile(
            r'"contId"\s*:\s*(\d+).{0,1800}?"pubTimeLong"\s*:\s*(\d{13})',
            re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(html):
            try:
                published = datetime.fromtimestamp(int(match.group(2)) / 1000).strftime("%Y-%m-%d")
            except (ValueError, OSError, OverflowError):
                continue
            if cls._validate_date(published):
                result[urljoin(base_url, f"/newsDetail_forward_{match.group(1)}")] = published
        return result

    @classmethod
    def extract_thepaper_list_items(
        cls, html: str, base_url: str
    ) -> Tuple[List[str], Dict[str, str], Dict[str, str]]:
        """Extract ordered article URLs and metadata from The Paper Next.js data."""
        if not html or "thepaper.cn" not in base_url.lower():
            return [], {}, {}
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "lxml")
            data_node = soup.find("script", id="__NEXT_DATA__")
            payload = json.loads(data_node.string or data_node.get_text()) if data_node else {}
            items = payload.get("props", {}).get("pageProps", {}).get("data", {}).get("list", [])
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return [], {}, {}

        links: List[str] = []
        dates: Dict[str, str] = {}
        titles: Dict[str, str] = {}
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            cont_id = str(item.get("contId") or "").strip()
            external_link = str(item.get("link") or "").strip()
            article_url = external_link if external_link.startswith(("http://", "https://")) else ""
            if not article_url and cont_id.isdigit():
                article_url = urljoin(base_url, f"/newsDetail_forward_{cont_id}")
            if not article_url:
                continue
            article_url = article_url.split("#", 1)[0]
            links.append(article_url)

            title = str(item.get("name") or "").strip()
            if title:
                titles[article_url] = title
            try:
                timestamp = int(item.get("pubTimeLong"))
                published = datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError, OverflowError):
                published = ""
            if published and cls._validate_date(published):
                dates[article_url] = published

        return list(dict.fromkeys(links)), dates, titles

    @classmethod
    def extract_list_item_titles(cls, html: str, base_url: str) -> Dict[str, str]:
        """提取栏目列表中网站展示的文章标题，供受限外链兜底。"""
        if not html:
            return {}
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            return {}
        result: Dict[str, str] = {}
        for anchor in soup.find_all("a", href=True):
            url = urljoin(base_url, anchor.get("href", "")).split("#", 1)[0]
            title = anchor.get_text(" ", strip=True)
            if url and title:
                result[url] = title
        return result

    @classmethod
    def _normalize_chinese_date(cls, date_str: str) -> Optional[str]:
        """规范化中文日期格式为 YYYY-MM-DD"""
        try:
            # 年月日格式
            date_str = date_str.replace('年', '-').replace('月', '-').replace('日', '')
            # 统一分隔符
            date_str = date_str.replace('/', '-')
            # 解析
            parts = date_str.split('-')
            if len(parts) >= 3:
                year = int(parts[0])
                month = int(parts[1])
                day = int(parts[2])
                return f"{year:04d}-{month:02d}-{day:02d}"
        except:
            pass
        return None


class FirecrawlClient:
    """
    Firecrawl API 客户端
    支持本地和远程两种模式
    """

    # 远程 API 默认地址
    REMOTE_BASE_URL = "https://api.firecrawl.dev/v0"
    LOCAL_BASE_URL = "http://localhost:3002"

    def __init__(self, api_key: Optional[str] = None, use_local: bool = False, local_url: Optional[str] = None):
        """
        初始化 Firecrawl 客户端

        Args:
            api_key: API Key（远程模式必需，本地模式可填 "local"）
            use_local: 是否使用本地服务
            local_url: 本地服务地址
        """
        self.use_local = use_local
        self.local_url = local_url or self.LOCAL_BASE_URL
        self.api_key = api_key or self._get_api_key()

        if self.use_local:
            self.base_url = self.local_url
        else:
            self.base_url = self.REMOTE_BASE_URL

    def _get_api_key(self) -> str:
        """获取 API Key（从环境变量）"""
        import os
        return os.environ.get("FIRECRAWL_API_KEY", "")

    def is_configured(self) -> bool:
        """
        检查客户端是否已正确配置可用。

        用于调用方决定是否跳过 Firecrawl 直接走备用链：
        - ``use_local=True`` → 本地服务不需要 key，视为已配置
        - ``use_local=False`` → 必须有非空 ``api_key`` 才视为已配置

        Returns:
            True 如果 Firecrawl 客户端可用，否则 False
        """
        if self.use_local:
            return True
        return bool(self.api_key)

    def _load_config_from_settings(self) -> None:
        """从设置中加载配置"""
        try:
            from app.api.settings import settings_store
            config = settings_store.get_firecrawl_config()
            self.use_local = config.use_local
            self.local_url = config.local_url
            self.api_key = config.api_key or self.api_key
            self.base_url = self.local_url if self.use_local else self.REMOTE_BASE_URL
        except Exception:
            pass

    async def scrape_url(self, url: str, timeout: int = 30) -> Dict[str, Any]:
        """
        爬取单个 URL

        Returns:
            Dict with keys: success, content, markdown, title, links, metadata
        """
        # 如果未指定模式，尝试从设置加载
        if self.api_key is None and self.local_url == self.LOCAL_BASE_URL:
            self._load_config_from_settings()

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # 根据本地/远程模式构造请求体
        if self.use_local:
            # 本地服务使用 v1 API
            payload = {
                "url": url,
                "formats": ["markdown", "html", "links"],
            }
            endpoint = "/v1/scrape"
        else:
            # 远程服务使用 v0 API
            payload = {
                "url": url,
                "pageOptions": {
                    "onlyMainContent": False,
                },
                "extractOptions": {
                    "mode": "markdown",
                }
            }
            endpoint = "/scrape"

        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                response = await client.post(
                    f"{self.base_url}{endpoint}",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()

                if self.use_local:
                    # 本地服务返回格式
                    if data.get("success"):
                        return {
                            "success": True,
                            "content": data.get("data", {}).get("markdown", ""),
                            "markdown": data.get("data", {}).get("markdown", ""),
                            "title": data.get("data", {}).get("metadata", {}).get("title", ""),
                            "links": data.get("data", {}).get("links", []),
                            "html": data.get("data", {}).get("html", ""),
                            "metadata": data.get("data", {}).get("metadata", {}),
                        }
                    else:
                        return {"success": False, "error": data.get("error", "Unknown error")}
                else:
                    # 远程服务返回格式
                    if data.get("success"):
                        return {
                            "success": True,
                            "content": data.get("data", {}).get("content", ""),
                            "markdown": data.get("data", {}).get("markdown", ""),
                            "title": data.get("data", {}).get("metadata", {}).get("title", ""),
                            "links": data.get("data", {}).get("links", []),
                            "html": data.get("data", {}).get("html", ""),
                            "metadata": data.get("data", {}).get("metadata", {}),
                        }
                    else:
                        return {"success": False, "error": data.get("error", "Unknown error")}
            except httpx.TimeoutException:
                return {"success": False, "error": "Request timeout"}
            except Exception as e:
                return {"success": False, "error": str(e)}

    async def scrape_batch(self, urls: List[str]) -> List[Dict[str, Any]]:
        """批量爬取"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        if self.use_local:
            # 本地服务：循环调用单个抓取
            results = []
            for url in urls:
                result = await self.scrape_url(url)
                results.append(result)
            return results
        else:
            # 远程服务：使用批量接口
            payload = {"urls": urls}
            async with httpx.AsyncClient(timeout=120) as client:
                try:
                    response = await client.post(
                        f"{self.base_url}/batch-scrape",
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    return response.json().get("data", [])
                except Exception as e:
                    logger.error(f"批量爬取失败: {e}")
                    return []


# 全局 Firecrawl 客户端
_firecrawl_client: Optional[FirecrawlClient] = None


def get_firecrawl_client(use_local: Optional[bool] = None) -> FirecrawlClient:
    """
    获取全局 Firecrawl 客户端

    Args:
        use_local: 强制指定使用本地/远程模式，None 则从设置读取
    """
    global _firecrawl_client

    # 如果有强制指定模式，重建客户端
    if use_local is not None and _firecrawl_client is not None:
        if _firecrawl_client.use_local != use_local:
            _firecrawl_client = None

    if _firecrawl_client is None:
        # 从设置中读取配置
        try:
            from app.api.settings import settings_store
            config = settings_store.get_firecrawl_config()
            _firecrawl_client = FirecrawlClient(
                api_key=config.api_key,
                use_local=config.use_local,
                local_url=config.local_url
            )
        except Exception:
            # 如果无法获取设置，使用默认值
            _firecrawl_client = FirecrawlClient()

    # 如果有强制指定模式但客户端已存在，更新它
    if use_local is not None:
        _firecrawl_client.use_local = use_local
        _firecrawl_client.base_url = _firecrawl_client.local_url if use_local else FirecrawlClient.REMOTE_BASE_URL

    return _firecrawl_client


def reset_firecrawl_client() -> None:
    """重置 Firecrawl 客户端（下次调用时会重新初始化）"""
    global _firecrawl_client
    _firecrawl_client = None


class Crawl4AIWrapper:
    """Crawl4AI 爬取包装器（备选方案）"""

    @staticmethod
    async def scrape(url: str, timeout: int = 30, cookies: str = None) -> Dict[str, Any]:
        """使用 crawl4ai 爬取网页

        Args:
            url: 目标URL
            timeout: 超时时间（秒）
            cookies: Cookie字符串，用于绕过反爬（如 "name=value; name2=value2"）
        """
        import asyncio
        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

            # 构建浏览器配置
            browser_config = BrowserConfig()

            # 构建爬取配置：等待页面加载完成后提取正文
            run_config = CrawlerRunConfig(
                # 等待页面加载
                delay_before_return_html=1.5,  # 等待1.5秒让动态内容加载
                # 滚动页面以加载更多内容
                scroll_delay=0.3,
                max_scroll_steps=5,
                # 忽略 body 可见性检查，确保获取完整内容
                ignore_body_visibility=True,
                verbose=False,
            )

            async def _do_crawl(crawler, url, config, cookies):
                """执行爬取的内部函数"""
                crawl_params = {"url": url, "config": config}
                if cookies:
                    crawl_params["cookies"] = cookies
                return await crawler.arun(**crawl_params)

            async with AsyncWebCrawler(config=browser_config, verbose=False) as crawler:
                # 使用 asyncio.wait_for 添加超时保护
                try:
                    result = await asyncio.wait_for(
                        _do_crawl(crawler, url, run_config, cookies),
                        timeout=timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Crawl4AI 爬取超时（{timeout}秒）: {url}")
                    return {"success": False, "error": f"爬取超时（{timeout}秒）"}

                if result.success:
                    # 检查是否是反爬页面
                    if result.html:
                        html_preview = result.html[:1000].lower()
                        anti_bot_keywords = ["403", "访问频率", "blocked", "forbidden", "访问过于频繁", "captcha", "验证码"]
                        if any(kw in html_preview for kw in anti_bot_keywords):
                            domain = urlparse(url).netloc
                            return {
                                "success": False,
                                "error": "anti_bot_detected",
                                "error_detail": f"网站 ({domain}) 有反爬保护，请尝试输入 Cookie",
                                "domain": domain,
                            }

                    # 提取正文内容（过滤导航）
                    markdown = result.markdown or ""
                    content = Crawl4AIWrapper._extract_main_content(markdown, url)

                    return {
                        "success": True,
                        "content": content,
                        "markdown": markdown,
                        "title": result.metadata.get("title", "") if result.metadata else "",
                        "links": result.links.get("internal", []) if result.links else [],
                        "html": result.html or "",
                        "metadata": result.metadata or {},
                    }
                else:
                    error_msg = getattr(result, 'error_message', None) or getattr(result, 'error', None) or "Crawl4AI爬取失败"
                    # 记录真实错误（之前只回退不打印，导致 80ms 内"失败"无法诊断）
                    logger.warning(
                        f"Crawl4AI 爬取失败 url={url} "
                        f"success={result.success} "
                        f"error_message={getattr(result, 'error_message', None)!r} "
                        f"html_len={len(result.html or '')} "
                        f"markdown_len={len(result.markdown or '')}"
                    )
                    # 检测反爬
                    if "403" in error_msg or "blocked" in error_msg.lower() or "forbidden" in error_msg.lower():
                        domain = urlparse(url).netloc
                        return {
                            "success": False,
                            "error": "anti_bot_detected",
                            "error_detail": f"网站 ({domain}) 有反爬保护，请尝试输入 Cookie",
                            "domain": domain,
                        }
                    return {"success": False, "error": error_msg}
        except Exception as e:
            error_str = str(e)
            if "403" in error_str or "blocked" in error_str.lower() or "forbidden" in error_str.lower():
                return {
                    "success": False,
                    "error": "anti_bot_detected",
                    "error_detail": "网站有反爬保护，请尝试输入 Cookie",
                    "domain": urlparse(url).netloc,
                }
            return {"success": False, "error": error_str}

    @staticmethod
    def _extract_main_content(markdown: str, url: str) -> str:
        """
        从 markdown 中提取正文内容，过滤导航和页头

        很多网站的页面结构是：
        - 顶部导航 (ENGLISH, 网站地图, 首页, ...)
        - Logo 和菜单
        - 中间导航
        - 文章正文（通常包含标题、发布时间、正文段落）
        - 底部导航

        本函数从 markdown 中定位并提取正文，跳过顶部的导航内容
        """
        if not markdown:
            return ""

        lines = markdown.split('\n')
        if len(lines) < 5:
            return markdown

        # 策略：从后往前或寻找正文特征来确定正文起始位置
        # 正文特征：标题（#开头）、发布时间、作者信息、来源等

        # 1. 寻找正文标记（多种格式）
        publish_markers = [
            # 时间相关
            '发布时间', '发布日期', '发表时间', '更新于', '更新时间',
            '发布时间：', '发布日期：', '发表时间：', '更新于：', '更新时间：',
            # 来源相关（重要！中科院等网站使用）
            '来源：', '来源:', '来自：', '作者：', '作者:', '责任编辑',
            '来源：', '来源:', '文章来源于',
            # 浏览量相关
            '浏览量：', '浏览量:', '阅读次数', '阅读量',
        ]
        body_start_idx = -1

        for i, line in enumerate(lines):
            stripped = line.strip()
            for marker in publish_markers:
                if marker in stripped:
                    # 找到正文标记，向前查找标题（通常是前几行）
                    body_start_idx = max(0, i - 5)  # 正文开始位置（标记前5行）
                    break
            if body_start_idx >= 0:
                break

        # 2. 如果没找到，寻找 Markdown 标题（H1-H3）
        if body_start_idx < 0:
            for i, line in enumerate(lines[:50]):  # 只在前50行查找
                stripped = line.strip()
                if stripped.startswith('# ') and len(stripped) > 5:
                    # 找到 H1 标题，检查接下来的行是否有正文特征
                    body_start_idx = i
                    break

        # 3. 如果还是没找到，寻找正文关键词
        if body_start_idx < 0:
            # 注意：中科院的来源行格式是："## \n2026年07月03日 来源： 机关党委 ..."
            # 需要处理这种格式
            for i, line in enumerate(lines):
                stripped = line.strip()
                # 检查来源格式的特征
                if '来源' in stripped and ('年' in stripped or '月' in stripped):
                    body_start_idx = max(0, i - 2)
                    break
            if body_start_idx < 0:
                body_keywords = ['正文', '内容如下', '各位', '大家好', '首先', '经过']
                for i, line in enumerate(lines[:30]):
                    stripped = line.strip()
                    # 检查是否包含正文关键词且有一定长度（排除短导航项）
                    for kw in body_keywords:
                        if kw in stripped and len(stripped) > 20:
                            body_start_idx = i
                            break
                    if body_start_idx >= 0:
                        break

        # 4. 过滤顶部导航行
        if body_start_idx < 0:
            # 没有找到正文标记，跳过前面的导航部分
            skip_patterns = [
                r'^\s*\d+\.\s*\[',  # 数字编号列表: "1. [ENGLISH](...)"
                r'^\s*\*\s*\[.*\]\(http',  # Markdown 导航: "* [首页](...)"
                r'^\s*\[!\[',  # Logo 图片: "[![](...)]"
                r'^MENU\s*Toggle',  # 移动端菜单
                r'^\s*#+\s*(?:ENGLISH|网站地图|首页|About|Contact)',  # 导航标题
            ]

            for i, line in enumerate(lines):
                is_nav = False
                for pattern in skip_patterns:
                    if re.match(pattern, line.strip(), re.IGNORECASE):
                        is_nav = True
                        break
                if not is_nav and len(line.strip()) > 30:
                    # 找到第一个较长的非导航行
                    body_start_idx = i
                    break

        # 5. 如果仍没找到，尝试找到导航和正文的分界点
        if body_start_idx < 0:
            # 统计连续短行的结束位置（导航通常是多行短列表）
            nav_end = 0
            short_line_count = 0
            for i, line in enumerate(lines):
                stripped = line.strip()
                if len(stripped) < 30 and stripped and not stripped.startswith('#'):
                    short_line_count += 1
                else:
                    if short_line_count > 5:
                        # 有超过5行短行，认为这是导航结束
                        nav_end = i
                        break
                    short_line_count = 0

            if nav_end > 0:
                body_start_idx = nav_end

        # 如果还是没有找到正文起始位置，使用整个内容
        if body_start_idx < 0:
            body_start_idx = 0

        # 6. 从正文开始位置提取内容
        content_lines = lines[body_start_idx:]

        # 7. 过滤顶部、底部的导航和版权信息
        filtered_lines = []
        skip_patterns = [
            # 底部导航和版权
            r'^[^\n]*版权[^\n]*$',
            r'^ICP备[^\n]*$',
            r'^京公网安备[^\n]*$',
            r'^[^\n]*版权所有[^\n]*$',
            r'^[^\n]*网站地图[^\n]*$',
            r'^[^\n]*联系我们[^\n]*$',
            r'^[^\n]*邮箱登录[^\n]*$',
            r'^[^\n]*English[^\n]*$',
            r'^[^\n]*PC版[^\n]*$',
            r'^[^\n]*手机版[^\n]*$',
            # 顶部面包屑导航
            r'^\[首页\]\(https?://[^\)]+\)\s*>',
            r'^当前位置[：:]?\s*>>',
            r'^>>.*\[首页\]',
            r'^\s*>>',
            r'^当前位置',
            # 无障碍工具条残留（重要！）
            r'^\s*声音开关',
            r'^\s*显示屏',
            r'^\s*帮助',
            r'^\s*读屏专用',
            r'^\s*关闭辅助工具',
            r'^\s*退出',
            r'^请按F11切换大界面模式',
            r'^\s*提示：该链接属站外链接',
            r'^\s*请注意，该操作',
            r'^\s*该网站无法启动',
            r'^\s*当前访问页面超出',
            r'^\s*无障碍辅助工具',
            r'^\s*ALT\+\d+',
            r'^\s*语音播报',
            r'^\s*【字体：大 中 小】',
            r'^\s*浏览量[：:]\s*\d+',
            r'^\s*javascript:void',
            r'^\s*是否继续访问',
            r'^\s*快捷方式',
            r'^\s*视窗区',
            r'^\s*交互区',
            r'^\s*服务区',
            r'^\s*列表区',
            r'^\s*正文区',
            r'^\s*导航区',
            r'^\s*更多分享',
            r'^\s*打印',
            r'^\s*上一篇',
            r'^\s*下一篇',
            r'^\s*分享到',
            # 栏目标题行（如 "## 工作动态"）
            r'^#{1,3}\s*(?:工作动态|党群园地|首页|科普园地|新闻中心|通知公告)',
            r'^#{1,3}\s*(?:新闻|动态|公告|简介)',
            # 附件标题
            r'^#{0,3}\s*附件[：:]?\s*$',
            # 备案信息行
            r'^备案序号[：:]',
            r'^京ICP备\d+号',
            r'^.*\. 更多\+',
            # 图片链接行（只有图片没有正文）
            r'^\s*!\[?\s*\]\(https?://[^\)]+\)\s*$',
            r'^\s*!\[\]\(https?://[^\)]+\)\s*$',
            # 空行或只有空白字符
            r'^\s*$',
        ]

        for line in content_lines:
            is_skip = False
            stripped = line.strip()

            # 如果行只包含图片链接，跳过
            if re.match(r'^\s*!\[?\s*\]\(https?://[^\)]+\)\s*$', stripped):
                is_skip = True
            elif re.match(r'^\s*!\[\]\([^)]+\)\s*$', stripped):
                is_skip = True
            else:
                for pattern in skip_patterns:
                    if re.match(pattern, stripped, re.IGNORECASE):
                        is_skip = True
                        break

            if not is_skip:
                filtered_lines.append(line)

        return '\n'.join(filtered_lines).strip()


def _extract_json_from_llm_response(response: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    从 LLM 响应文本中提取 JSON 对象。

    LLM（特别是 thinking 模型如 minimax-m27）几乎总会先输出
    ``<think>...</think>`` 思考块，并习惯用 markdown ```` ```json ```` 围栏
    包裹 JSON。原来的贪婪正则 ``r'\{[\s\S]*\}'`` 会把 think 块里
    任意 ``{}`` 也吃进去，导致 ``json.loads`` 必败。

    提取顺序：
    1. 剥掉所有 ``<think>...</think>`` 块（跨行、非贪婪）
    2. 尝试从 ```` ```json ```` 围栏中提取（兼容 `````JSON``）
    3. 围栏失败 → 在剩余文本中用栈匹配找最外层 ``{...}``（处理嵌套和字符串内的 ``{}``）
    4. 解析失败或找不到 → 返回 ``None``（不抛异常）

    Args:
        response: LLM 原始响应文本

    Returns:
        解析出的 dict；失败返回 ``None``
    """
    if not response:
        return None

    text = response

    # 1. 剥掉所有 <think>...</think> 块（跨行、非贪婪）
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    # 2. 尝试从 ```json 围栏中提取（兼容 ```JSON 大写）
    #    围栏内容里可能含换行，所以用 \s* 兼容
    fence_match = re.search(r"```(?:json|JSON)\s*\n?([\s\S]*?)\n?```", text)
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass  # 围栏内容不是合法 JSON，继续往下找

    # 3. 围栏失败 → 在剩余文本中用栈匹配找最外层 { ... }
    #    简单做法：找到第一个 { ，再扫描到匹配的 }（考虑字符串和嵌套）
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None

    return None


class WebScraper:
    """
    网页爬取引擎
    使用 Firecrawl + URL 日期提取 + LLM 摘要
    如 Firecrawl 不可用，自动回退到 Crawl4AI
    如 Crawl4AI 也不可用，使用内置的备用爬取方案
    """

    def __init__(self, cancel_event: Optional[asyncio.Event] = None, progress_callback: Optional[callable] = None):
        self._llm_service = None
        self._cancel_event = cancel_event
        self._firecrawl = get_firecrawl_client()
        self._progress_callback = progress_callback
        self._site_router = get_site_strategy_router()

    def _route_for(self, url: str, html: str = "", cookies: Optional[str] = None) -> RouteDecision:
        """Classify each URL independently; never leak one site's fallback to another."""
        return self._site_router.classify(url, html=html, cookies=cookies)

    @staticmethod
    def _strategy_result_usable(result: Optional[Dict[str, Any]]) -> bool:
        if not result or not result.get("success"):
            return False
        content = str(result.get("content") or result.get("markdown") or "").strip()
        html = str(result.get("html") or "")
        links = result.get("links") or []
        return len(content) >= 50 or len(html) >= 500 or bool(links)

    async def _run_strategy_chain(
        self,
        url: str,
        options: ScrapeOptions,
        cookies: Optional[str],
        decision: RouteDecision,
    ) -> Dict[str, Any]:
        """Execute the route's ordered strategies and preserve attempt diagnostics."""
        attempts: List[Dict[str, Any]] = []
        last_result: Dict[str, Any] = {"success": False, "error": "没有可用的爬取策略"}

        for strategy in decision.strategies:
            if self._is_cancelled():
                return {"success": False, "error": "爬取已取消"}
            if strategy == "firecrawl" and not self._firecrawl.is_configured():
                attempts.append({"strategy": strategy, "status": "skipped", "reason": "not_configured"})
                continue

            started = time.monotonic()
            try:
                if strategy == "http":
                    candidate = await self._scrape_with_alternate(url, options, cookies)
                elif strategy == "browser":
                    candidate = await Crawl4AIWrapper.scrape(
                        url, timeout=options.timeout, cookies=cookies
                    )
                elif strategy == "firecrawl":
                    candidate = await self._firecrawl.scrape_url(url, timeout=options.timeout)
                else:
                    continue
            except Exception as exc:
                candidate = {"success": False, "error": str(exc)}

            duration_ms = round((time.monotonic() - started) * 1000)
            attempts.append({
                "strategy": strategy,
                "status": "success" if candidate.get("success") else "failed",
                "duration_ms": duration_ms,
                "content_length": len(str(candidate.get("content") or candidate.get("markdown") or "")),
                "html_length": len(str(candidate.get("html") or "")),
                "link_count": len(candidate.get("links") or []),
                "error": candidate.get("error"),
            })
            last_result = candidate
            if self._strategy_result_usable(candidate):
                metadata = dict(candidate.get("metadata") or {})
                metadata["crawl_route"] = {
                    **decision.to_dict(),
                    "selected_strategy": strategy,
                    "attempts": attempts,
                }
                candidate["metadata"] = metadata
                logger.info(
                    "站点路由完成: url=%s type=%s rule=%s strategy=%s attempts=%s",
                    url,
                    decision.site_type,
                    decision.rule_name,
                    strategy,
                    len(attempts),
                )
                return candidate

        metadata = dict(last_result.get("metadata") or {})
        metadata["crawl_route"] = {
            **decision.to_dict(),
            "selected_strategy": None,
            "attempts": attempts,
        }
        last_result["metadata"] = metadata
        return last_result

    def _is_cancelled(self) -> bool:
        """检查是否已取消"""
        return self._cancel_event is not None and self._cancel_event.is_set()

    def _get_llm_service(self):
        """获取 LLM 服务"""
        if self._llm_service is None:
            from app.core.llm import llm_service
            self._llm_service = llm_service
        return self._llm_service

    def _extract_links_from_html(self, html: str, base_url: str, markdown: str = "") -> List[str]:
        """
        从 HTML 和 markdown 内容提取链接

        Args:
            html: HTML 内容
            base_url: 基础 URL
            markdown: markdown 内容（用于动态渲染的页面，如热榜类网站）
        """
        links = []

        # 1. 从 HTML 提取链接
        pattern = r'<a[^>]+href=["\']([^"\']+)["\']'
        for match in re.finditer(pattern, html):
            href = match.group(1)
            if href and not href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                if href.startswith('http'):
                    links.append(href)
                else:
                    links.append(urljoin(base_url, href))

        # 2. 从 markdown 内容提取链接（用于动态渲染页面）
        # 格式：[标题](url) 或 [标题](url "描述")
        if markdown:
            md_links = re.findall(r'\[([^\]]+)\]\((https?://[^\s\)]+)', markdown)
            for text, href in md_links:
                # 跳过无意义链接（图片、logo、空标题等）
                skip_patterns = ['logo', '图片', 'image', 'icon', 'avatar', '缩略', 'thumbnail',
                               '查看详细', '查看更多', 'more', 'icon', 'btn', 'button']
                if any(p in text.lower() for p in skip_patterns):
                    continue
                if href and len(text) > 2:  # 标题太短跳过
                    links.append(href)

        # 保持网站 DOM 顺序；后续会按候选上限截断，不能使用 set 打乱顺序。
        return list(dict.fromkeys(links))

    def _is_thepaper_url(self, url: str) -> bool:
        parsed = urlparse(url or "")
        hostname = (parsed.hostname or "").lower()
        return hostname == "thepaper.cn" or hostname.endswith(".thepaper.cn")

    def _is_thepaper_listing_url(self, url: str) -> bool:
        if not self._is_thepaper_url(url):
            return False
        return re.search(r"/(?:channel|list)_\d+/?$", urlparse(url).path, re.IGNORECASE) is not None

    def _thepaper_mobile_url(self, url: str) -> str:
        parsed = urlparse(url or "")
        match = re.search(r"/newsDetail_forward_(\d+)", parsed.path, re.IGNORECASE)
        if not match:
            return url
        return f"https://m.thepaper.cn/newsDetail_forward_{match.group(1)}"

    @staticmethod
    def _html_fragment_to_text(fragment: str) -> str:
        """Convert a trusted API HTML fragment into clean paragraph text."""
        if not fragment:
            return ""
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(fragment, "lxml")
            for tag in soup(["script", "style", "noscript", "svg", "video", "audio"]):
                tag.decompose()
            paragraphs = [
                node.get_text(" ", strip=True)
                for node in soup.find_all(["p", "section"])
                if node.get_text(" ", strip=True)
            ]
            text = "\n".join(paragraphs) if paragraphs else soup.get_text("\n", strip=True)
            return clean_content_light(text)
        except Exception:
            return clean_content_light(re.sub(r"<[^>]+>", " ", fragment))

    @staticmethod
    def _normalize_api_date(value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)) or str(value).isdigit():
            try:
                timestamp = int(value)
                if timestamp > 10_000_000_000:
                    timestamp //= 1000
                return datetime.fromtimestamp(timestamp, APP_TIMEZONE).strftime("%Y-%m-%d")
            except (OSError, OverflowError, ValueError):
                return None
        match = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", str(value))
        if not match:
            return None
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"

    @staticmethod
    def _extract_javascript_json(value: Any) -> Dict[str, Any]:
        """Read JSON wrapped as `var Name = {...};` by legacy news APIs."""
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            return {}
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            parsed = json.loads(value[start:end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    async def _scrape_known_external_fast(
        self, url: str, options: ScrapeOptions, cookies: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch public structured data used by JS-only article share pages."""
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if cookies:
            headers["Cookie"] = cookies
        timeout = max(5, min(getattr(options, "timeout", 30), 20))

        if (
            host == "content-static.cctvnews.cctv.com"
            and parsed.path.startswith("/snow-book/")
            and parse_qs(parsed.query).get("item_id")
        ):
            # This share page intentionally exposes only an app-download shell.
            # Treat it as terminal so the generic renderer chain does not spend
            # tens of seconds retrying content the publisher does not expose.
            return {
                "success": False,
                "terminal": True,
                "error": "publisher_app_only",
                "error_detail": "发布方仅在央视新闻客户端提供正文，公开分享页未提供正文内容",
            }

        people_match = re.search(r"/h5/detail/[^/]+/(\d+)", parsed.path, re.IGNORECASE)
        if host == "app.people.cn" and people_match:
            api_url = f"https://api-app.people.cn/api/v2/articles/detail/{people_match.group(1)}"
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
                response = await client.get(api_url)
                response.raise_for_status()
            payload = response.json()
            item = payload.get("item", {}) if isinstance(payload, dict) else {}
            content_html = str(item.get("content") or "")
            content = self._html_fragment_to_text(content_html)
            if payload.get("code") != 0 or len(content.replace("\n", "").replace(" ", "")) < 20:
                return {"success": False, "error": "people_api_empty"}
            published_at = self._normalize_api_date(item.get("date"))
            return {
                "success": True,
                "content": content,
                "markdown": content,
                "title": str(item.get("title") or item.get("listTitle") or "").strip(),
                "links": [],
                "html": content_html,
                "metadata": {
                    "published_at": published_at,
                    "author": str(item.get("source") or "").strip(),
                },
            }

        # 微博热搜：直接调用 AJAX API 获取数据
        if host in ("s.weibo.com", "weibo.com") and re.search(r"/top/summary", parsed.path, re.IGNORECASE):
            api_url = "https://weibo.com/ajax/side/hotSearch"
            api_headers = {
                **headers,
                "Referer": "https://s.weibo.com/top/summary",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
            }
            if not cookies:
                return {
                    "success": False,
                    "error": "anti_bot_detected",
                    "error_detail": "微博热搜需要登录 Cookie 才能访问，请在浏览器登录微博后复制 Cookie",
                    "domain": host,
                }
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=api_headers) as client:
                    response = await client.get(api_url)
                    response.raise_for_status()
                payload = response.json()
                realtime = (payload.get("data") or {}).get("realtime") or []
                if not realtime:
                    return {"success": False, "error": "weibo_api_empty", "error_detail": "微博热搜 API 返回空数据"}
                # 格式化热搜列表
                lines = ["# 微博热搜榜\n"]
                links = []
                for i, item in enumerate(realtime):
                    rank = item.get("rank", i)
                    title = str(item.get("word") or item.get("note") or "").strip()
                    num = item.get("num", 0)
                    category = str(item.get("category") or "").strip()
                    label = item.get("label_name", "")
                    if not title:
                        continue
                    entry = f"{rank + 1}. {title}"
                    if num:
                        entry += f" - 热度: {num}"
                    if category:
                        entry += f" [{category}]"
                    if label:
                        entry += f" ({label})"
                    lines.append(entry)
                    # 构建热搜话题链接（使用搜索结果页，包含实际文章）
                    word_id = item.get("word_scheme") or item.get("word")
                    if word_id:
                        # 使用搜索结果页格式，包含实际的微博文章
                        search_url = f"https://s.weibo.com/weibo?q=%23{word_id}%23"
                        links.append(search_url)
                content = "\n".join(lines)
                # 返回热搜列表和文章链接，支持深度爬取
                return {
                    "success": True,
                    "content": content,
                    "markdown": content,
                    "title": "微博热搜榜",
                    "links": links,  # 返回文章链接，支持深度爬取
                    "html": "",
                    "metadata": {
                        "published_at": current_local_date().isoformat(),
                    },
                }
            except httpx.TimeoutException:
                return {"success": False, "error": "weibo_api_timeout", "error_detail": "微博热搜 API 请求超时"}
            except Exception as e:
                logger.warning("微博热搜 API 请求失败: %s", e)
                return {"success": False, "error": f"weibo_api_error: {e}"}

        xinhua_match = re.search(r"/(?:share|detail)/(\d+)", parsed.path, re.IGNORECASE)
        if host == "h.xinhuaxmt.com" and xinhua_match:
            article_id = xinhua_match.group(1)
            request_query = ""
            timestamp = int(time.time() * 1000)
            try:
                sm3 = lambda value: hashlib.new("sm3", value.encode("utf-8")).hexdigest()
                public_key = sm3("H5")
                signature = sm3(
                    f"Key={public_key}&Timestamp={timestamp}&Token=&Request={request_query}"
                )
            except (ValueError, TypeError):
                return {"success": False, "error": "xinhua_sm3_unavailable"}
            api_headers = {
                **headers,
                "Referer": f"{parsed.scheme}://{parsed.netloc}/",
                "Timestamp": str(timestamp),
                "Signature": signature,
                "Device-Access-Id": "",
            }
            api_url = f"https://h.xinhuaxmt.com/1017/n/newsapi/h5/news-detail/{article_id}"
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=api_headers) as client:
                response = await client.get(api_url)
                response.raise_for_status()
            payload = response.json()
            item = self._extract_javascript_json(payload.get("data") if isinstance(payload, dict) else None)
            content_html = str(item.get("content") or "")
            content = self._html_fragment_to_text(content_html)
            if str(payload.get("code")) != "0" or len(content.replace("\n", "").replace(" ", "")) < 20:
                return {"success": False, "error": "xinhua_api_empty"}
            published_at = self._normalize_api_date(
                item.get("releaseTimestamp")
                or item.get("relaseDateTimeStamp")
                or item.get("releasedate")
            )
            return {
                "success": True,
                "content": content,
                "markdown": content,
                "title": str(item.get("topic") or item.get("shortTopic") or "").strip(),
                "links": [],
                "html": content_html,
                "metadata": {
                    "published_at": published_at,
                    "author": str(item.get("authors") or item.get("docSource") or "").strip(),
                },
            }

        return None

    def _extract_thepaper_detail_content(self, html: str) -> tuple[str, str, Optional[str], str]:
        """Extract The Paper detail pages without invoking a browser renderer."""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            return "", "", None, ""

        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()

        title = ""
        title_node = soup.find("h1")
        if title_node:
            title = title_node.get_text(" ", strip=True)
        if not title:
            meta_title = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "title"})
            title = meta_title.get("content", "").strip() if meta_title else ""
        if not title and soup.title:
            title = re.sub(r"[_-].*$", "", soup.title.get_text(" ", strip=True)).strip()

        text = soup.get_text("\n", strip=True)
        date_match = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\s+\d{1,2}:\d{2}", text)
        published_at = None
        if date_match:
            published_at = (
                f"{int(date_match.group(1)):04d}-"
                f"{int(date_match.group(2)):02d}-"
                f"{int(date_match.group(3)):02d}"
            )

        author = ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if title and title in lines:
            title_index = lines.index(title)
            for line in lines[title_index + 1:title_index + 5]:
                if re.search(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", line):
                    break
                if len(line) >= 2 and line not in {"下载APP", "听全文", "字号"}:
                    author = line
                    break

        selectors = [
            "article",
            ".news_part",
            ".news_txt",
            ".newsdetail_content",
            ".article_content",
            ".detail_content",
            "[class*=article]",
            "[class*=content]",
        ]
        candidates: List[str] = []
        for selector in selectors:
            for node in soup.select(selector):
                parts = [
                    p.get_text(" ", strip=True)
                    for p in node.find_all(["p", "section"])
                    if p.get_text(" ", strip=True)
                ]
                if parts:
                    candidates.append("\n".join(parts))
                else:
                    candidate = node.get_text("\n", strip=True)
                    if candidate:
                        candidates.append(candidate)

        content = max(candidates, key=len, default="")
        if len(content) < 50 and lines:
            start = 0
            if title and title in lines:
                start = lines.index(title) + 1
            body_lines = []
            noise = {
                "下载APP", "听全文", "字号", "责任编辑", "图片编辑", "我要举报",
                "特别声明", "澎湃新闻APP下载", "登录",
            }
            for line in lines[start:]:
                if any(marker in line for marker in noise):
                    if body_lines:
                        break
                    continue
                if published_at and re.search(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", line):
                    continue
                if author and line == author:
                    continue
                if len(line) <= 1:
                    continue
                body_lines.append(line)
            content = "\n".join(body_lines)

        return title, clean_content_light(content), published_at, author

    async def _scrape_thepaper_fast(self, url: str, options: ScrapeOptions, cookies: Optional[str] = None) -> Dict[str, Any]:
        parsed = urlparse(url or "")
        path = parsed.path.lower()
        if not self._is_thepaper_url(url):
            return {"success": False, "error": "not_thepaper"}

        fetch_url = self._thepaper_mobile_url(url) if "/newsdetail_forward_" in path else url
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if cookies:
            headers["Cookie"] = cookies

        timeout = max(5, min(getattr(options, "timeout", 30), 20))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            response = await client.get(fetch_url)
            response.raise_for_status()

        html = response.text or ""
        links = self._extract_links_from_html(html, url)
        if self._is_thepaper_listing_url(url):
            structured_links, _, _ = DateExtractor.extract_thepaper_list_items(html, url)
            return {
                "success": bool(html),
                "content": "",
                "markdown": "",
                "title": "",
                "links": list(dict.fromkeys(structured_links + links)),
                "html": html,
                "metadata": {},
            }

        if "/newsdetail_forward_" not in path:
            return {"success": False, "error": "unsupported_thepaper_url"}

        title, content, published_at, author = self._extract_thepaper_detail_content(html)
        if len(content.replace("\n", "").replace(" ", "")) < 20:
            return {"success": False, "error": "thepaper_fast_empty"}

        metadata: Dict[str, Any] = {}
        if published_at:
            metadata["published_at"] = published_at
        if author:
            metadata["author"] = author

        return {
            "success": True,
            "content": content,
            "markdown": content,
            "title": title,
            "links": links,
            "html": html,
            "metadata": metadata,
        }

    async def scrape(self, url: str, options: Optional[ScrapeOptions] = None) -> ScrapedResult:
        """
        爬取单个网页

        1. Firecrawl 爬取内容（优先）
        2. Crawl4AI（回退）
        3. 内置备用爬取方案（最后回退）
            4. 网站明确发布日期提取
        5. LLM 摘要提取（可选）
        """
        if options is None:
            options = ScrapeOptions()

        result = ScrapedResult(url=url)
        logger.info(f"开始爬取: {url}")

        # 获取 cookies（如果有）
        cookies = getattr(options, 'cookies', None)

        try:
            # 1. 优先使用已成功的爬取方式
            scrape_result = None
            try:
                scrape_result = await self._scrape_known_external_fast(url, options, cookies)
                if scrape_result and scrape_result.get("success"):
                    logger.info("外部新闻结构化抓取成功: %s", url)
                elif scrape_result and scrape_result.get("terminal"):
                    result.status = "access_restricted"
                    result.error_message = scrape_result.get("error_detail") or scrape_result.get("error")
                    logger.warning("外部新闻正文不可公开抓取，停止重试: %s, %s", url, result.error_message)
                    return result
                elif scrape_result and scrape_result.get("error") == "anti_bot_detected":
                    # 特定站点处理器已检测到反爬（如微博需要 Cookie），直接返回
                    result.status = "anti_bot_blocked"
                    result.error_message = scrape_result.get("error_detail", "网站有反爬保护")
                    domain = scrape_result.get("domain", "")
                    if domain:
                        result.error_message += f" ({domain})"
                    logger.warning("站点处理器检测到反爬保护: %s, %s", url, result.error_message)
                    return result
                elif scrape_result:
                    logger.warning("外部新闻结构化抓取未命中，回退通用链路: %s, %s", url, scrape_result.get("error"))
                    scrape_result = None
            except Exception as external_error:
                logger.warning("外部新闻结构化抓取异常，回退通用链路: %s, %s", url, external_error)
                scrape_result = None

            if scrape_result is None and self._is_thepaper_url(url):
                try:
                    scrape_result = await self._scrape_thepaper_fast(url, options, cookies)
                    if scrape_result.get("success"):
                        logger.info(f"澎湃快速抓取成功: {url}")
                    else:
                        logger.warning(f"澎湃快速抓取未命中，回退通用链路: {url}, {scrape_result.get('error')}")
                        scrape_result = None
                except Exception as fast_error:
                    logger.warning(f"澎湃快速抓取异常，回退通用链路: {url}, {fast_error}")
                    scrape_result = None

            if scrape_result is None:
                decision = self._route_for(url, cookies=cookies)
                logger.info(
                    "站点识别: url=%s type=%s rule=%s strategies=%s reason=%s",
                    url,
                    decision.site_type,
                    decision.rule_name,
                    ",".join(decision.strategies),
                    decision.reason,
                )
                scrape_result = await self._run_strategy_chain(url, options, cookies, decision)
            else:
                metadata = dict(scrape_result.get("metadata") or {})
                metadata.setdefault("crawl_route", {
                    "site_type": "structured_api",
                    "strategies": ["structured_adapter"],
                    "reason": "matched built-in structured adapter",
                    "rule_name": "structured-adapter",
                    "render_list_if_sparse": False,
                    "min_article_links": 1,
                    "selected_strategy": "structured_adapter",
                    "attempts": [{"strategy": "structured_adapter", "status": "success"}],
                })
                scrape_result["metadata"] = metadata

            # 4. 所有爬取方式都失败
            if not scrape_result.get("success"):
                # 检查是否是反爬错误
                if scrape_result.get("error") == "anti_bot_detected":
                    result.status = "anti_bot_blocked"
                    result.error_message = scrape_result.get("error_detail", "网站有反爬保护")
                    result.error_message += f" (域名: {scrape_result.get('domain', '')})"
                else:
                    result.status = "error"
                    result.error_message = scrape_result.get("error", "爬取失败")
                logger.error(f"爬取失败: {url}, 错误: {result.error_message}")
                return result

            # 5. 提取内容
            raw_html = scrape_result.get("html", "")
            # 优先使用已处理过的 content（经过 _extract_main_content）
            # 如果为空才使用 markdown
            processed_content = scrape_result.get("content", "")
            markdown = scrape_result.get("markdown", "")
            raw_content = processed_content.strip() if processed_content else markdown.strip()

            # 5.0 结构去重（Crawl4AI/Firecrawl 同样会触发 cas.cn TRS_UEDITOR 重复）
            # 必须在 clean_content_light 之前做，避免被混进无意义短行后失效
            if raw_content:
                from app.services.alternate_scraper import _deduplicate_duplicate_blocks
                raw_content = _deduplicate_duplicate_blocks(raw_content)

            # 5.1 检测是否需要 JavaScript 渲染（通用机制）
            # 当内容很少且页面有 JavaScript 框架标记时，尝试使用 Playwright 渲染
            if len(raw_content) < 200 and self._detect_js_rendering_needed(raw_html):
                logger.info(f"检测到页面需要 JavaScript 渲染，尝试使用 Playwright: {url}")
                rendered_html = await self._render_with_playwright(url, raw_html)
                if rendered_html and len(rendered_html) > len(raw_html):
                    # 重新提取渲染后的内容
                    raw_html = rendered_html
                    # 使用 readability 从渲染后的 HTML 提取内容
                    try:
                        from readability import Document
                        doc = Document(rendered_html)
                        rendered_content = doc.summary()
                        # 转换为纯文本
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(rendered_content, 'html.parser')
                        rendered_text = soup.get_text(separator='\n', strip=True)
                        if rendered_text and len(rendered_text) > len(raw_content):
                            raw_content = rendered_text
                            processed_content = rendered_text
                            logger.info(f"Playwright 渲染成功，内容长度: {len(rendered_text)}")
                    except Exception as e:
                        logger.warning(f"从渲染后的 HTML 提取内容失败: {e}")

            # 清理内容（如果 content 已经是处理过的，只做轻微清理）
            if processed_content:
                # 已经过正文提取，只需做轻度清理
                result.content = clean_content_light(raw_content)
            else:
                # 需要完整清理
                result.content = clean_content(raw_content)

            # 5.5 语义尾部裁剪：剥离混入正文末尾的栏目串/版权/页脚/地址电话等
            # 必须在 clean_content_light 之后做，因为它能处理已经被压成单段的混合文本。
            # 对 length < 50 的极短结果不做处理，避免误伤。
            if result.content and len(result.content) >= 50:
                stripped = strip_semantic_noise_blocks(result.content)
                if stripped and len(stripped) >= 20:
                    result.content = stripped

            result.html = raw_html

            # 尝试获取标题：先从 metadata 获取，如果没有则从内容提取
            result.title = scrape_result.get("title", "")
            if not result.title:
                result.title = extract_title_from_content(result.content)

            # Preserve links supplied by structured/renderer adapters. Dynamic pages
            # often expose only a small subset as literal HTML anchors.
            adapter_links = scrape_result.get("links", []) or []
            html_links = self._extract_links_from_html(raw_html, url, markdown)
            # Crawl4AI 返回的 links 可能是 dict 列表（每个 link 是 {href, text, ...}），需要提取 URL 字符串
            adapter_urls = []
            for link in adapter_links:
                if isinstance(link, dict):
                    adapter_urls.append(link.get("href", "") or link.get("url", ""))
                elif isinstance(link, str):
                    adapter_urls.append(link)
            result.links = list(dict.fromkeys([u for u in adapter_urls + html_links if u]))
            result.word_count = len(result.content.replace("\n", "").replace(" ", ""))

            # 6. 检测是否为列表页（通过内容判断，传入 HTML 以检测 JS 渲染特征）
            if self._is_list_page(result.content, result.links, html=result.html):
                logger.info(f"检测为列表页，跳过正文提取: {url}")
                result.content = ""
                result.word_count = 0
                result.status = "success"
                return result

            # 7. 发布日期只接受网站明确标注的字段。正文事件日期、URL
            # 归档日期和 LLM 推断均不得作为 published_at。
            fast_metadata = scrape_result.get("metadata", {}) or {}
            result.published_at = fast_metadata.get("published_at") or DateExtractor.extract_from_html(raw_html)
            _, content_author = extract_date_from_content(result.content, url)
            if result.published_at:
                logger.debug(f"网站发布日期: {result.published_at}")
            else:
                logger.info(f"详情页未找到明确发布日期: {url}")

            # 同时提取作者
            result.author = fast_metadata.get("author") or result.author
            if content_author and not result.author:
                result.author = content_author
                logger.debug(f"内容作者: {content_author}")

            # 保存元数据（包含 is_final_content 等标记）
            result.metadata = fast_metadata

            # 澎湃已有稳定的结构化解析。逐篇调用 LLM 会让后台深爬稳定撞上
            # 总超时，因此即使调用方误开 extract_metadata 也必须走本地元数据提取。
            use_llm_metadata = options.extract_metadata and not self._is_thepaper_url(url)

            # 8. 大模型提取元信息（如需要）
            if use_llm_metadata and result.content and result.word_count >= 50:
                metadata = await self._extract_metadata_with_llm(result.title, result.content, result.url)
                logger.info(f"LLM 元信息提取结果: title={len(metadata.get('title', ''))}字, summary={len(metadata.get('summary', ''))}字, keywords={len(metadata.get('keywords', []))}个")

                # 优先使用 LLM 提取的标题
                if metadata.get("title") and len(metadata.get("title", "")) > len(result.title):
                    result.title = metadata["title"]

                # 只在未从内容提取到作者时才使用 LLM 提取的
                if not result.author and metadata.get("author"):
                    result.author = metadata.get("author")

                result.summary = metadata.get("summary", "") or metadata.get("摘要", "")
                logger.info(f"设置的摘要: {result.summary[:50] if result.summary else '空'}...")

                result.keywords = metadata.get("keywords", [])

            if result.content and result.word_count >= 20:
                if not result.summary:
                    result.summary = summarize_locally(result.content)
                if not result.keywords:
                    result.keywords = extract_keywords_locally(result.title, result.content)

            # 9. 文体分析（可选，在摘要提取后进行）
            if use_llm_metadata and result.content and result.word_count >= 50 and not result.style:
                result.style = await self._extract_style_with_llm(result.title, result.content)
                if result.style:
                    logger.info(f"文体已识别: {result.style}")

            # 来源被跳过 LLM 文体分析，或模型调用失败时，必须保留本地可解释兜底。
            if not result.style and (result.title or result.content):
                from app.services.article_enrichment import infer_article_style_locally
                result.style = infer_article_style_locally(result.title, result.content)
                logger.info(f"文体使用本地规则识别: {result.style}")

            # 将摘要组合到内容前面
            if result.summary:
                result.content = format_content_with_summary(result.content, result.summary)
                logger.info("摘要已添加到内容前面")

            result.status = "success"
            logger.info(f"爬取成功: {url}, 字数: {result.word_count}, 日期: {result.published_at}")

        except Exception as e:
            result.status = "error"
            result.error_message = str(e)
            logger.error(f"爬取异常: {url}, 错误: {e}")

        return result

    async def _scrape_with_alternate(self, url: str, options: ScrapeOptions, cookies: Optional[str] = None) -> Dict[str, Any]:
        """
        使用内置备用爬取方案
        """
        from app.services.alternate_scraper import get_alternate_scraper
        
        try:
            scraper = get_alternate_scraper()
            result = await scraper.scrape(url, cookies=cookies)
            
            if result.get("success"):
                # 转换为统一格式
                return {
                    "success": True,
                    "content": result.get("content", ""),
                    "markdown": result.get("markdown", ""),
                    "title": result.get("title", ""),
                    "links": result.get("links", []),
                    "html": result.get("html", ""),
                    "metadata": {},
                }
            else:
                return result
        except Exception as e:
            logger.error(f"备用爬取方案失败: {e}")
            return {"success": False, "error": f"备用爬取方案失败: {str(e)}"}

    def save_to_database(
        self,
        result: ScrapedResult,
        category_id: Optional[str] = None,
        source_id: Optional[str] = None,
        deduplicate: bool = True,
        _lock_retries: int = 2,
    ) -> Tuple[bool, str]:
        """
        将爬取结果保存到数据库

        Args:
            result: 爬取结果
            category_id: 分类 ID
            source_id: 来源 ID
            deduplicate: 保留兼容参数；URL 重复时始终原位更新，避免来源和 KG 关联丢失

        Returns:
            Tuple[bool, str]: (是否成功, 文章ID 或 错误信息)
        """
        if result.status == "metadata_only" or LIST_METADATA_PLACEHOLDER in (result.content or ""):
            logger.warning("拒绝保存仅含栏目元数据的记录: %s", result.url)
            return False, LIST_METADATA_ONLY_ERROR

        try:
            from app.core.database import get_session_local
            from app.models.article import (
                Article, Category, ScrapeSource, Keyword, ArticleLink, ArticleKeyword
            )

            SessionLocal = get_session_local()
            db = SessionLocal()

            try:
                result.url = canonicalize_article_url(result.url)
                if result.content and result.word_count >= 20:
                    if not result.summary:
                        result.summary = summarize_locally(result.content)
                    if not result.keywords:
                        result.keywords = extract_keywords_locally(result.title, result.content)

                # 爬取源配置历史上只写入 settings.json，文章表却通过外键引用
                # PostgreSQL 的 scrape_sources。保存前补齐数据库记录，避免请求
                # 表面成功但事务因悬空 source_id 回滚。
                if category_id and not db.query(Category).filter(Category.id == category_id).first():
                    logger.warning(f"分类不存在，文章将不关联分类: {category_id}")
                    category_id = None

                if source_id and not db.query(ScrapeSource).filter(ScrapeSource.id == source_id).first():
                    from app.api.settings import settings_store
                    source_config = settings_store.scrape_sources.get(source_id)
                    if source_config:
                        source_category_id = source_config.get("category") or category_id
                        if source_category_id and not db.query(Category).filter(
                            Category.id == source_category_id
                        ).first():
                            source_category_id = None
                        db.add(ScrapeSource(
                            id=source_id,
                            name=source_config.get("name") or source_id,
                            url=source_config.get("url") or result.url,
                            category_id=source_category_id,
                            description=source_config.get("description"),
                            is_enabled=source_config.get("is_enabled", True),
                        ))
                        db.flush()
                        logger.info(f"已同步爬取源到数据库: {source_id}")
                    else:
                        logger.warning(f"爬取源不存在，文章将不关联来源: {source_id}")
                        source_id = None

                # 检查是否已存在
                existing = db.query(Article).filter(Article.url == result.url).first()

                if existing:
                    merge_scraped_result_into_article(
                        existing,
                        result,
                        category_id=category_id,
                        source_id=source_id,
                    )

                    if result.keywords:
                        db.query(ArticleKeyword).filter(
                            ArticleKeyword.article_id == existing.id
                        ).delete()

                        for kw_name in result.keywords:
                            if not kw_name or not kw_name.strip():
                                continue
                            kw_name = kw_name.strip()
                            keyword = db.query(Keyword).filter(Keyword.name == kw_name).first()
                            if not keyword:
                                keyword = Keyword(name=kw_name)
                                db.add(keyword)
                                db.flush()
                            existing.keywords.append(ArticleKeyword(keyword_id=keyword.id))

                    from app.services.article_signals import classify_article_provenance
                    classify_article_provenance(db, existing)

                    db.commit()
                    logger.info(
                        f"更新数据库文章并保留来源: id={existing.id}, "
                        f"source_id={existing.source_id}, url={existing.url[:50]}"
                    )
                    return True, existing.id

                # 创建新文章
                article = Article(
                    url=result.url,
                    title=result.title or "",
                    content=result.content or "",
                    html=result.html or "",
                    word_count=result.word_count or 0,
                    author=result.author,
                    summary=result.summary or "",
                    style=result.style,  # 文体
                    status=result.status,
                    error_message=result.error_message,
                    category_id=category_id,
                    source_id=source_id,
                )

                # 解析日期
                if result.published_at:
                    try:
                        from datetime import datetime as dt
                        article.published_at = dt.strptime(result.published_at, "%Y-%m-%d").date()
                    except (ValueError, TypeError):
                        pass

                if result.scraped_at:
                    try:
                        article.scraped_at = datetime.fromisoformat(result.scraped_at)
                    except (ValueError, TypeError):
                        pass

                article.content_hash = article.calculate_content_hash()

                # 保存链接
                if result.links:
                    for link_url in result.links:
                        article.links.append(ArticleLink(target_url=link_url))

                # 保存关键词
                if result.keywords:
                    for kw_name in result.keywords:
                        if not kw_name or not kw_name.strip():
                            continue
                        kw_name = kw_name.strip()
                        keyword = db.query(Keyword).filter(Keyword.name == kw_name).first()
                        if not keyword:
                            keyword = Keyword(name=kw_name)
                            db.add(keyword)
                            db.flush()
                        article.keywords.append(ArticleKeyword(keyword_id=keyword.id))

                db.add(article)
                db.flush()
                from app.services.article_signals import classify_article_provenance
                classify_article_provenance(db, article)
                db.commit()
                db.refresh(article)

                logger.info(f"保存到数据库文章: id={article.id}, url={article.url[:50]}")
                return True, article.id

            finally:
                db.close()

        except Exception as e:
            if "database is locked" in str(e).lower() and _lock_retries > 0:
                delay = 0.25 * (3 - _lock_retries)
                logger.warning(
                    "SQLite 写锁，%.2f 秒后重试保存（剩余 %s 次）: %s",
                    delay,
                    _lock_retries,
                    result.url,
                )
                time.sleep(delay)
                return self.save_to_database(
                    result,
                    category_id=category_id,
                    source_id=source_id,
                    deduplicate=deduplicate,
                    _lock_retries=_lock_retries - 1,
                )
            logger.error(f"保存到数据库失败: {e}")
            return False, str(e)

    def _detect_js_rendering_needed(self, html: str) -> bool:
        """
        通用检测：页面是否需要 JavaScript 渲染才能获取完整内容

        检测逻辑（基于通用特征，不针对特定网站）：
        1. 检查是否有 JavaScript 框架标记（React, Vue, Angular 等）
        2. 检查是否有异步加载的标记
        3. 检查是否有特定的内容容器（如 <div id="app">, <div id="root">）
        4. 检查是否有明显的动态加载特征

        Args:
            html: 原始 HTML 内容

        Returns:
            bool: 是否需要 JavaScript 渲染
        """
        if not html:
            return False

        # 1. 检查 JavaScript 框架标记
        js_framework_indicators = [
            # React
            r'<div[^>]*id=["\']root["\'][^>]*>',
            r'<div[^>]*id=["\']app["\'][^>]*>',
            r'reactroot',
            r'__NEXT_DATA__',
            r'_next/static',
            # Vue
            r'<div[^>]*id=["\']app["\'][^>]*>',
            r'v-cloak',
            r'v-if=',
            r'v-for=',
            # Angular
            r'<app-root[^>]*>',
            r'ng-version',
            # 其他框架
            r'data-reactroot',
            r'data-reactid',
            r'__vue__',
            r'__nuxt__',
        ]

        for pattern in js_framework_indicators:
            if re.search(pattern, html, re.IGNORECASE):
                logger.debug(f"检测到 JS 框架标记: {pattern}")
                return True

        # 2. 检查异步加载特征
        async_loading_indicators = [
            r'fetch\s*\(',
            r'XMLHttpRequest',
            r'axios\.get',
            r'axios\.post',
            r'\$\.ajax',
            r'\$\.get',
            r'\$\.post',
            r'async\s+function',
            r'await\s+fetch',
            r'loadMore',
            r'load_more',
            r'infinite.?scroll',
            r'lazy.?load',
        ]

        for pattern in async_loading_indicators:
            if re.search(pattern, html, re.IGNORECASE):
                logger.debug(f"检测到异步加载特征: {pattern}")
                return True

        # 3. 检查 JSON 数据源特征（已在 _extract_articles_from_json_source 中处理）
        json_source_indicators = [
            r'\.json\s*["\']',
            r'json\.js',
            r'data\.js',
            r'config\.js',
        ]

        for pattern in json_source_indicators:
            if re.search(pattern, html, re.IGNORECASE):
                logger.debug(f"检测到 JSON 数据源特征: {pattern}")
                return True

        # 4. 检查是否有明显的动态内容容器（内容很少但有框架标记）
        # 这些容器通常需要 JavaScript 渲染才能显示内容
        dynamic_container_indicators = [
            r'<div[^>]*class=["\'][^"\']*loading[^"\']*["\'][^>]*>',
            r'<div[^>]*class=["\'][^"\']*skeleton[^"\']*["\'][^>]*>',
            r'<div[^>]*class=["\'][^"\']*placeholder[^"\']*["\'][^>]*>',
            r'<div[^>]*class=["\'][^"\']*spinner[^"\']*["\'][^>]*>',
            r'正在加载',
            r'Loading\.\.\.',
            r'请稍候',
        ]

        for pattern in dynamic_container_indicators:
            if re.search(pattern, html, re.IGNORECASE):
                logger.debug(f"检测到动态内容容器: {pattern}")
                return True

        return False

    async def _render_list_page_for_links(self, url: str, html: str) -> str:
        """
        为 cas.cn 等网站专门渲染列表页以获取动态加载的文章链接

        Args:
            url: 页面 URL
            html: 原始 HTML（可能不包含真实文章链接）

        Returns:
            str: 渲染后的 HTML，如果失败则返回空字符串
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("Playwright 未安装，无法渲染列表页")
            return ""

        try:
            logger.info(f"渲染列表页获取文章链接: {url}")
            async with async_playwright() as p:
                browser = None
                try:
                    browser = await p.chromium.launch(channel="chrome", headless=True)
                except Exception as exc:
                    logger.warning("系统 Chrome 启动失败，回退到 Playwright Chromium: %s", exc)
                    browser = await p.chromium.launch(headless=True)

                try:
                    page = await browser.new_page()
                    page.set_default_timeout(30000)

                    # 访问页面并等待网络空闲
                    await page.goto(url, wait_until="networkidle", timeout=45000)

                    # 等待一些动态内容加载
                    await page.wait_for_timeout(3000)

                    # 返回渲染后的 HTML
                    rendered_html = await page.content()
                    logger.info(f"列表页渲染成功，HTML 长度: {len(rendered_html)}")
                    return rendered_html

                finally:
                    if page:
                        await page.close()
        except Exception as e:
            logger.warning(f"列表页渲染失败: {e}")
            return ""

    async def _render_with_playwright(self, url: str, html: str) -> str:
        """
        通用机制：使用 Playwright 渲染 JavaScript 页面

        当检测到页面需要 JavaScript 渲染时，使用 Playwright 执行 JavaScript 并等待页面加载完成，
        然后返回渲染后的 HTML 内容。

        Args:
            url: 页面 URL
            html: 原始 HTML 内容

        Returns:
            str: 渲染后的 HTML 内容，如果失败则返回原始 HTML
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("Playwright 未安装，无法渲染 JavaScript 页面")
            return html

        try:
            logger.info(f"使用 Playwright 渲染页面: {url}")
            async with async_playwright() as p:
                # 优先使用系统 Chrome，失败则回退到 Playwright Chromium
                browser = None
                try:
                    browser = await p.chromium.launch(
                        channel="chrome",
                        headless=True,
                    )
                    logger.debug("系统 Chrome 启动成功")
                except Exception as exc:
                    logger.warning("系统 Chrome 启动失败，回退到 Playwright Chromium: %s", exc)
                    browser = await p.chromium.launch(headless=True)
                    logger.debug("Playwright Chromium 启动成功")

                try:
                    page = await browser.new_page()

                    # 设置合理的超时时间
                    page.set_default_timeout(30000)  # 30 秒

                    # 访问页面并等待网络空闲
                    await page.goto(url, wait_until="networkidle", timeout=30000)

                    # 等待页面内容加载（通用策略）
                    # 等待 body 中有足够内容
                    await page.wait_for_function(
                        "document.body && document.body.innerText.length > 100",
                        timeout=10000
                    )

                    # 获取渲染后的 HTML
                    rendered_html = await page.content()

                    logger.info(f"Playwright 渲染成功，HTML 长度: {len(rendered_html)}")
                    return rendered_html

                finally:
                    await browser.close()

        except Exception as e:
            logger.error(f"Playwright 渲染失败: {e}")
            return html

    def _is_list_page(self, content: str, links: List[str], html: str = "") -> bool:
        """
        判断是否为列表页/导航页
        （优化版：更宽松的判断，避免误删有价值的内容）

        导航页特征：
        1. 内容主要是 "* 栏目名" 格式的列表
        2. 内容是机构介绍/组织架构/栏目导航
        3. 缺乏完整的文章句子结构
        4. 包含大量栏目名称关键词
        """
        if not content:
            return True

        lines = [l.strip() for l in content.split('\n') if l.strip()]

        # 如果内容长度超过 300 字符，优先认为是文章页（保守策略）
        if len(content) >= 300:
            logger.debug(f"内容足够长 ({len(content)} 字)，认为是文章页")
            return False

        # 内容很少时，检查是否是 JavaScript 渲染的页面
        # 如果是 JS 渲染的页面，不应该误判为列表页
        if len(content) < 200 or len(lines) < 2:
            # 检查是否有 JavaScript 渲染特征
            if html and self._detect_js_rendering_needed(html):
                logger.debug(f"内容很少但检测到 JS 渲染特征，不认为是列表页: {len(content)} 字")
                return False
            # 内容非常少且没有 JS 渲染特征，才认为是列表页
            if len(content) < 80:
                return True

        # 检测导航关键词密度
        nav_keywords = [
            '历史沿革', '园区概况', '组织机构', '科研部门', '管理部门', '支撑部门',
            '学术委员会', '研究队伍', '院士专家', '研究员', '正高级', '成果展示',
            '教育培养', '教育处', '招生管理', '培养与学位', '首页', '网站', '联系',
            '工作动态', '综合新闻', '通知公告', '党建', '党政', '新闻', '更多',
            '党群园地', '党群工作', '机构设置', '职能机构', '直属机构',
            '成果转化', '知识产权', '人才教育', '教育简介', '主要职责', '办院方针',
            '院况简介', '科技奖励', '科技期刊', '科技专项', '科研进展',
            '地理位置', '交通路线', '邮箱登录', '网站地图', 'English', 'PC 版',
            '当前位置', '您现在的位置', '首页', '设为首页', '加入收藏',
        ]

        nav_line_count = 0
        list_item_count = 0
        # 统计正文段落行（包含多个句子或较长内容的行）
        paragraph_lines = 0

        for line in lines:
            # 统计列表项格式的行 (* 或 - 开头)
            if line.startswith('* ') or line.startswith('- '):
                # 只有短列表项（导航项）才算，长内容前的列表不算
                if len(line) < 30:
                    list_item_count += 1
                continue

            # 检测是否包含导航关键词
            is_nav = False
            for kw in nav_keywords:
                if kw in line:
                    nav_line_count += 1
                    is_nav = True
                    break

            # 统计正文段落行（有句子结构的行）
            if not is_nav:
                sentence_count = line.count('。') + line.count('.') + line.count('！') + line.count('？')
                if sentence_count >= 2 or len(line) > 100:
                    paragraph_lines += 1

        total_lines = len(lines)

        # 条件 0: 如果有正文段落，认为是文章
        if paragraph_lines >= 3:
            logger.debug(f"检测为文章页 (正文段落): {paragraph_lines}/{total_lines}")
            return False

        # 条件 1: 如果有大量列表项格式的行，且占比超过 60%，才认为是导航页
        if total_lines >= 8 and list_item_count / total_lines > 0.6:
            logger.info(f"检测为导航页 (列表项): {list_item_count}/{total_lines}")
            return True

        # 条件 2: 如果导航关键词行占比超过 60%，才认为是导航页
        if total_lines >= 8 and nav_line_count / total_lines > 0.6:
            logger.info(f"检测为导航页 (关键词): {nav_line_count}/{total_lines}")
            return True

        # 条件 3: 内容没有完整的句子结构（句号少）
        sentence_markers = content.count('。') + content.count('!') + content.count('？')
        if len(content) > 200 and sentence_markers < 1:
            logger.info(f"检测为导航页 (句子少): {sentence_markers} 个句子标记")
            return True

        # 条件 4: 如果内容都是短行（每行<30 字）且行数多，可能是列表
        short_lines = [l for l in lines if len(l) < 30]
        if len(lines) > 15 and len(short_lines) / len(lines) > 0.9:
            logger.info(f"检测为导航页 (短行多): {len(short_lines)}/{len(lines)}")
            return True

        # 条件 5: 检测是否有"* xxx" 格式的导航列表块，且这些行包含大量机构栏目词
        # 这种情况专门处理"【摘要】xxx\n【正文】\n* 栏目 1\n* 栏目 2\n..."的格式
        star_nav_lines = []
        for line in lines:
            if line.startswith('* '):
                # 检查是否是栏目名称格式（短，包含导航词）
                stripped = line[2:].strip()  # 去掉 "* "
                if len(stripped) < 20:  # 栏目名通常较短
                    is_nav_item = False
                    for kw in nav_keywords:
                        if kw in stripped:
                            is_nav_item = True
                            break
                    if is_nav_item:
                        star_nav_lines.append(line)

        # 如果有 8 个以上的导航列表项，才认为是导航页
        if len(star_nav_lines) >= 8:
            logger.info(f"检测为导航页 (栏目列表): {len(star_nav_lines)} 个栏目项")
            return True

        # 有足够多的句子标记，认为是文章页
        if sentence_markers >= 2:
            return False

        # 默认：不轻易认为是列表页，保留内容
        return False

    async def _extract_articles_from_json_source(self, html: str, base_url: str) -> List[Dict[str, str]]:
        """
        通用机制：从页面的 JavaScript 代码中自动检测并提取 JSON 数据源

        很多网站（如 gov.cn）使用 JavaScript 动态加载文章列表，实际数据来自 JSON 文件。
        这个方法会分析 HTML 中的 JavaScript 代码，自动识别 JSON 数据源的 URL 模式。

        Args:
            html: 列表页 HTML 内容
            base_url: 列表页 URL

        Returns:
            List[Dict]: 文章列表，每项包含 {title, url, date}
        """
        articles = []
        parsed = urlparse(base_url)

        logger.info(f"开始检测 JSON 数据源，HTML 长度: {len(html)}, URL: {base_url}")

        # 检查 HTML 中是否包含 .json 关键词
        if '.json' not in html:
            logger.info("HTML 中不包含 .json 关键词，跳过 JSON 数据源检测")
            return articles

        logger.info("HTML 中包含 .json 关键词，继续检测...")

        # 常见的 JSON 数据源 URL 模式
        json_url_patterns = [
            # 模式 0: 直接引用 ./xxx.json 或 '../xxx.json"（gov.cn 等政府网站常用格式）
            r'["\'](\.{1,2}/[^"\']+\.json)["\']',
            # 模式 1: $.ajax({url: "./data.json"}) 或 fetch("./data.json")
            r'(?:url|fetch)\s*[:=]\s*["\'](\./[^"\']+\.json)["\']',
            # 模式 2: url = "./data.json" 或 url = '../data.json"
            r'url\s*=\s*["\'](\.{1,2}/[^"\']+\.json)["\']',
            # 模式 3: var dataUrl = "/api/data.json"
            r'(?:dataUrl|jsonUrl|listUrl)\s*=\s*["\'](/[^"\']+\.json)["\']',
            # 模式 4: 绝对路径 JSON
            r'(?:url|fetch)\s*[:=]\s*["\'](https?://[^"\']+\.json)["\']',
        ]

        json_url = None
        for i, pattern in enumerate(json_url_patterns):
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                json_path = match.group(1)
                logger.info(f"匹配到模式 {i+1}: {json_path}")
                # 构建完整的 JSON URL
                if json_path.startswith('./'):
                    # 相对路径，基于当前页面的目录
                    base_dir = base_url.rsplit('/', 1)[0]
                    json_url = f"{base_dir}/{json_path[2:]}"
                elif json_path.startswith('../'):
                    # 上级目录相对路径
                    base_dir = base_url.rsplit('/', 2)[0]
                    json_url = f"{base_dir}/{json_path[3:]}"
                elif json_path.startswith('/'):
                    # 绝对路径
                    json_url = f"{parsed.scheme}://{parsed.netloc}{json_path}"
                elif json_path.startswith('http'):
                    # 完整 URL
                    json_url = json_path
                else:
                    # 其他相对路径
                    json_url = f"{base_url.rsplit('/', 1)[0]}/{json_path}"
                break

        if not json_url:
            logger.info("未检测到 JSON 数据源模式")
            return articles

        logger.info(f"检测到 JSON 数据源: {json_url}")

        try:
            # 获取 JSON 数据
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(json_url)
                response.raise_for_status()
                json_data = response.json()

            if not isinstance(json_data, list):
                logger.warning(f"JSON 数据格式不正确: {type(json_data)}")
                return articles

            # 自动识别 JSON 字段映射
            # 常见的字段名模式
            url_fields = ['URL', 'url', 'link', 'href', 'articleUrl', 'detail_url']
            title_fields = ['TITLE', 'title', 'name', 'headline', 'articleTitle']
            date_fields = ['DOCRELPUBTIME', 'DOCRELDATE', 'published_at', 'publishDate', 'date', 'createTime', 'pubTime']

            # 找到第一个非空的字段映射
            url_field = next((f for f in url_fields if f in json_data[0]), None)
            title_field = next((f for f in title_fields if f in json_data[0]), None)
            date_field = next((f for f in date_fields if f in json_data[0]), None)

            if not url_field:
                logger.warning(f"JSON 数据中未找到 URL 字段，可用字段: {list(json_data[0].keys())}")
                return articles

            # 提取文章信息
            for item in json_data:
                if not isinstance(item, dict):
                    continue

                url = str(item.get(url_field, '')).strip()
                title = str(item.get(title_field, '')).strip() if title_field else ''
                date_str = str(item.get(date_field, '')).strip() if date_field else ''

                if not url:
                    continue

                # 确保 URL 是完整的
                if url.startswith('/'):
                    url = f"{parsed.scheme}://{parsed.netloc}{url}"
                elif not url.startswith('http'):
                    url = f"{base_url.rsplit('/', 1)[0]}/{url}"

                articles.append({
                    'url': url,
                    'title': title,
                    'date': date_str
                })

            logger.info(f"从 JSON 数据源提取到 {len(articles)} 篇文章")

        except Exception as e:
            logger.error(f"获取 JSON 数据失败: {e}")

        return articles

    def extract_list_page_articles(self, content: str, links: List[str], base_url: str) -> List[Dict[str, str]]:
        """
        从列表页提取文章信息

        Args:
            content: 页面内容（markdown 格式）
            links: 链接列表
            base_url: 基础 URL

        Returns:
            List[Dict]: 文章列表，每项包含 {title, url, date}
        """
        articles = []
        seen_urls = set()

        # 从链接中提取文章
        for link in links:
            if link in seen_urls:
                continue

            # 过滤：只保留文章链接（排除首页、列表页、图片等）
            if any(ext in link.lower() for ext in ['.jpg', '.png', '.gif', '.pdf']):
                continue

            # 检查是否是文章 URL（通常包含日期或特定模式）
            is_article = False

            # 模式 1: URL 包含日期格式（支持 /YYYY-MM-DD/、/YYYYMMDD/、文件名中的 8 位日期）
            if re.search(r'/\d{4}[-/]\d{1,2}[-/]\d{1,2}/', link) or re.search(r'(?:\d{8})(?:/|\.)', link):
                is_article = True

            # 模式 2: URL 包含 /tYYYYMMDD_ 或 .shtml .htm 等
            if '/t' in link and re.search(r't\d{8}', link):
                is_article = True
            if not is_article and link.endswith(('.shtml', '.htm', '.html')):
                # 检查文件名是否包含 content_ 或 8 位日期（gov.cn 常见格式）
                if re.search(r'/content_[a-zA-Z0-9_]+\.(?:html?|shtml)$', urlparse(link).path, re.IGNORECASE):
                    is_article = True
                elif re.search(r'\d{8}', link):  # 文件名含 8 位数字
                    is_article = True

            # 模式 3: URL 是新闻/文章路径
            if any(p in link for p in ['/yw/', '/news/', '/article/', '/content/', '/info/']):
                is_article = True

            # 模式 4: 文件名包含 content_ 的 .htm 文件（gov.cn 文章特征）
            if not is_article and re.search(r'/content_[\w]+\.(?:html?|shtml)$', urlparse(link).path, re.IGNORECASE):
                is_article = True

            # 模式 4: 从链接文本提取标题（如果有）
            if is_article and link not in seen_urls:
                seen_urls.add(link)

                # 从 URL 提取日期
                date_str = ""
                date_match = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', link)
                if date_match:
                    date_str = f"{date_match.group(1)}-{date_match.group(2).zfill(2)}-{date_match.group(3).zfill(2)}"
                else:
                    # 从文件名中提取 8 位日期：content_2024080101.htm → 2024-08-01
                    date_match = re.search(r'(\d{8})', link)
                    if date_match:
                        d = date_match.group(1)
                        # 验证是有效日期
                        try:
                            datetime.strptime(d, "%Y%m%d")
                            date_str = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                        except ValueError:
                            # 尝试 6 位格式 YYYYMM
                            date_match6 = re.search(r'(\d{6})', link)
                            if date_match6:
                                d6 = date_match6.group(1)
                                try:
                                    datetime.strptime(d6 + "01", "%Y%m%d")
                                    date_str = f"{d6[:4]}-{d6[4:6]}-01"
                                except ValueError:
                                    date_str = ""

                articles.append({
                    'url': link,
                    'title': '',  # 需要进一步爬取才能获取
                    'date': date_str
                })

        return articles[:20]  # 限制返回数量

    async def scrape_list_page_and_articles(
        self,
        list_url: str,
        max_articles: int = 10,
        save_to_db: bool = True,
        category_id: Optional[str] = None,
        source_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        爬取列表页并自动抓取文章详细内容

        Args:
            list_url: 列表页 URL
            max_articles: 最大爬取文章数
            save_to_db: 是否保存到数据库
            category_id: 分类 ID
            source_id: 来源 ID

        Returns:
            Dict: {scraped_count, articles: [...]}
        """
        from app.core.database import get_session_local
        from app.models.article import Article

        # 1. 爬取列表页
        list_result = await self.scrape(list_url)
        if list_result.status != "success":
            return {"status": "error", "message": f"列表页爬取失败：{list_result.error_message}"}

        # 2. 尝试从 JSON 数据源提取文章（通用机制）
        articles = await self._extract_articles_from_json_source(list_result.html or "", list_url)

        # 3. 如果 JSON 数据源没有结果，从 HTML 中提取链接
        if not articles:
            articles = self.extract_list_page_articles(list_result.content, list_result.links, list_url)
        if not articles:
            return {"status": "error", "message": "未找到文章链接"}

        # 3. 限制数量
        articles = articles[:max_articles]

        scraped_count = 0
        scraped_articles = []

        # 4. 逐个爬取文章详情
        for i, article_info in enumerate(articles):
            if self._is_cancelled():
                break

            article_url = article_info.get("url", "")
            if not article_url:
                continue

            # 爬取文章详情
            article_result = await self.scrape(article_url)
            if article_result.status == "success":
                # 保存数据库
                if save_to_db:
                    saved, article_id = self.save_to_database(
                        article_result,
                        category_id=category_id,
                        source_id=source_id
                    )
                    if saved:
                        scraped_count += 1
                        scraped_articles.append({
                            "url": article_url,
                            "title": article_result.title,
                            "db_id": article_id
                        })
                else:
                    scraped_count += 1
                    scraped_articles.append({
                        "url": article_url,
                        "title": article_result.title,
                        "content_length": len(article_result.content)
                    })

        return {
            "status": "success",
            "scraped_count": scraped_count,
            "articles": scraped_articles
        }

    async def _extract_metadata_with_llm(self, title: str, content: str, url: str = "") -> Dict[str, Any]:
        """使用大模型提取元信息，包括标题、摘要和文体"""
        try:
            llm = self._get_llm_service()

            # 准备提示词 - 始终包含标题和内容以提取摘要
            content_preview = content[:5000]  # 限制内容长度

            prompt = f"""你是一位专业的内容分析师。请分析以下文章，提取关键信息。

文章标题：{title if title else "（未找到标题）"}

文章内容：
{content_preview}

请仔细阅读文章内容，提取以下信息：

1. **标题**：如果原文没有明确标题，请根据文章主题提取一个简洁的标题（不超过40个字符）
2. **作者/来源**：文章的作者或发布来源（如有）
3. **发布日期**：如果文中提到，格式为 YYYY-MM-DD；如未提及则返回空字符串
4. **摘要**：用100-150字概括文章的核心内容，包括主题、核心观点和结论（非常重要！）
5. **关键词**：提取3-5个最重要的关键词（用逗号分隔）

请以JSON格式返回结果，确保JSON格式完全正确：
{{"title":"","author":"","published_at":"","summary":"","keywords":[]}}

只返回JSON，不要添加任何解释或其他内容。"""

            response = await llm.non_stream_chat(
                model_id="",
                messages=[{"role": "user", "content": prompt}],
            )

            logger.info(f"LLM 原始响应: {response[:500]}...")

            if response and not response.startswith("[错误]"):
                # 提取 JSON（处理 <think> 块 + ```json 围栏）
                data = _extract_json_from_llm_response(response)
                if data:
                    keywords = _normalize_keywords(data.get("keywords", []))
                    if not keywords:
                        keywords = extract_keywords_locally(title, content)

                    summary = data.get("summary", "") or data.get("摘要", "")
                    if not summary:
                        summary = summarize_locally(content)

                    return {
                        "title": data.get("title", title) if title or not data.get("title") else data.get("title"),
                        "published_at": data.get("published_at", ""),
                        "author": data.get("author", ""),
                        "summary": summary,
                        "keywords": keywords,
                    }
        except Exception as e:
            logger.error(f"LLM 元信息提取失败: {e}")

        return {
            "title": title,
            "published_at": "",
            "author": "",
            "summary": summarize_locally(content),
            "keywords": extract_keywords_locally(title, content),
        }

    async def _extract_style_with_llm(self, title: str, content: str) -> Optional[str]:
        """使用大模型分析文章文体类型"""
        try:
            llm = self._get_llm_service()
            content_preview = content[:3000]  # 限制内容长度

            prompt = f"""你是一位专业的文档分类专家。请分析以下文章的文体类型。

文章标题：{title if title else "（未找到标题）"}

文章内容：
{content_preview}

请根据文章的内容、格式和语言风格，判断这篇文档属于以下哪种文体类型（请选择一个最合适的）：

1. **新闻报道** - 报道事件、活动、会议等动态新闻
2. **通知公告** - 政府或单位的行政通知、政策文件、招标公告等
3. **会议纪要** - 记录会议内容、讨论事项、决议等
4. **领导讲话** - 领导人在会议、活动上的发言稿、致辞
5. **工作简报** - 工作总结、工作动态、工作进展报告
6. **政策解读** - 对政策、法规的解读和分析
7. **专题文章** - 深度分析、研究报告、专题论述
8. **行业动态** - 行业发展趋势、市场分析、行业新闻
9. **其他** - 不属于上述类型的其他文章

请以JSON格式返回结果：
{{"style":"文体类型","confidence":"高/中/低","reason":"简要说明判断理由"}}

只返回JSON，不要添加任何解释或其他内容。"""

            response = await llm.non_stream_chat(
                model_id="",
                messages=[{"role": "user", "content": prompt}],
            )

            logger.info(f"LLM 文体分析响应: {response[:300]}...")

            if response and not response.startswith("[错误]"):
                # 提取 JSON（处理 <think> 块 + ```json 围栏）
                data = _extract_json_from_llm_response(response)
                if data:
                    style = data.get("style", "")
                    if style:
                        logger.info(f"文体分析结果: {style}")
                        return style

        except Exception as e:
            logger.error(f"LLM 文体分析失败: {e}")

        return None

    async def scrape_batch(
        self,
        urls: List[str],
        options: Optional[ScrapeOptions] = None,
        max_concurrent: int = 3
    ) -> List[ScrapedResult]:
        """批量爬取"""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def scrape_with_limit(url: str) -> ScrapedResult:
            async with semaphore:
                return await self.scrape(url, options)

        tasks = [scrape_with_limit(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        return [
            r if not isinstance(r, Exception) else ScrapedResult(url=urls[i], status="error", error_message=str(r))
            for i, r in enumerate(results)
        ]

    async def deep_scrape(
        self,
        url: str,
        options: Optional[ScrapeOptions] = None,
        max_articles: int = 10,
        date_range: Optional[str] = None,
        custom_date_range: Optional[dict] = None,
        scrape_level: Optional[str] = "deep",
        scrape_id: Optional[str] = None,
        progress_callback: Optional[callable] = None
    ) -> tuple[ScrapedResult, List[ScrapedResult]]:
        """
        深度爬取：从列表页识别文章链接，爬取文章内容
        """
        import uuid
        if scrape_id is None:
            scrape_id = str(uuid.uuid4())[:8]

        logger.info(f"[DEEP_SCRAPE] 深度爬取开始 | URL: {url} | 日期范围: {date_range or custom_date_range}")

        if options is None:
            options = ScrapeOptions()

        # 1. 爬取列表页
        logger.info(f"解析列表页: {url}")
        list_page = await self.scrape(url, options)
        list_page.title = list_page.title or "列表页"

        if list_page.status != "success":
            return list_page, []

        # 检查是否为最终内容（如微博热搜等聚合页面）
        # 这类页面的内容本身就是最终结果，不需要进一步爬取文章
        logger.info(f"检查是否为最终内容: metadata={list_page.metadata}")
        if list_page.metadata and list_page.metadata.get("is_final_content"):
            logger.info(f"检测到最终内容页面，直接返回: {url}")
            return list_page, [list_page]

        # 列表页显示日期仅作为详情页明确发布日期缺失时的站点级兜底。
        list_item_dates = DateExtractor.extract_list_item_dates(list_page.html, url)
        list_item_titles = DateExtractor.extract_list_item_titles(list_page.html, url)
        is_thepaper_listing = self._is_thepaper_listing_url(url)
        if is_thepaper_listing:
            list_item_dates.update(
                DateExtractor.extract_thepaper_list_dates(list_page.html, url)
            )
            _, structured_dates, structured_titles = DateExtractor.extract_thepaper_list_items(
                list_page.html, url
            )
            list_item_dates.update(structured_dates)
            list_item_titles.update(structured_titles)

        # 2. 识别文章链接
        # 2.1 先尝试从 JSON 数据源提取（通用机制，支持 gov.cn 等动态加载网站）
        json_articles = await self._extract_articles_from_json_source(list_page.html or "", url)

        if json_articles:
            # 从 JSON 数据源提取成功
            article_links = [a['url'] for a in json_articles if a.get('url')]
            logger.info(f"从 JSON 数据源提取到 {len(article_links)} 个文章链接")
        else:
            # 先尝试通用 URL 模式过滤
            article_links = self._filter_article_links(
                list_page.links, url, trusted_list_urls=set(list_item_dates)
            )

            # 稀疏列表由站点类型决定是否浏览器渲染，不再对单一域名写死。
            route_decision = self._route_for(url, html=list_page.html or "", cookies=options.cookies)
            sparse_threshold = route_decision.min_article_links
            should_render_sparse_list = (
                len(article_links) < sparse_threshold
                and (
                    route_decision.render_list_if_sparse
                    or self._detect_js_rendering_needed(list_page.html or "")
                )
            )
            if should_render_sparse_list:
                original_article_count = len(article_links)
                logger.info(
                    "稀疏列表触发浏览器渲染: url=%s type=%s links=%s threshold=%s rule=%s",
                    url,
                    route_decision.site_type,
                    len(article_links),
                    sparse_threshold,
                    route_decision.rule_name,
                )
                rendered_html = await self._render_list_page_for_links(url, list_page.html)
                if rendered_html:
                    rendered_dates = DateExtractor.extract_list_item_dates(rendered_html, url)
                    rendered_titles = DateExtractor.extract_list_item_titles(rendered_html, url)
                    list_item_dates.update(rendered_dates)
                    list_item_titles.update(rendered_titles)
                    rendered_links = self._extract_links_from_html(rendered_html, url)
                    rendered_articles = self._filter_article_links(
                        rendered_links,
                        url,
                        trusted_list_urls=set(list_item_dates),
                    )
                    if len(rendered_articles) > len(article_links):
                        article_links = rendered_articles
                        list_page.html = rendered_html
                    logger.info(
                        "浏览器渲染列表结果: 原候选=%s，渲染候选=%s",
                        original_article_count,
                        len(rendered_articles),
                    )

            # 去重
            article_links = list(dict.fromkeys(article_links))

        # 使用回调更新进度
        cb = progress_callback or self._progress_callback
        if cb:
            cb(2, "正在爬取文章", f"识别到 {len(article_links)} 个链接，开始爬取...")

        logger.info(f"识别到 {len(article_links)} 个文章链接")

        if not article_links:
            return list_page, []

        # 3. 计算日期范围。URL 中的日期可能是建页/更新/归档时间，
        # 不能据此提前丢弃文章；正文抓取后再按 published_at 最终过滤。
        if date_range or custom_date_range:
            today = current_local_date()
            if date_range in ("today", "1d"):
                start_date, end_date = today, today
            elif date_range in ("week", "7d"):
                # 含今天在内共 7 个自然日。
                start_date, end_date = today - timedelta(days=6), today
            elif date_range in ("month", "30d"):
                # 含今天在内共 30 个自然日。
                start_date, end_date = today - timedelta(days=29), today
            elif custom_date_range:
                start_date = custom_date_range.get("start_date") or date(2000, 1, 1)
                end_date = custom_date_range.get("end_date") or today
            else:
                start_date, end_date = None, None

            if start_date and end_date:
                before_count = len(article_links)
                if list_item_dates:
                    article_links = self._prefilter_links_by_list_dates(
                        article_links,
                        list_item_dates,
                        start_date,
                        end_date,
                    )
                logger.info(
                    f"网站列表发布日期预筛: {before_count} -> {len(article_links)} 篇 "
                    f"[{start_date} ~ {end_date}]"
                )

        if is_thepaper_listing:
            # 澎湃列表日期可信且详情解析稳定，不需要额外抓取 2 倍候选后再截断。
            article_links = article_links[:max_articles]
        else:
            # 必须在日期预筛之后限制候选，避免排在前 2N 之外的有效日期文章
            # 尚未参与筛选就被提前丢弃。
            article_links = article_links[:max_articles * 2]

        # 3. 批量爬取文章（逐个爬取并更新进度）
        if not article_links:
            logger.info("⚠️ 没有符合日期条件的文章链接，请调整日期范围")
            return list_page, []

        logger.info(f"开始爬取 {len(article_links)} 篇文章")
        article_results = []
        for i, article_url in enumerate(article_links):
            if self._is_cancelled():
                logger.info("爬取已取消")
                break
            result = await self.scrape(article_url, options)
            if not result.published_at:
                list_date = list_item_dates.get(article_url.split("#", 1)[0])
                if list_date:
                    result.published_at = list_date
                    logger.info(f"使用列表页发布日期: {article_url} -> {list_date}")
            # 受限外链只保留列表页可验证元数据，不得伪造成抓取成功的正文。
            list_key = article_url.split("#", 1)[0]
            list_title = list_item_titles.get(list_key)
            if list_item_dates.get(list_key) and list_title and (
                result.status != "success" or result.word_count <= 0
            ):
                mark_result_as_metadata_only(result, list_title)
                logger.warning(f"详情页不允许公开爬取，未保存到文档管理: {article_url}")
            article_results.append(result)
            # 每爬取一篇更新一次进度
            if cb:
                cb(3, f"正在爬取 ({i+1}/{len(article_links)})", f"已爬取 {len(article_results)} 篇", current=i+1, total=len(article_links))

        # 4. 处理结果
        valid_results = [r for r in article_results if r.status == "success" and r.word_count > 0]
        metadata_only_results = [r for r in article_results if r.status == "metadata_only"]
        output_results = valid_results + metadata_only_results
        logger.info(
            "文章结果: 完整正文 %s 篇，仅栏目元数据 %s 篇",
            len(valid_results),
            len(metadata_only_results),
        )

        if (date_range or custom_date_range) and start_date and end_date:
            if is_thepaper_listing:
                for item in output_results:
                    if not item.published_at:
                        item.published_at = list_item_dates.get(item.url.split("#", 1)[0])
                        if not item.published_at:
                            match = re.search(r'newsDetail_forward_(\d+)', item.url)
                            if match:
                                item.published_at = list_item_dates.get(
                                    urljoin(url, f"/newsDetail_forward_{match.group(1)}")
                                )
            before_count = len(output_results)
            if not is_thepaper_listing:
                # 判断是否为聚合类页面（微博热搜、知乎热榜等）
                # 这类页面的文章通常没有发布日期，需要保留
                is_aggregation = any(x in url.lower() for x in ['/top/', '/hot', '/trending', '/rank', 'tophub'])

                if is_aggregation:
                    # 聚合页面：保留所有文章（包括没有日期的）
                    output_results = [
                        r for r in output_results
                        if not r.published_at or self._date_in_range(r.published_at, start_date, end_date)
                    ]
                else:
                    dated_results = [
                        r for r in output_results
                        if r.published_at and self._date_in_range(r.published_at, start_date, end_date)
                    ]
                    undated_results = [r for r in output_results if not r.published_at]

                    # 日期筛选应当排除“明确超范围”的文章，而不是把“站点未提供
                    # 可解析日期”的有效正文静默丢弃。若绝大多数候选都没有日期，
                    # 说明这是相对时间、客户端渲染或其他站点特有格式；保留这些
                    # 候选并记录告警，让后续站点适配不会重演本次 20 -> 1 的问题。
                    if undated_results and len(undated_results) > len(output_results) * 0.5:
                        logger.warning(
                            "发布日期缺失占多数，保留未定日期的有效正文以避免误筛: "
                            "范围内=%s，缺失=%s，总计=%s",
                            len(dated_results), len(undated_results), len(output_results),
                        )
                        output_results = dated_results + undated_results
                    else:
                        output_results = dated_results
            logger.info(f"发布日期过滤: {before_count} -> {len(output_results)} 篇")

        # 5. 按日期排序（最新的在前）
        output_results = self._sort_by_date(output_results)

        # 6. 限制最终数量
        output_results = output_results[:max_articles]

        logger.info(f"深度爬取完成: {len(output_results)} 条结果")
        return list_page, output_results

    def _filter_article_links(
        self,
        links: List[str],
        base_url: str,
        trusted_list_urls: Optional[set] = None,
    ) -> List[str]:
        """过滤出文章链接"""
        parsed_base = urlparse(base_url)
        domain = parsed_base.netloc
        base_path = parsed_base.path

        # 从列表页 URL 提取主栏目名称
        # 例如: https://www.cas.cn/yw/ -> main_category = "yw"
        #       https://aircas.ac.cn/dqyd/gzdt/ -> main_category = "dqyd"
        path_parts = base_path.strip('/').split('/')
        main_category = path_parts[0] if path_parts else ""

        logger.info(f"主栏目: /{main_category}/ (来自: {base_url})")

        # 判断是否为热榜/聚合类页面（允许外部链接）
        is_aggregation_page = any(x in base_url.lower() for x in ['/n/', '/hot', '/trending', '/rank', 'tophub', 'weibo.com', 'zhihu.com/'])

        # 特殊处理：微博热搜页面的话题链接
        is_weibo_hot = 'weibo.com' in base_url.lower() and '/top/' in base_url.lower()

        article_links = []
        trusted_list_urls = trusted_list_urls or set()

        # 跳过模式（导航、地图、登录等无意义链接）
        skip_patterns = [
            # 通用导航模式
            'login', 'register', 'about', 'contact', 'search',
            'index.html', 'index.htm', 'page=', '/page/',
            # 网站地图和导航
            'sitemap', 'site-map', '网站地图', 'map', 'nav', '导航',
            'menu', 'menus', 'sidebar', 'footer', 'header',
            # 语言切换
            'english', '/en/', '/eng/', 'locale', 'language', 'lang=',
            '邮箱登录', 'login.html', 'login.htm',
            # 联系我们和版权
            '联系我们', 'copyright', '版權', '版权所有',
            # 面包屑当前位置（首页、当前位置等）
            '首页', 'home', 'current', '当前位置', '您现在的位置',
            # 常见站点导航
            '党群园地', '工作动态', '组织机构', '科普园地',
            # 其他无用链接
            'share', 'share.html', '收藏', 'favorite', 'bookmark',
        ]

        # 热榜/聚合类网站允许的外部域名
        allowed_external_domains = [
            'zhihu.com', 'weibo.com', 'sina.com', 'tencent.com',
            'douban.com', 'bilibili.com', 'weixin', 'mp.weixin',
            'xinhuanet.com', 'people.com.cn', 'cctv.com',
            'baidu.com', 'sohu.com', 'ifeng.com', 'thepaper.cn',
            'jiemian.com', 'caixin.com', 'yicai.com',
        ]

        # 文章链接特征（更严格的要求）
        logger.info(f"开始过滤链接: 输入 {len(links)} 个链接, is_weibo_hot={is_weibo_hot}")
        for link in links:
            if not link or link.startswith(('javascript:', '#', 'mailto:')):
                continue

            parsed = urlparse(link)
            link_lower = link.lower()
            list_key = link.split("#", 1)[0]
            is_trusted_list_url = list_key in trusted_list_urls
            is_external = bool(parsed.netloc and parsed.netloc != domain)

            # 微博热搜话题链接特殊处理（搜索结果页和话题页）
            if is_weibo_hot and ('/weibo?' in link or '/topic/' in parsed.path):
                article_links.append(link)
                logger.debug(f"  接受(微博热搜): {link}")
                continue

            # Structured list data is the source site's explicit article feed.
            # Accept its external targets before generic index/share navigation rules.
            if is_trusted_list_url and is_external:
                article_links.append(link)
                logger.debug(f"  接受(列表页已标日期的外链): {link}")
                continue

            # 栏目分页不是文章，不能占用候选文章配额。
            if re.search(r'/index(?:_\d+)?\.(?:html?|shtml)$', parsed.path, re.IGNORECASE):
                continue

            # 栏目、频道、标签等导航页常以 .html 结尾，不能仅凭扩展名
            # 当作文章。真实详情页通常具有明确的语义路径段。
            is_listing_navigation = re.search(
                r'/(?:list|lists|channel|category|categories|topics?|tags?)/[^/]+\.(?:html?|shtml)$',
                parsed.path,
                re.IGNORECASE,
            ) is not None
            if is_listing_navigation:
                continue

            # 跳过模式
            if not is_trusted_list_url and any(p in link_lower for p in skip_patterns):
                continue

            # 外部链接检查
            if is_external:
                # 栏目列表自身列出的文章（且同一条目明确显示发布日期）
                # 可能跳转到微信公众号等外站，仍属于该栏目的有效文章。
                # 热榜/聚合类页面：允许特定外部链接
                if is_aggregation_page:
                    # 检查是否是允许的外部域名
                    is_allowed = any(d in parsed.netloc.lower() for d in allowed_external_domains)
                    if is_allowed:
                        article_links.append(link)
                        logger.debug(f"  接受(外部): {link}")
                    continue
                else:
                    # 非聚合页面：不接受外部链接
                    continue

            # 同域名链接检查
            link_path = parsed.path
            link_parts = link_path.strip('/').split('/')

            # 文章链接特征检查
            # 日期模式：/YYYYMM/ 或 /YYYYMMDD/ 或 tYYYYMMDD 或 YYYYMMDD.shtml
            has_date_in_url = bool(re.search(r'(?:\d{8})(?:/|\.)', link)) or bool(re.search(r'/(\d{6})(?:/|\.)', link)) or bool(re.search(r'/t\d{8}', link))
            # 文件扩展名
            has_file_ext = any(link.endswith(ext) for ext in ['.html', '.htm', '.shtml', '.php'])
            # 澎湃新闻频道页使用 /newsDetail_forward_数字 作为文章详情链接，
            # 不带传统文件扩展名，仅在澎湃同域名下识别，避免影响其他站点。
            is_thepaper_detail = (
                domain.endswith('thepaper.cn')
                and re.search(r'/newsDetail_forward_\d+', parsed.path, re.IGNORECASE) is not None
            )
            is_explicit_article_path = re.search(
                r'/(?:article|articles|news|detail|content|post)/(?:[^/]+/)?[^/]+(?:\.(?:html?|shtml|php))?$',
                parsed.path,
                re.IGNORECASE,
            ) is not None
            # gov.cn 文章路径模式：/栏目/content_xxx[_xxx].htm
            is_gov_content = (
                domain.endswith('gov.cn')
                and re.search(r'/content_[a-zA-Z0-9_]+\.(?:html?|shtml)$', parsed.path, re.IGNORECASE) is not None
            )
            # 允许无日期但有 content_ 特征的 .htm 文件（如 gov.cn）
            is_content_file_link = has_file_ext and re.search(r'/content_', link, re.IGNORECASE) is not None
            # 允许文件名中包含日期的 .htm 文件
            # 支持格式：/YYYYMMDD/、/YYYYMM/ + tYYYYMMDD_、content_2024080101.htm 等
            is_date_in_filename = has_file_ext and (
                re.search(r'(?:^|[\s/_])\d{8}[\s._]', link) is not None  # 8位日期
                or re.search(r'/\d{6}/t\d{8}', link) is not None  # 202607/t20260731 (空天院格式)
                or re.search(r'/\d{6}/', link) is not None  # 年月目录 (YYYYMM)
            )

            # 栏目检查：只接受主栏目路径下的文章
            is_same_category = len(link_parts) > 1 and link_parts[0] == main_category

            # 如果列表页是根路径（如 /），允许所有链接
            if not main_category:
                is_same_category = True

            # 热榜类页面（如 /n/xxx）：允许所有同域名的详情页
            if main_category == 'n' and has_file_ext:
                is_same_category = True

            # 栏目首页模式：列表页是栏目首页（如 /cpc/index.htm）时，允许根目录下按日期组织的文章
            # 例如求是网：列表页 /cpc/，文章 /20260705/...
            is_category_index_page = any(base_url.rstrip('/').endswith(x) for x in ['/index.htm', '/index.html', '/index.php', '/index'])

            if is_thepaper_detail:
                article_links.append(link)
                logger.debug(f"  接受(澎湃详情页): {link}")
            elif is_explicit_article_path:
                article_links.append(link)
                logger.debug(f"  接受(明确详情路径): {link}")
            elif is_gov_content or is_content_file_link:
                article_links.append(link)
                logger.debug(f"  接受(gov.cn 文章内容): {link}")
            elif has_file_ext and is_same_category:
                article_links.append(link)
                logger.debug(f"  接受(栏目匹配): {link}")
            elif has_file_ext and not is_same_category and is_category_index_page:
                # 栏目首页模式下，也接受根目录下按日期组织的文章
                article_links.append(link)
                logger.debug(f"  接受(栏目首页，根目录日期): {link}")
            elif has_file_ext and not is_same_category:
                # 文件名包含日期也接受，即使栏目不同（跨栏目按日期组织的文章也常见）
                if is_date_in_filename or has_date_in_url:
                    article_links.append(link)
                    logger.debug(f"  接受(跨栏目日期链接): {link}")
                else:
                    logger.debug(f"  过滤(不同栏目): {link}")

        logger.info(f"文章链接过滤完成: 识别到 {len(article_links)} 个文章链接")
        # 有序去重，避免 set 打乱列表页顺序后再被 max_articles 截断。
        return list(dict.fromkeys(article_links))

    def _date_in_range(self, date_str: str, start: date, end: date) -> bool:
        """检查日期是否在范围内"""
        try:
            # 允许交换 start/end
            if start > end:
                start, end = end, start

            parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
            return start <= parsed <= end
        except (ValueError, TypeError):
            return False

    def _prefilter_links_by_list_dates(
        self,
        article_links: List[str],
        list_item_dates: Dict[str, str],
        start: date,
        end: date,
        strict_coverage: float = 0.8,
    ) -> List[str]:
        """Use list dates only when their URL mapping is reliable enough."""
        if not article_links or not list_item_dates:
            return article_links

        matched_in_range: List[str] = []
        matched_out_of_range: List[str] = []
        undated: List[str] = []

        for link in article_links:
            key = link.split("#", 1)[0]
            list_date = list_item_dates.get(key)
            if not list_date:
                undated.append(link)
            elif self._date_in_range(list_date, start, end):
                matched_in_range.append(link)
            else:
                matched_out_of_range.append(link)

        matched_count = len(matched_in_range) + len(matched_out_of_range)
        coverage = matched_count / len(article_links)

        if matched_count == 0:
            logger.warning(
                "列表日期与候选文章 URL 无交集，跳过日期预筛: 候选=%s，日期映射=%s",
                len(article_links),
                len(list_item_dates),
            )
            return article_links

        if coverage >= strict_coverage:
            logger.info(
                "列表日期映射可靠，执行严格预筛: 覆盖=%s/%s (%.0f%%)",
                matched_count,
                len(article_links),
                coverage * 100,
            )
            return matched_in_range

        logger.warning(
            "列表日期映射覆盖不足，保留未标日期候选: 覆盖=%s/%s (%.0f%%)，范围内=%s，明确超范围=%s",
            matched_count,
            len(article_links),
            coverage * 100,
            len(matched_in_range),
            len(matched_out_of_range),
        )
        return matched_in_range + undated

    def _sort_by_date(self, results: List[ScrapedResult]) -> List[ScrapedResult]:
        """按日期排序（最新的在前）"""
        def get_sort_key(r: ScrapedResult) -> tuple:
            if not r.published_at:
                return (1, date.min)
            try:
                return (0, datetime.strptime(r.published_at, "%Y-%m-%d").date())
            except:
                return (1, date.min)

        return sorted(results, key=get_sort_key, reverse=True)


def get_scraper() -> WebScraper:
    """获取爬取器实例"""
    return WebScraper()
