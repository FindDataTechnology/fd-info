"""一轮摄取：拉取结果 → 索引 → 正文 → 水位。

失败是数据，不是控制流：来源不可用时写 source_state 并返回摘要，
不抛异常中断其他来源。正文不可取的条目保留 `body_status=failed`，
绝不把未落正文标成 stored。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .body_store import BodyStore, BodyUnavailable
from .models import InfoItem, SourceResult
from .storage import Store

BodyFetcher = Callable[[str], str | None]  # 入参=条目的 url（实现均为 url 签名；
# 摄取期只对新建/变更条目调用，见 ingest_result）


@dataclass
class IngestSummary:
    source: str
    status: str
    total: int = 0
    created: int = 0
    changed: int = 0
    unchanged: int = 0
    body_stored: int = 0
    body_failed: int = 0
    reason: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _ingest_items(
    items,
    store: Store,
    body_store: BodyStore,
    summary: IngestSummary,
    *,
    body_fetcher: BodyFetcher | None,
) -> None:
    """逐条 upsert + 落正文，统计进 summary。整窗与分页共用这一份。"""
    for item in items:
        created, changed, item_id = store.upsert(item)
        summary.created += int(created)
        summary.changed += int(changed)
        summary.unchanged += int(not created and not changed)

        if not (created or changed):
            continue
        if item.withdrawn and not created:
            # 撤回（design D3）：只翻 withdrawn 位即止，正文指针与 body_status
            # 保持原样——不物理删除，text-core 侧文档与历史不动。撤回页通常
            # 已取不到正文，若继续走正文路径会把 doc_id 错误清成 failed。
            continue
        if item.body is None and body_fetcher is not None and item.url:
            try:
                body = body_fetcher(item.url)
            except BodyUnavailable:
                body = None
        else:
            body = item.body
        if body:
            try:
                doc_id = body_store.put(item, body)
                store.mark_body(item_id, doc_id=doc_id, body_status="stored")
                summary.body_stored += 1
            except BodyUnavailable:
                store.mark_body(item_id, doc_id=None, body_status="failed")
                summary.body_failed += 1
        else:
            store.mark_body(item_id, doc_id=None, body_status="failed")
            summary.body_failed += 1


def ingest_result(
    result: SourceResult,
    store: Store,
    body_store: BodyStore,
    *,
    body_fetcher: BodyFetcher | None = None,
) -> IngestSummary:
    summary = IngestSummary(source=result.source, status=result.status, total=len(result.items))
    if not result.is_usable:
        summary.reason = result.reason or "来源不可用"
        store.set_source_state(result.source, ok=False, error=summary.reason)
        return summary

    newest = None
    for item in result.items:
        if newest is None or (item.published_at, item.item_id) > (newest.published_at, newest.item_id):
            newest = item
    _ingest_items(result.items, store, body_store, summary, body_fetcher=body_fetcher)

    if newest is not None:
        store.set_source_state(
            result.source,
            ok=True,
            watermark_published=newest.published_at,
            watermark_item_id=newest.item_id,
        )
    else:
        # 合法空窗口：仍推进成功时间，水位保持不变。
        store.set_source_state(result.source, ok=True)
    return summary


def ingest_paged(
    source: str,
    window: str,
    page_fetcher: Callable[[str | None], "PageResult"],
    store: Store,
    body_store: BodyStore,
    *,
    body_fetcher: BodyFetcher | None = None,
    max_pages: int | None = None,
) -> IngestSummary:
    """分页续爬摄取：一页一页抓、每页落库后推进游标，挂了下次从游标续。

    与 ingest_result 的区别（断点续爬的核心）：
    - 每页结束就 `set_cursor` 落库（不是整轮结束才推进水位）；
    - 下次启动先 `get_cursor`，有则从该游标续抓，**不重抓已落库的页**；
    - 单页内存有界（一页 = page_size 条），全量靠多页/多轮推进。

    page_fetcher(cursor) -> PageResult(items, next_cursor, done, usable, reason)。
    源适配器只需把「翻页」暴露成这个签名；一页抓挂抛异常即可，游标停在上一页，
    已落库的页不浪费。
    """
    summary = IngestSummary(source=source, status="ok", total=0)
    cursor = store.get_cursor(source, window)
    pages = 0
    while True:
        page = page_fetcher(cursor)
        if not page.usable:
            summary.status = "unavailable"
            summary.reason = page.reason or "来源不可用"
            store.set_source_state(source, ok=False, error=summary.reason)
            return summary

        summary.total += len(page.items)
        _ingest_items(page.items, store, body_store, summary, body_fetcher=body_fetcher)
        pages += 1

        # 每页落库后立刻推进游标（断点续爬：这一页已安全，挂在下页也只从下页续）。
        if page.done:
            # 该窗口抓完：清游标（下次同窗口从头=空窗口；新窗口另算），推进水位。
            store.set_cursor(source, window, None)
            newest = max(page.items, key=lambda i: (i.published_at, i.item_id), default=None)
            if newest is not None:
                store.set_source_state(source, ok=True,
                                       watermark_published=newest.published_at,
                                       watermark_item_id=newest.item_id)
            else:
                store.set_source_state(source, ok=True)
            return summary

        cursor = page.next_cursor
        store.set_cursor(source, window, cursor)
        if max_pages is not None and pages >= max_pages:
            # 达到本轮页数上限：游标已落库，下次续。状态仍 ok（有进展）。
            return summary
