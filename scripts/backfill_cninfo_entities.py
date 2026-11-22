"""回填 cninfo 条目的主体归属（info-entity-attribution，tasks 3.1）。

存量 cninfo 条目在抓取修复前入库，`entities` 恒空——而 `url` 里只有
`announcementId`（`.../finalpage/<date>/<id>.PDF`），反推不出证券代码。
唯一可靠的回填路径是**按发布日窗口重拉 CNINFO 列表接口**（列表记录带
`secCode`），按 `(source, source_id)` 匹配已存条目写回 `entities`。

用法（pod 内或本地对测试库）::

    FD_INFO_DB=/data/fd_info.db python scripts/backfill_cninfo_entities.py --dry-run
    FD_INFO_DB=/data/fd_info.db python scripts/backfill_cninfo_entities.py --apply

幂等：按 item_id 写回，内容未变不写；重复 `--apply` 第二次更新 0 条。
回填不到的条目（来源侧已清理过期公告）计数进 `unbackfillable` 如实报告，
不猜造。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fd_info.fetchers.cninfo import CninfoSource  # noqa: E402
from fd_info.storage import Store  # noqa: E402

SOURCE = "cninfo"


def _fetch_window(dates: list[str], *, source=None) -> dict[str, tuple[str, ...]]:
    """重拉若干发布日的 CNINFO 列表，返回 ``{source_id: entities}``。

    按日期分组批量拉：CNINFO 的 ``seDate`` 支持闭区间，连续日期合并成一个
    窗口拉取，减少请求数。``source`` 可注入（测试用假实现）。
    """
    if not dates:
        return {}
    source = source or CninfoSource()
    since, until = min(dates), max(dates)
    result = source.fetch(since, until)
    out: dict[str, tuple[str, ...]] = {}
    for it in result.items:
        if it.entities:
            out[it.source_id] = it.entities
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="落库（缺省 dry-run）")
    ap.add_argument(
        "--max-windows",
        type=int,
        default=0,
        help="限制重拉的日期窗口数（0=全部）；用于小批量试点",
    )
    args = ap.parse_args()

    store = Store(os.environ.get("FD_INFO_DB", "./fd_info.db"))
    try:
        dates = store.distinct_published_dates(SOURCE)
        if not dates:
            print(f"{SOURCE}: 无存量条目，无需回填")
            return 0
        targets = dates if args.max_windows <= 0 else dates[: args.max_windows]
        print(f"{SOURCE}: 存量覆盖 {len(dates)} 个发布日，本次处理 {len(targets)} 个")

        code_map = _fetch_window(targets)
        print(f"重拉得到 {len(code_map)} 条带证券代码的条目")

        # 逐条比对：只影响本仓已存、且确能补到代码的条目
        pending: list[tuple[str, tuple[str, ...]]] = []
        unbackfillable = 0
        for d in targets:
            page = store.list_since(
                sources=[SOURCE], published_after=f"{d}T00:00:00Z", limit=500
            )
            for row in page["items"]:
                if not row["published_at"].startswith(d):
                    continue
                if row.get("entities"):
                    continue  # 已有代码，跳过
                codes = code_map.get(row["source_id"])
                if codes:
                    pending.append((row["item_id"], codes))
                else:
                    unbackfillable += 1

        print(f"待更新 {len(pending)} 条；无法回填 {unbackfillable} 条（来源侧已无此项，如实保持空）")
        if not args.apply:
            print("dry-run：未写库（加 --apply 落库）")
            return 0

        changed = 0
        for item_id, codes in pending:
            if store.set_entities(item_id, codes):
                changed += 1
        print(f"已更新 {changed} 条")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
