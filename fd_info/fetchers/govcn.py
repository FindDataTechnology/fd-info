"""gov.cn 政策文件库 fetcher —— 政策全文语料（reuse-curated-sources 原则的落地）。

来源：gov.cn 搜索后端 ``sousuo.www.gov.cn/search-gov/data``（真 JSON API，无
登录/无验证码/无风控），收录国务院文件/部委文件/中央文件等正式公文。这是**整理好
的权威库**，比逐部委爬通知公告覆盖全、体裁对口（正式带文号的政策文件）。

为什么走 fd-info 而不是行业指标平台：政策是**全文语料**（要进 text-core 语料库
供检索/引用），不是列式指标行。fd-info 的「信息流 → 正文落 text-core 语料库」
管线正是干这个的。

**断点续爬**（本轮补的平台能力）：实现 ``fetch_page(cursor, since, until)``——
cursor = 页码游标。每页只抓一页的条目（+ 逐篇正文），返 ``PageResult(items,
next_cursor, done)``。挂了下次从游标续、不重抓已落库的页；内存恒定（一页有界）。
全量 1.96 万篇靠多页/多轮推进，不再攒内存（此前手搓 Job 全量攒内存 OOM 的教训）。

正文抽取：httpx 拿完整 body → ``<div id="UCAP-CONTENT">`` → 剥标签。纯标准库+httpx
（与 news.py 同，不引 scrapling；httpx 的 body 完整，没有 scrapling html_content
对 gov.cn 详情页的截断问题）。

合规：robots ``Disallow:/`` 已由用户裁定「内部语料、低频、公开数据」；``delay`` 礼貌
限速（每请求间隔）。
"""

from __future__ import annotations

import html
import re
import time
from typing import Callable

import httpx

from ..models import InfoItem, PageResult, SourceResult
from .base import FetchWindow, item_from_row

SOURCE = "govcn"
API = "https://sousuo.www.gov.cn/search-gov/data"
USER_AGENT = "fd-info/0.1 (+https://github.com/FindDataTechnology)"

#: 分类 -> (t 参数, catMap 键)。默认三类 = 正式政策文件（排 otherfile 解读/公报汇编）。
CATEGORIES: dict[str, tuple[str, str]] = {
    "gongwen": ("zhengcelibrary", "gongwen"),        # 国务院文件
    "bumenfile": ("zhengcelibrary", "bumenfile"),    # 部委文件
    "zhongyangfile": ("zhengce", "zhongyangfile"),   # 中央文件
}
DEFAULT_CATEGORIES = ("gongwen", "bumenfile", "zhongyangfile")
PAGE_SIZE = 20

Transport = Callable[[str, dict[str, str]], str]

_UCAP = re.compile(r'<div[^>]*id="UCAP-CONTENT"[^>]*>(.*?)</div>', re.S)
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t　]+")


def _strip_html(raw: str) -> str:
    text = _SCRIPT.sub(" ", raw)
    text = re.sub(r"</(p|div|section|br|li|h\d|tr)>", "\n", text, flags=re.I)
    text = html.unescape(_TAG.sub("", text))
    text = _WS.sub(" ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _norm_date(s: str | None) -> str | None:
    """``2026.11.10`` -> ``2026-11-10``；无法解析返回 None。"""
    if not s:
        return None
    m = re.match(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", str(s).strip())
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


class GovcnSource:
    name = SOURCE

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        categories: tuple[str, ...] = DEFAULT_CATEGORIES,
        page_size: int = PAGE_SIZE,
        delay: float = 0.8,
        fetch_body: bool = True,
    ) -> None:
        self._transport = transport or self._http
        self.categories = tuple(c for c in categories if c in CATEGORIES)
        self.page_size = page_size
        self.delay = delay
        self.fetch_body = fetch_body

    # ── 整窗接口（兼容 cmd_fetch 的非分页分支；分页优先见 fetch_page）─────────
    def fetch(self, since: str, until: str) -> SourceResult:
        """把分页跑完拼成整窗结果（供不走续爬的旧调用路径）。"""
        items: list[InfoItem] = []
        cursor = None
        while True:
            page = self.fetch_page(cursor, since, until)
            if not page.usable:
                return SourceResult(SOURCE, "unavailable", reason=page.reason)
            items.extend(page.items)
            if page.done:
                break
            cursor = page.next_cursor
        return SourceResult(SOURCE, "ok", items=tuple(items), window_checked=True)

    # ── 分页接口（断点续爬：cursor=页码）───────────────────────────────────
    def fetch_page(self, cursor: str | None, since: str, until: str) -> PageResult:
        """抓一页。cursor 编码 ``<cat_idx>:<page>``（如 ``0:3``）；None = 从第 0 类第 1 页起。

        - 该分类整页都早于 since → 跳到下个分类；所有分类都翻过 → done。
        - 每个分页条目逐篇抓正文（fetch_body 时）。
        """
        cat_idx, page = (0, 1) if cursor is None else self._parse_cursor(cursor)
        if cat_idx >= len(self.categories):
            return PageResult(done=True)

        cat = self.categories[cat_idx]
        try:
            entries = self._fetch_list(cat, page)
        except Exception as exc:  # noqa: BLE001 - 上游失败是一条可续的数据
            return PageResult(usable=False, reason=f"{cat} p{page}: {type(exc).__name__}: {exc}")

        items: list[InfoItem] = []
        any_within_window = False
        for e in entries:
            pub = _norm_date(e.get("pubtimeStr"))
            if pub is None or (since and pub < since):
                continue
            any_within_window = True
            items.append(self._make_item(e, pub))

        # 下一页游标：同类有下一页且窗口仍在 → 同 cat 下一页；否则换下个分类 p1。
        has_more_in_cat = len(entries) >= self.page_size and any_within_window
        if has_more_in_cat:
            next_cursor = f"{cat_idx}:{page + 1}"
            done = False
        else:
            nxt_idx = cat_idx + 1
            next_cursor = None if nxt_idx >= len(self.categories) else f"{nxt_idx}:1"
            done = next_cursor is None
        return PageResult(items=tuple(items), next_cursor=next_cursor, done=done)

    # ── 内部 ───────────────────────────────────────────────────────────────
    def _parse_cursor(self, cursor: str) -> tuple[int, int]:
        try:
            a, b = cursor.split(":", 1)
            return int(a), int(b)
        except (ValueError, AttributeError):
            return 0, 1

    def _fetch_list(self, cat: str, page: int) -> list[dict]:
        t, key = CATEGORIES[cat]
        url = f"{API}?t={t}&type=gwyzcwjk&q=&p={page}&n={self.page_size}"
        raw = self._transport(url, {"User-Agent": USER_AGENT})
        import json
        cm = json.loads(raw)["searchVO"]["catMap"]
        return (cm.get(key) or {}).get("listVO") or []

    def _make_item(self, e: dict, pub: str) -> InfoItem:
        url = str(e.get("url") or "")
        title = str(e.get("title") or "").strip() or (e.get("id") and f"政策{e.get('id')}") or "无题政策"
        summary = str(e.get("summary") or "").strip() or None
        body = None
        if self.fetch_body and url:
            if self.delay:
                time.sleep(self.delay)
            try:
                page = self._transport(url, {"User-Agent": USER_AGENT})
                m = _UCAP.search(page)
                if m:
                    body = _strip_html(m.group(1)) or None
            except Exception:  # noqa: BLE001 - 单篇正文失败：留 pending 供回填
                body = None
        return item_from_row(
            source=SOURCE, source_id=url or str(e.get("id")), source_type="policy",
            published=pub, title=title, url=url or None, summary=summary, body=body,
            domain="gov",
        )

    def _http(self, url: str, headers: dict[str, str]) -> str:
        # trust_env=False：系统代理会劫持 gov.cn 直连（与 news.py 同因）
        with httpx.Client(timeout=30, trust_env=False, follow_redirects=True) as c:
            r = c.get(url, headers=headers)
            r.raise_for_status()
            return r.text
