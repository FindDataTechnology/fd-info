"""端到端：抓取补实体 → 按主体过滤（info-entity-attribution tasks 4.1）。

不打网：cninfo transport 注入假响应（真实端点形态），走真实
CninfoSource → Store.upsert → list_since(source_codes=...) 全链。
"""

from fd_info.fetchers.cninfo import CninfoSource
from fd_info.storage import Store


def cninfo_payload():
    return {
        "announcements": [
            {
                "announcementId": "1225682651",
                "announcementTitle": "关于股东部分股份解除质押的公告",
                "announcementTime": 1795178186000,  # 2026-11-20
                "adjunctUrl": "finalpage/2026-11-20/1225682651.PDF",
                "secCode": "300081",
                "secName": "ST恒信",
            },
            {
                "announcementId": "1225682652",
                "announcementTitle": "关于签订重大合同的公告",
                "announcementTime": 1795178186000,
                "adjunctUrl": "finalpage/2026-11-20/1225682652.PDF",
                "secCode": "600519",
                "secName": "贵州茅台",
            },
        ]
    }


def test_end_to_end_entity_attribution_roundtrip(tmp_path):
    store = Store(tmp_path / "info.db")
    src = CninfoSource(transport=lambda data: cninfo_payload(), columns=("sse",))
    result = src.fetch("2026-11-20", "2026-11-20")
    assert result.status == "ok"
    for it in result.items:
        store.upsert(it)

    # 每条都带了实体标注
    tagged = store.list_since()["items"]
    assert all(r["entities"] for r in tagged)

    # 按主体过滤只命中该主体
    hits = store.list_since(source_codes=["600519"])["items"]
    assert [h["source_id"] for h in hits] == ["1225682652"]

    # 不存在的代码 → 空 + 如实原因
    miss = store.list_since(source_codes=["999999"], sources=["cninfo"])
    assert miss["items"] == []
    assert miss["empty_reason"] == "no_items_for_subject"

    store.close()
