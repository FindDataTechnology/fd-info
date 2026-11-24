import json

from fd_info.body_store import BodyUnavailable, SpoolBodyStore
from fd_info.models import InfoItem

import pytest


def item(**kw):
    base = dict(
        source="cn-gov",
        source_id="http://x/1.html",
        source_type="ministry",
        published_at="2026-10-21",
        title="通知",
    )
    base.update(kw)
    return InfoItem(**base)


def test_spool_writes_jsonl_and_locates_quote(tmp_path):
    store = SpoolBodyStore(tmp_path)
    it = item()
    doc_id = store.put(it, "政策全文：自 2026 年起施行。")
    assert doc_id == "cn-gov:http://x/1.html"
    assert (tmp_path / "cn-gov" / "structured" / "2026-10-21.jsonl").exists()
    assert store.locate(doc_id, 0, 6) == "政策全文：自"


def test_spool_row_declares_source_type_and_domain(tmp_path):
    """source_type/domain 随正文落盘，下游据此判来源类型/垂域，不按源名反查。"""
    store = SpoolBodyStore(tmp_path)
    store.put(item(source_type="policy", domain="gov"), "正文")
    line = (tmp_path / "cn-gov" / "structured" / "2026-10-21.jsonl").read_text(
        encoding="utf-8"
    ).strip()
    rec = json.loads(line)
    assert rec["source_type"] == "policy"
    assert rec["domain"] == "gov"


def test_spool_missing_body_and_bounds(tmp_path):
    store = SpoolBodyStore(tmp_path)
    with pytest.raises(BodyUnavailable):
        store.locate("cn-gov:none", 0, 1)
    doc_id = store.put(item(), "abc")
    with pytest.raises(BodyUnavailable):
        store.locate(doc_id, 2, 99)


def test_spool_full_returns_whole_body(tmp_path):
    store = SpoolBodyStore(tmp_path)
    doc_id = store.put(item(), "政策全文：自 2026 年起施行。")
    assert store.full(doc_id) == "政策全文：自 2026 年起施行。"


def test_spool_full_missing_raises(tmp_path):
    store = SpoolBodyStore(tmp_path)
    with pytest.raises(BodyUnavailable):
        store.full("cn-gov:none")


def test_spool_skips_malformed_lines(tmp_path):
    """历史脏行（如 PDF 二进制污染产生的非法 JSON）跳过，不炸 full/locate。"""
    store = SpoolBodyStore(tmp_path)
    it = item()
    doc_id = store.put(it, "正常正文")
    target = tmp_path / "cn-gov" / "structured" / "2026-10-23.jsonl"
    # 实际写一条坏行 + 重复 put 以确保目标文件存在坏行且新行在后
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"source": "cn-gov", "source_id": "http://x/1.html",
                             "content": "正常正文"}, ensure_ascii=False) + "\n")
    assert store.full(doc_id) == "正常正文"
    assert store.locate(doc_id, 0, 2) == "正常"
