"""gov.cn 政策 fetcher：假 transport（固定 API JSON + 固定详情 HTML），全离线。

锁：分页解析、正文从 UCAP-CONTENT 抽、since 截断、cursor 翻页语义、
以及**与 ingest_paged 合起来的断点续爬**（抓到一半挂、从游标续不重抓）。
"""

import json

from fd_info.fetchers.govcn import GovcnSource
from fd_info.pipeline import ingest_paged
from fd_info.storage import Store


def _api(cat, entries):
    return json.dumps({"searchVO": {"catMap": {cat: {"listVO": entries}}}})


def _entry(i, pub="2026.11.10", title=None):
    return {
        "id": str(1000 + i),
        "title": title or f"政策文件{i}",
        "pubtimeStr": pub,
        "summary": f"摘要{i}",
        "url": f"https://www.gov.cn/zhengce/zhengceku/202611/content_{i}.htm",
    }


def _detail(body):
    return f'<html><div id="UCAP-CONTENT"><p>{body}</p></div></html>'


def make_transport(pages):
    """pages: {url_substr: 返回内容}。list API 返回按 cat 取；detail 返回固定 body。"""
    calls = []

    def transport(url, headers):
        calls.append(url)
        if "search-gov/data" in url:
            for k, v in pages.items():
                if k in url:
                    return v
            return _api("gongwen", [])
        # 详情页
        return _detail("中华人民共和国国务院令 第N号 正文内容")

    return transport, calls


def test_page_extracts_body_and_paginates():
    api = _api("gongwen", [_entry(1), _entry(2)])
    transport, _ = make_transport({"p=1": api})
    src = GovcnSource(transport=transport, categories=("gongwen",), page_size=2, delay=0)
    page = src.fetch_page(None, "2026-11-01", "2026-11-23")
    assert len(page.items) == 2
    assert page.items[0].body and "国务院令" in page.items[0].body
    assert page.items[0].source_type == "policy"
    assert page.items[0].url.endswith("content_1.htm")
    # 满页 2 条 → 还有下一页，cursor 指向同类 p2
    assert page.next_cursor == "0:2" and page.done is False


def test_since_truncation_ends_category():
    # 整页都早于 since → 该分类不再翻，跳到下个分类/结束
    api = _api("gongwen", [_entry(1, pub="2020.01.01"), _entry(2, pub="2020.01.02")])
    transport, _ = make_transport({"p=1": api})
    src = GovcnSource(transport=transport, categories=("gongwen",), page_size=2, delay=0)
    page = src.fetch_page(None, "2026-11-01", "2026-11-23")
    assert page.items == ()
    assert page.done is True


def test_fetch_page_unavailable_is_usable_false():
    def boom(url, headers):
        raise RuntimeError("网络断")
    src = GovcnSource(transport=boom, categories=("gongwen",), page_size=2, delay=0)
    page = src.fetch_page(None, "2026-11-01", "2026-11-23")
    assert page.usable is False and page.reason


class _BodyStore:
    def __init__(self):
        self.docs = {}

    def put(self, item, body):
        d = f"doc-{item.item_id}"
        self.docs[d] = body
        return d


def test_govcn_resume_through_pipeline(tmp_path):
    """核心闭环：govcn 分页源 + ingest_paged，第2页挂 → 从游标续不重抓第1页。"""
    store = Store(tmp_path / "t.db")
    win = "2026-11-01|2026-11-23"

    # 第1页成功，第2页抛错
    def round1(cursor):
        if cursor is None:
            api = _api("gongwen", [_entry(1), _entry(2)])
            tr, _ = make_transport({"p=1": api})
            return GovcnSource(transport=tr, categories=("gongwen",), page_size=2,
                               delay=0).fetch_page(None, "2026-11-01", "2026-11-23")
        raise RuntimeError("上游断在第2页")

    try:
        ingest_paged("govcn", win, round1, store, _BodyStore())
    except RuntimeError:
        pass
    assert store.get_cursor("govcn", win) == "0:2"
    assert store._conn.execute("SELECT count(*) FROM info_items").fetchone()[0] == 2

    # 第2次从 0:2 续
    seen = []

    def round2(cursor):
        seen.append(cursor)
        api = _api("gongwen", [_entry(3), _entry(4)])
        tr, _ = make_transport({"p=2": api})
        return GovcnSource(transport=tr, categories=("gongwen",), page_size=2,
                           delay=0).fetch_page(cursor, "2026-11-01", "2026-11-23")

    summary = ingest_paged("govcn", win, round2, store, _BodyStore())
    # 从游标 0:2 续（不重抓第1页）；第2页满 → 追查第3页确认到底（0:3 空 → done）
    assert seen[0] == "0:2", f"必须从游标续，实得 {seen!r}"
    assert summary.total == 2  # 只入库第2页的 2 条，第1页没重抓
    assert store._conn.execute("SELECT count(*) FROM info_items").fetchone()[0] == 4
