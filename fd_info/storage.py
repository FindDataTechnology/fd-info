"""SQLite 索引与来源状态（design D1/D2）。

幂等由库层唯一约束保证：应用层"先查再插"在并发轮询下会漏，唯一索引不会。
游标翻页按 ``(published_at, item_id)`` 键集分页——不用 OFFSET，插入新条目
不会导致第二页重复或遗漏。
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import InfoItem, utc_now

#: 带主体归属的来源集合（info-entity-attribution）。新增会为条目填 entities 的
#: 源时须同步此集合——它决定「该源不支持主体标注」空态的判定（本设计 D4）。
ENTITY_ATTRIBUTED_SOURCES = frozenset({"cninfo", "sse-interaction"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS info_items (
    item_id       TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    source_type   TEXT NOT NULL,
    published_at  TEXT NOT NULL,
    ingested_at   TEXT NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT,
    url           TEXT,
    lang          TEXT NOT NULL DEFAULT 'zh',
    entities      TEXT NOT NULL DEFAULT '[]',
    raw_hash      TEXT NOT NULL,
    doc_id        TEXT,
    body_status   TEXT NOT NULL DEFAULT 'pending',
    withdrawn     INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_items_cursor
    ON info_items (published_at DESC, item_id DESC);
CREATE INDEX IF NOT EXISTS idx_items_source
    ON info_items (source, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_body_status
    ON info_items (body_status);

CREATE TABLE IF NOT EXISTS source_state (
    source              TEXT PRIMARY KEY,
    watermark_published TEXT,
    watermark_item_id   TEXT,
    last_success_at     TEXT,
    last_error          TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    enabled             INTEGER NOT NULL DEFAULT 1
);
"""


class Store:
    def __init__(self, path: str | Path = "./fd_info.db") -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ----- 写入 -------------------------------------------------------------

    def upsert(self, item: InfoItem) -> tuple[bool, bool, str]:
        """返回 ``(是否新建, 内容是否变化, item_id)``。内容不变时零副作用。"""
        row = item.to_row()
        with self._lock:
            existing = self._conn.execute(
                "SELECT raw_hash, doc_id, body_status, withdrawn FROM info_items WHERE item_id=?",
                (row["item_id"],),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """INSERT INTO info_items
                    (item_id, source, source_id, source_type, published_at, ingested_at,
                     title, summary, url, lang, entities, raw_hash, doc_id, body_status,
                     withdrawn, updated_at)
                    VALUES (:item_id, :source, :source_id, :source_type, :published_at,
                            :ingested_at, :title, :summary, :url, :lang, :entities,
                            :raw_hash, :doc_id, :body_status, :withdrawn, :updated_at)""",
                    {**row, "entities": json.dumps(row["entities"], ensure_ascii=False)},
                )
                self._conn.commit()
                return True, True, row["item_id"]
            if existing["raw_hash"] != row["raw_hash"]:
                # 内容变更：doc_id/body_status 的真值由 pipeline 正文落库后经
                # mark_body 回填；upsert 不把已存指针清掉（撤回场景无正文可写，
                # 指针必须保留——design D3：不物理删除，历史可查）。
                # updated_at：调用方给的优先，缺省盖变更时间戳（D3：另存 updated_at）。
                self._conn.execute(
                    """UPDATE info_items SET
                       title=:title, summary=:summary, url=:url, lang=:lang,
                       entities=:entities, raw_hash=:raw_hash,
                       doc_id=COALESCE(:doc_id, doc_id),
                       body_status=CASE WHEN :doc_id IS NULL
                                        THEN body_status ELSE :body_status END,
                       withdrawn=:withdrawn,
                       updated_at=COALESCE(:updated_at, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                       WHERE item_id=:item_id""",
                    {**row, "entities": json.dumps(row["entities"], ensure_ascii=False)},
                )
                self._conn.commit()
                return False, True, row["item_id"]
            if row["withdrawn"] != existing["withdrawn"]:
                self._conn.execute(
                    """UPDATE info_items SET withdrawn=?,
                       updated_at=COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                       WHERE item_id=?""",
                    (row["withdrawn"], row["updated_at"], row["item_id"]),
                )
                self._conn.commit()
                return False, True, row["item_id"]
            return False, False, row["item_id"]

    def mark_body(self, item_id: str, *, doc_id: str | None, body_status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE info_items SET doc_id=?, body_status=? WHERE item_id=?",
                (doc_id, body_status, item_id),
            )
            self._conn.commit()

    def set_entities(self, item_id: str, entities: tuple[str, ...] | list[str]) -> bool:
        """回填主体归属（info-entity-attribution）。返回是否真的改了行。

        幂等：与现存 entities 相同则不写。仅用于历史回填——新入库在 fetcher
        处直写，不走此路径。
        """
        payload = json.dumps(list(entities), ensure_ascii=False)
        with self._lock:
            row = self._conn.execute(
                "SELECT entities FROM info_items WHERE item_id=?", (item_id,)
            ).fetchone()
            if row is None:
                return False
            if (row["entities"] or "[]") == payload:
                return False
            self._conn.execute(
                "UPDATE info_items SET entities=? WHERE item_id=?", (payload, item_id)
            )
            self._conn.commit()
            return True

    def distinct_published_dates(self, source: str) -> list[str]:
        """某源已存条目的发布日期（YYYY-MM-DD）去重升序——回填重扫窗口用。"""
        rows = self._conn.execute(
            "SELECT DISTINCT substr(published_at, 1, 10) AS d FROM info_items"
            " WHERE source=? ORDER BY d",
            (source,),
        ).fetchall()
        return [r["d"] for r in rows if r["d"]]

    def untagged_items_on_date(self, source: str, date: str) -> list[dict[str, Any]]:
        """某源某发布日、entities 为空的条目全量（回填用，不分页截断）。

        ``list_since`` 的 500 行上限会漏掉单日 800-1200 条里的大部分——回填
        要全量比对，故单独一条直查。date 为 ``YYYY-MM-DD``。
        """
        rows = self._conn.execute(
            "SELECT * FROM info_items WHERE source=? AND substr(published_at,1,10)=?"
            " AND (entities IS NULL OR entities='[]')",
            (source, date),
        ).fetchall()
        return [_decode(r) for r in rows]

    def set_source_state(
        self,
        source: str,
        *,
        ok: bool,
        error: str | None = None,
        watermark_published: str | None = None,
        watermark_item_id: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO source_state (source) VALUES (?) ON CONFLICT(source) DO NOTHING",
                (source,),
            )
            if ok:
                self._conn.execute(
                    """UPDATE source_state SET
                       watermark_published=COALESCE(?, watermark_published),
                       watermark_item_id=COALESCE(?, watermark_item_id),
                       last_success_at=?, last_error=NULL, consecutive_failures=0
                       WHERE source=?""",
                    (watermark_published, watermark_item_id, utc_now(), source),
                )
            else:
                self._conn.execute(
                    """UPDATE source_state SET
                       last_error=?, consecutive_failures=consecutive_failures+1
                       WHERE source=?""",
                    (error or "unknown", source),
                )
            self._conn.commit()

    # ----- 读取 -------------------------------------------------------------

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM info_items WHERE item_id=?", (item_id,)
        ).fetchone()
        return _decode(row) if row else None

    def list_since(
        self,
        *,
        cursor: str | None = None,
        sources: list[str] | None = None,
        source_types: list[str] | None = None,
        published_after: str | None = None,
        source_codes: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        where: list[str] = []
        params: list[Any] = []
        if sources:
            where.append(f"source IN ({','.join('?' * len(sources))})")
            params.extend(sources)
        if source_types:
            where.append(f"source_type IN ({','.join('?' * len(source_types))})")
            params.extend(source_types)
        if published_after:
            where.append("published_at >= ?")
            params.append(published_after)
        if source_codes:
            # 主体归属精确成员判定（json_each 防变长代码子串误命中），
            # 置于 cursor 条件之前——只收窄结果集，翻页语义不变（设计 D2/D3）。
            placeholders = ",".join("?" * len(source_codes))
            where.append(
                "EXISTS (SELECT 1 FROM json_each(info_items.entities)"
                f" WHERE json_each.value IN ({placeholders}))"
            )
            params.extend(source_codes)
        if cursor:
            pub, item = _decode_cursor(cursor)
            where.append("(published_at < ? OR (published_at = ? AND item_id < ?))")
            params.extend([pub, pub, item])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._conn.execute(
            f"""SELECT * FROM info_items{clause}
                ORDER BY published_at DESC, item_id DESC LIMIT ?""",
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [_decode(r) for r in rows]
        next_cursor = (
            _encode_cursor(items[-1]["published_at"], items[-1]["item_id"])
            if has_more and items
            else None
        )
        result: dict[str, Any] = {"items": items, "next_cursor": next_cursor, "has_more": has_more}
        # 空态如实区分（info-entity-attribution）：提供了主体过滤但空结果时，
        # 说清是「该主体无条目」还是「限定源全不提供主体标注」。
        if source_codes and not items:
            result["empty_reason"] = empty_reason_for(sources, source_types)
        return result

    def health(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM source_state ORDER BY source").fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            lag = self._conn.execute(
                """SELECT (julianday(ingested_at) - julianday(published_at)) * 86400.0 AS lag
                   FROM info_items WHERE source=? AND body_status!='failed'
                   ORDER BY ingested_at DESC LIMIT 200""",
                (row["source"],),
            ).fetchall()
            lags = sorted(float(r["lag"]) for r in lag if r["lag"] is not None)
            p95 = lags[min(len(lags) - 1, int(len(lags) * 0.95))] if lags else None
            out.append(
                {
                    "source": row["source"],
                    "status": "ok"
                    if row["consecutive_failures"] == 0 and row["last_success_at"]
                    else "unavailable",
                    "watermark_published": row["watermark_published"],
                    "watermark_item_id": row["watermark_item_id"],
                    "last_success_at": row["last_success_at"],
                    "last_error": row["last_error"],
                    "consecutive_failures": row["consecutive_failures"],
                    "lag_p95_seconds": round(p95, 1) if p95 is not None else None,
                }
            )
        return out


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["entities"] = json.loads(data.get("entities") or "[]")
    data["withdrawn"] = bool(data.get("withdrawn"))
    return data


def empty_reason_for(
    sources: list[str] | None, source_types: list[str] | None
) -> str:
    """主体过滤空结果的如实原因（info-entity-attribution）。

    - ``sources_lack_entity_attribution``：被限定的源全在不带主体标注集合内——
      空是「该源无主体标注」而非「该主体无条目」。
    - ``no_items_for_subject``：其余情形——该主体在窗口内确实无条目。

    source_types 在此不参与判定（主体标注按源，不按类型）。
    """
    if sources and set(sources).isdisjoint(ENTITY_ATTRIBUTED_SOURCES):
        return "sources_lack_entity_attribution"
    return "no_items_for_subject"


def _encode_cursor(published_at: str, item_id: str) -> str:
    raw = json.dumps({"p": published_at, "i": item_id}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return str(raw["p"]), str(raw["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"游标不合法：{cursor!r}") from exc
