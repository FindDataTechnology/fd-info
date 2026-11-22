"""新闻 RSS 聚合 fetcher：无 API key 的公开 RSS 源。

为什么要有它：news 垂域现有 5928 篇全是**公告/信披**（cninfo 公告、部委发布、
交易所互动），没有一条是新闻稿。本 fetcher 拉中文财经/时政媒体的公开 RSS，
补上「真新闻」这一层。

源的取舍（2026-11-22 实调，本机直连可达、无需 key）：
  - 中新网 财经 / 时政（``chinanews.com.cn/rss/*.xml``）：30 条/feed，标准 RSS2.0
  - 华尔街见闻（``dedicated.wallstreetcn.com/rss.xml``）：60 条/feed，含整篇正文
排除的：新浪 RSS 列表最后更新停在 2018（pubDate 2018-09-23）；36kr ``/feed``
返回 HTML 反爬页；Google News RSS 从本机不可达。

正文：RSS ``description`` 本身就是摘要或整篇正文（华尔街见闻是整篇），剥掉
HTML 标签后入 ``body`` —— 因此不需要详情页回填，``body_status`` 直接 stored。
``source_id`` 取 ``guid``，缺 guid 时用 ``link``（两个实调源都只有 link，均稳定
不变）；``entities`` 如实为空（RSS 不携带证券代码，不猜造）。

HTTP 一律 ``trust_env=False``（与 interaction.py 同因：系统代理会劫持请求）。
``transport`` 可注入，测试用固定 RSS 样本，绝不真打网。
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

import httpx

from ..models import InfoItem, SourceResult
from .base import FetchWindow, item_from_row

SOURCE = "news-rss"
USER_AGENT = "fd-info/0.1 (+https://github.com/FindDataTechnology)"
SH_TZ = timezone(timedelta(hours=8))

#: (通道名, RSS 地址)。通道名只用于 partial 的 missing 定位，不进 source_id。
FEEDS: tuple[tuple[str, str], ...] = (
    ("chinanews-finance", "https://www.chinanews.com.cn/rss/finance.xml"),
    ("chinanews-china", "https://www.chinanews.com.cn/rss/china.xml"),
    ("wallstreetcn", "https://dedicated.wallstreetcn.com/rss.xml"),
)

Transport = Callable[[str, dict[str, str]], str]

_TAG = re.compile(r"<[^>]+>")
_BLANK = re.compile(r"[ \t　]+")
_NEWLINES = re.compile(r"\n{3,}")


class NewsSource:
    name = SOURCE

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        feeds: tuple[tuple[str, str], ...] = FEEDS,
    ) -> None:
        self._transport = transport or self._http
        self.feeds = tuple(feeds)

    def fetch(self, since: str, until: str) -> SourceResult:
        window = FetchWindow(since, until)
        items: list[InfoItem] = []
        seen: set[str] = set()
        missing: list[str] = []
        for label, url in self.feeds:
            try:
                for item in self._fetch_feed(label, url, window):
                    # 多个 feed 可能带同一条（中新网财经/时政会串），按稳定身份折叠
                    if item.item_id not in seen:
                        seen.add(item.item_id)
                        items.append(item)
            except Exception as exc:  # noqa: BLE001 - 单 feed 失败是一条数据
                missing.append(f"{label}: {type(exc).__name__}: {exc}")
        if missing and not items:
            return SourceResult(SOURCE, "unavailable", reason="; ".join(missing))
        if missing:
            return SourceResult(
                SOURCE, "partial", items=tuple(items), reason="部分新闻源拉取失败",
                missing=tuple(missing), window_checked=True,
            )
        return SourceResult(SOURCE, "ok", items=tuple(items), window_checked=True)

    def _fetch_feed(self, label: str, url: str, window: FetchWindow) -> list[InfoItem]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"}
        raw = self._transport(url, headers)
        channel = ET.fromstring(raw).find("channel")
        if channel is None:
            raise ValueError("不是 RSS（缺 channel）")
        out: list[InfoItem] = []
        for node in channel.findall("item"):
            item = self._to_item(node, window)
            if item is not None:
                out.append(item)
        return out

    @staticmethod
    def _to_item(node: ET.Element, window: FetchWindow) -> InfoItem | None:
        title = _text(node, "title")
        link = _text(node, "link")
        source_id = _text(node, "guid") or link
        published = _parse_rfc822(_text(node, "pubDate"))
        if not (title and source_id and published):
            return None
        # 窗口按源侧时区（+08:00）的日期判定，与 ministry 的「页面日期 vs 窗口」同口径
        if not (window.since <= published.astimezone(SH_TZ).strftime("%Y-%m-%d") <= window.until):
            return None
        body = _strip_html(_text(node, "description")) or None
        return item_from_row(
            source=SOURCE,
            source_id=source_id,
            source_type="news",
            published=published.isoformat(),
            title=title,
            url=link or None,
            body=body,
        )

    @staticmethod
    def _http(url: str, headers: dict[str, str]) -> str:
        # trust_env=False：系统代理（本机 127.0.0.1:7892）会劫持请求，显式关闭。
        with httpx.Client(timeout=30.0, trust_env=False, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text


def _text(node: ET.Element, tag: str) -> str:
    return (node.findtext(tag) or "").strip()


def _parse_rfc822(raw: str) -> datetime | None:
    """RFC 822 → 带时区的 datetime；解析不出返回 None（该条丢弃，不猜时间）。"""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _strip_html(raw: str) -> str:
    """RSS description → 干净正文。华尔街见闻给整篇 HTML，中新网给纯文本。"""
    if "<" not in raw:
        return _BLANK.sub(" ", raw).strip()
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"</(p|div|section|br|li|h\d)>", "\n", text, flags=re.I)
    text = html.unescape(_TAG.sub("", text))
    lines = [_BLANK.sub(" ", line).strip() for line in text.splitlines()]
    return _NEWLINES.sub("\n\n", "\n".join(line for line in lines if line)).strip()
