"""部委发布 fetcher：复用 fd-cn-gov 的 11 个列表脚本。

不在本仓复制选择器。适配器动态导入 ``fd_cn_gov.scripts.<name>``，读其
MANIFEST.archives 与 ``crawl_archive``——原仓零改动；未安装 fd-cn-gov 时
如实返回 unavailable，不假装成功。

正文（详情页）由 ``fd_cn_gov.detail.fetch_detail`` 取；同样为可选依赖。
"""

from __future__ import annotations

import importlib
from typing import Callable, Iterable

from ..models import InfoItem, SourceResult
from .base import FetchWindow, item_from_row

SOURCE = "cn-gov"

#: 11 个部委脚本（与 fd-cn-gov/fd_cn_gov/scripts/ 同名）
SCRIPTS: tuple[str, ...] = (
    "mee_gsgg_archive",
    "mem_tzgg_archive",
    "mnr_tzgg_archive",
    "moa_govpublic_archive",
    "mof_gkml_archive",
    "mofcom_xwfb_archive",
    "mohurd_xinwen_archive",
    "mot_shuju_archive",
    "ndrc_tzgg_archive",
    "pbc_xinwen_archive",
    "safe_whxw_archive",
)


class MinistrySource:
    name = SOURCE

    def __init__(
        self,
        *,
        scripts: Iterable[str] = SCRIPTS,
        module_loader: Callable[[str], object] | None = None,
        max_pages: int = 3,
    ) -> None:
        self.scripts = tuple(scripts)
        self._load = module_loader or self._import
        self.max_pages = max_pages

    def fetch(self, since: str, until: str) -> SourceResult:
        window = FetchWindow(since, until)
        items: list[InfoItem] = []
        missing: list[str] = []
        unsupported: list[str] = []
        ran = 0
        for name in self.scripts:
            try:
                module = self._load(name)
                if not hasattr(module, "crawl_archive"):
                    # 有 MANIFEST 但没有列表抓取（详情型/接口型脚本）——是能力边界，
                    # 不是运行失败。记为 unsupported，不让整个源变 unavailable。
                    unsupported.append(name)
                    continue
                ran += 1
                records = _crawl(module, self.max_pages)
                items.extend(_within(records, window, name))
            except Exception as exc:  # noqa: BLE001 - 单源失败是一条数据，不是整批中断
                missing.append(f"{name}: {exc}")
        if ran == 0 and not items:
            return SourceResult(SOURCE, "unavailable", reason="; ".join(missing or unsupported))
        if missing or unsupported:
            reason = "部分部委不可用或未实现列表抓取"
            return SourceResult(
                SOURCE, "partial", items=tuple(items), reason=reason,
                missing=tuple(missing + unsupported), window_checked=True,
            )
        return SourceResult(SOURCE, "ok", items=tuple(items), window_checked=True)

    @staticmethod
    def _import(name: str):
        return importlib.import_module(f"fd_cn_gov.scripts.{name}")


def _crawl(module: object, max_pages: int) -> list[dict]:
    crawl = getattr(module, "crawl_archive", None)
    archives = getattr(getattr(module, "MANIFEST", None), "archives", None) or []
    if crawl is None:
        raise RuntimeError("模块没有 crawl_archive")
    rows: list[dict] = []
    for archive in archives:
        section = getattr(archive, "section", "")
        subsection = getattr(archive, "subsection", section)
        url = getattr(archive, "url", "")
        rows.extend(crawl(section, subsection, url, max_pages))
    return rows


def _within(records: list[dict], window: FetchWindow, script: str) -> list[InfoItem]:
    out: list[InfoItem] = []
    for row in records:
        date = str(row.get("date") or "")[:10]
        if not date or date < window.since or date > window.until:
            continue
        url = str(row.get("url") or "").strip()
        title = str(row.get("title") or "").strip()
        if not (url and title):
            continue
        out.append(
            item_from_row(
                source=SOURCE,
                source_id=url,
                source_type="ministry",
                published=date,
                title=title,
                url=url,
            )
        )
    return out


def fetch_body(url: str) -> str | None:
    """详情页正文（**干净文本**）：可选依赖 fd-cn-gov；失败返回 None（调用方标 failed）。

    2026-11-08 修：`fetch_detail` 返回的是 `html_content` —— **原始 HTML**。
    此前直接把它当正文存下，于是 spool 里 46 条 cn-gov 记录 100% 是标签汤
    （`<dt> 来 源： </dt><dd id="ly">…`、`<div class="social-icon-wrapper">`），
    再被当成语料切块、向量化，政务检索只能捞出标记。实测 1275 个 gov 切片里
    1126 个（88%）含 HTML 标签。

    正解是走 fd-cn-gov 自己的解析器（`parse_gov_doc`：多候选正文容器 + 过滤页脚
    与分享按钮 + 抽文号/成文日期），与它 `pipeline.build_record` 里
    `content=doc.body` 的口径完全一致。解析器不可用时**宁可返回 None 让这条标
    failed**，也不要把 HTML 当正文写进语料——脏数据比缺数据更难清理。
    """
    try:
        from fd_cn_gov.detail.fetch import fetch_detail
    except ImportError:
        return None
    result = fetch_detail(url)
    if not getattr(result, "ok", False):
        return None
    raw = (getattr(result, "body", "") or "").strip()
    if not raw:
        return None

    try:
        from fd_text_core.verticals.gov.parser import parse_gov_doc
    except ImportError:
        # 没有解析器就无法把 HTML 变成正文：如实失败，不落脏数据
        return None
    try:
        doc = parse_gov_doc(raw)
    except Exception:
        return None
    body = (getattr(doc, "body", "") or "").strip()
    return body or None
