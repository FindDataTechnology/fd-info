"""信源注册表：启停与按名取 fetcher。"""

from __future__ import annotations

from .base import Source
from .cninfo import CninfoSource
from .govcn import GovcnSource
from .interaction import InteractionSource
from .ministry import MinistrySource
from .news import NewsSource

SOURCES: dict[str, type[Source]] = {
    CninfoSource.name: CninfoSource,
    MinistrySource.name: MinistrySource,
    InteractionSource.name: InteractionSource,
    NewsSource.name: NewsSource,
    GovcnSource.name: GovcnSource,
}

#: 默认启用：公告、部委与新闻 RSS。互动待上游恢复后加入。
#: govcn 政策库是**大源**（万级、逐篇正文），不进 DEFAULT_ENABLED——它有自己的
#: CronJob 显式 --source govcn 驱动（并靠 --max-pages 分多轮续爬），不拖慢默认轮。
DEFAULT_ENABLED: tuple[str, ...] = (CninfoSource.name, MinistrySource.name, NewsSource.name)


def enabled_sources(names: list[str] | None = None) -> list[Source]:
    chosen = names or list(DEFAULT_ENABLED)
    return [SOURCES[name]() for name in chosen if name in SOURCES]
