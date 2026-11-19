# fd-info image — 柏讯信息流索引 MCP（查询面 + CronJob 摄取入口同一镜像）。
#
# 发布通道：GitHub Actions -> TCR（.github/workflows/image.yml，公开仓直出）。
#
# cn-gov 源的运行时依赖（11 部委脚本顶层 import scrapling）从 PyPI 装
# （`fd-cn-gov`）。此前它靠同级目录 fd-cn-gov/ 随快照携带，那份快照只存在于
# scs001/fd-info-images；改从 PyPI 后本源仓可独立构建。
# 运行形态：
#   - 查询面（Deployment）：FD_INFO_DB/FD_INFO_SPOOL 必须指向持久卷；单副本（单文件 SQLite）。
#   - 摄取（CronJob）：python -m fd_info.cli fetch --source <name>
# 无 LLM 依赖；FD_TEXT_CORE_URL 指向集群内 fd-text-core（正文检索转调）。
FROM python:3.12-slim

ARG PIP_INDEX_URL=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FD_INFO_DB=/data/fd_info.db \
    FD_INFO_SPOOL=/data/spool

WORKDIR /app

# 线自描述端点 /.well-known/line-manifest.json（change converge-line-identity-and-keys 1.4）：
# 仓内那份是唯一真源，端点原样回吐；不 COPY 进镜像则镜像里永远 404。
COPY line-manifest.json ./
COPY pyproject.toml ./
COPY fd_info ./fd_info

RUN if [ -n "$PIP_INDEX_URL" ]; then PIPOPT="-i $PIP_INDEX_URL"; else PIPOPT=""; fi \
 && pip install --no-cache-dir $PIPOPT 'fd-cn-gov>=0.1.3' \
 && pip install --no-cache-dir $PIPOPT ".[server]" \
 && useradd -m -u 1000 appuser \
 && mkdir -p /data/spool && chown -R appuser /data

USER appuser

EXPOSE 8080 8081
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "fd_info.server", "--http", "8080"]
