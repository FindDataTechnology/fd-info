"""断点续爬（ingest_paged）——平台能力，不是某个源的特性。

锁定三件此前缺失的事：
1. 每页落库后**立即推进游标**（挂在下页只从下页续，不重抓已落库的页）；
2. 进程中途挂掉后，下次以同一窗口重进**从游标续**、不重抓；
3. 游标绑定窗口：窗口变了游标作废（新的一天从头）。
"""

from fd_info.fetchers.base import item_from_row
from fd_info.models import PageResult
from fd_info.pipeline import ingest_paged
from fd_info.storage import Store


class _BodyStore:
    """最小正文桩：把正文落成可查的 doc_id。"""
    def __init__(self):
        self.docs = {}

    def put(self, item, body):
        doc_id = f"doc-{item.item_id}"
        self.docs[doc_id] = body
        return doc_id


def _item(i: int):
    return item_from_row(
        source="govcn", source_id=f"u{i}", source_type="news",
        published="2026-11-22", title=f"政策{i}", url=f"https://x/{i}",
        body=f"正文{i}",
    )


def _paged_fetcher(pages):
    """pages: {cursor_or_None: (items, next_cursor, done)}；按 cursor 取页。"""
    calls = []

    def fetch(cursor):
        calls.append(cursor)
        items, nxt, done = pages[cursor]
        return PageResult(items=tuple(items), next_cursor=nxt, done=done)

    return fetch, calls


def test_each_page_advances_cursor(tmp_path):
    store = Store(tmp_path / "t.db")
    pages = {
        None: ([_item(1), _item(2)], "c2", False),
        "c2": ([_item(3), _item(4)], "c3", False),
        "c3": ([_item(5)], None, True),
    }
    fetch, calls = _paged_fetcher(pages)
    summary = ingest_paged("govcn", "2026-11-22|2026-11-23", fetch, store, _BodyStore())
    assert summary.total == 5
    assert calls == [None, "c2", "c3"]
    # 跑完 done：游标清空、5 条全落库
    assert store.get_cursor("govcn", "2026-11-22|2026-11-23") is None
    rows = store._conn.execute("SELECT count(*) FROM info_items WHERE source='govcn'").fetchone()
    assert rows[0] == 5


def test_resume_after_midway_failure(tmp_path):
    """核心：第 2 页挂了 → 游标停在 c2 后；重进从 c2 续，不重抓第 1 页。"""
    store = Store(tmp_path / "t.db")
    win = "2026-11-22|2026-11-23"

    # 第一次：第 1 页成功；第 2 页抛异常（模拟进程被杀/上游断）
    def fetch_round1(cursor):
        if cursor is None:
            return PageResult(items=(_item(1), _item(2)), next_cursor="c2", done=False)
        raise RuntimeError("上游断在第2页")

    try:
        ingest_paged("govcn", win, fetch_round1, store, _BodyStore())
    except RuntimeError:
        pass
    # 第 1 页已落库、游标停在 c2
    assert store.get_cursor("govcn", win) == "c2"
    assert store._conn.execute("SELECT count(*) FROM info_items").fetchone()[0] == 2

    # 第二次：从 c2 续（调用方拿到的 cursor 应是 c2，不是 None）
    seen = []

    def fetch_round2(cursor):
        seen.append(cursor)
        assert cursor == "c2", f"应从上轮游标续，实得 {cursor!r}"
        return PageResult(items=(_item(3), _item(4)), next_cursor=None, done=True)

    summary = ingest_paged("govcn", win, fetch_round2, store, _BodyStore())
    assert seen == ["c2"]
    assert summary.total == 2  # 只抓了第2页的 2 条，没重抓第1页
    assert store._conn.execute("SELECT count(*) FROM info_items").fetchone()[0] == 4


def test_cursor_scoped_to_window(tmp_path):
    """游标绑定窗口：新窗口（新的一天）游标作废，从 None 起。"""
    store = Store(tmp_path / "t.db")
    store.set_cursor("govcn", "2026-11-21|2026-11-22", "old-cursor")
    assert store.get_cursor("govcn", "2026-11-21|2026-11-22") == "old-cursor"
    # 换窗口 → 游标作废
    assert store.get_cursor("govcn", "2026-11-22|2026-11-23") is None
