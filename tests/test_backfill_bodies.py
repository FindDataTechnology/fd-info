"""正文回填命令：终态分类与逐条留痕。

2026-11-11 生产实证两个缺口：
  1. 一份超大 PDF 把 512Mi 的回填容器打成 OOMKilled（exitCode 137），整轮中断；
  2. OOM 时进程日志**全空** —— 连"取到哪一条"都无从判断，只能靠兄弟 pod 的
     汇总行反推。
所以这里钉两件事：oversize 是终态（落库、不重试、不占队头），且每个处理过的
条目必须先在 stderr 留下一行。
"""
from __future__ import annotations

import json

import pytest

from fd_info import cli
from fd_info.models import InfoItem


def _item(n: int = 1) -> InfoItem:
    return InfoItem(
        source="cninfo",
        source_id=f"12000000{n:02d}",
        source_type="announcement",
        published_at="2026-11-10T00:00:00Z",
        title=f"公告 {n}",
        url=f"https://static.cninfo.com.cn/finalpage/2026-11-11/12000000{n:02d}.PDF",
        body_status="failed",
    )


def _run(tmp_path, monkeypatch, fetcher, capsys) -> tuple[int, dict, str]:
    db = tmp_path / "fd_info.db"
    monkeypatch.setenv("FD_INFO_DB", str(db))
    monkeypatch.setenv("FD_INFO_SPOOL", str(tmp_path / "spool"))

    from fd_info.storage import Store

    store = Store(db)
    store.upsert(_item(1))
    store.close()

    monkeypatch.setattr(cli, "cninfo_body_detailed", fetcher)
    import argparse

    rc = cli.cmd_backfill_bodies(argparse.Namespace(source=None, limit=10, sleep=0))
    out, err = capsys.readouterr()
    summary = json.loads(out.strip().splitlines()[-1])
    return rc, summary, err


def test_oversize_is_terminal_and_does_not_fail_the_run(tmp_path, monkeypatch, capsys):
    rc, summary, _ = _run(tmp_path, monkeypatch, lambda url: (None, "oversize"), capsys)
    assert rc == 0, "终态不是故障，整轮不该判红"
    assert summary == {"checked": 1, "backfilled": 0, "still_failed": 0, "skipped": 1}

    from fd_info.storage import Store

    store = Store(tmp_path / "fd_info.db")
    row = store._conn.execute("select body_status from info_items").fetchone()
    store.close()
    assert row["body_status"] == "oversize", "终态必须落库，否则明天还排在队头"


def test_each_attempted_item_leaves_a_stderr_line(tmp_path, monkeypatch, capsys):
    """OOM 时进程没有 stdout —— 归属只能靠 stderr，且必须在取正文**之前**落一行。"""
    _, _, err = _run(tmp_path, monkeypatch, lambda url: (None, "no_text"), capsys)
    lines = [json.loads(l) for l in err.strip().splitlines() if l.startswith("{")]
    assert [l["phase"] for l in lines] == ["start", "done"], "start 行是 OOM 时唯一的遗言"
    assert lines[0]["url"].endswith("1200000001.PDF")
    assert lines[1]["status"] == "no_text" and "secs" in lines[1]


def test_retryable_failure_stays_failed_and_fails_the_run(tmp_path, monkeypatch, capsys):
    rc, summary, _ = _run(tmp_path, monkeypatch, lambda url: (None, "failed"), capsys)
    assert rc == 1
    assert summary["still_failed"] == 1

    from fd_info.storage import Store

    store = Store(tmp_path / "fd_info.db")
    row = store._conn.execute("select body_status from info_items").fetchone()
    store.close()
    assert row["body_status"] == "failed", "可重试类必须留在队列里"
