"""信源注册表：启停与按名取 fetcher。"""

from __future__ import annotations

from .base import Source
from .cninfo import CninfoSource
from .interaction import InteractionSource
from .ministry import MinistrySource
from .news import NewsSource

SOURCES: dict[str, type[Source]] = {
    CninfoSource.name: CninfoSource,
    MinistrySource.name: MinistrySource,
    InteractionSource.name: InteractionSource,
    NewsSource.name: NewsSource,
}

#: 默认启用：公告、部委与新闻 RSS。互动待上游恢复后加入。
DEFAULT_ENABLED: tuple[str, ...] = (CninfoSource.name, MinistrySource.name, NewsSource.name)


def enabled_sources(names: list[str] | None = None) -> list[Source]:
    chosen = names or list(DEFAULT_ENABLED)
    return [SOURCES[name]() for name in chosen if name in SOURCES]
