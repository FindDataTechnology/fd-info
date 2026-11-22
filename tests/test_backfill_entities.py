"""回填脚本的判据（info-entity-attribution tasks 3.1）。

覆盖：distinct_published_dates / set_entities 幂等 / _fetch_window 提取带码条目。
不打网——_fetch_window 注入假 source。
"""

import importlib.util
import os
import sys

from fd_info.models import InfoItem, SourceResult
from fd_info.storage import Store

_SCP = os.path.join(os.path.dirname(__file__), "..", "scripts", "backfill_cninfo_entities.py")


def _load_script():
    spec = importlib.util.spec_from_file_location("backfill_cninfo_entities", _SCP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def item(source_id, published, entities=(), source="cninfo"):
    return InfoItem(
        source=source, source_id=source_id, source_type="announcement",
        published_at=published, title="t", entities=entities,
    )


class FakeSource:
    def __init__(self, items):
        self._items = items
        self.calls = []

    def fetch(self, since, until):
        self.calls.append((since, until))
        return SourceResult("cninfo", "ok", items=tuple(self._items), window_checked=True)


def test_distinct_published_dates(tmp_path):
    s = Store(tmp_path / "i.db")
    s.upsert(item("a", "2026-10-20"))
    s.upsert(item("b", "2026-10-20"))
    s.upsert(item("c", "2026-10-21"))
    assert s.distinct_published_dates("cninfo") == ["2026-10-20", "2026-10-21"]
    s.close()


def test_set_entities_idempotent(tmp_path):
    s = Store(tmp_path / "i.db")
    _, _, iid = s.upsert(item("a", "2026-10-20"))
    assert s.set_entities(iid, ("600519",)) is True
    assert s.set_entities(iid, ("600519",)) is False  # 幂等：内容未变不写
    assert s.get_item(iid)["entities"] == ["600519"]
    s.close()


def test_fetch_window_extracts_codes():
    mod = _load_script()
    fake = FakeSource([
        item("a", "2026-10-20", entities=("600519",)),
        item("b", "2026-10-20", entities=()),  # 无码，不进映射
    ])
    out = mod._fetch_window(["2026-10-20", "2026-10-21"], source=fake)
    assert out == {"a": ("600519",)}
    # 逐日拉（不是合并区间——合并会因 CNINFO 分页上限漏掉大半天数）
    assert fake.calls == [("2026-10-20", "2026-10-20"), ("2026-10-21", "2026-10-21")]


def test_untagged_items_on_date_returns_all(tmp_path):
    """单日全量不分页截断（list_since 500 上限会漏，回填要全量）。"""
    s = Store(tmp_path / "i.db")
    for i in range(600):
        s.upsert(item(f"x{i}", "2026-10-21"))
    s.upsert(item("tagged", "2026-10-21", entities=("600519",)))
    s.upsert(item("other", "2026-10-22"))
    got = s.untagged_items_on_date("cninfo", "2026-10-21")
    assert len(got) == 600  # 全部未标注，超 500 不截断
    assert all(r["entities"] == [] for r in got)
    s.close()
