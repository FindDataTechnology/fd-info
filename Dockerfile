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
# 运维脚本（回填/验收）：随镜像带上，生产 pod 内可直接跑。
# 不 COPY 则 pod 里 /app/scripts 不存在（2026-11-22 回填时实测踩到）。
COPY scripts ./scripts

# scrapling 末装并钉 <0.5：fd-cn-gov 用 AsyncDynamicSession（0.4.x 有、
# 0.5.0 移除）。不钉会在 rebuild 时被顶到 0.5.x → cn-gov 全部 11 个部委源
# 导入失败、CronJob 全 Error（2026-11-22 实证）。必须最后一次装，防止
# 前述 pip 步骤把它又升上去。
RUN if [ -n "$PIP_INDEX_URL" ]; then PIPOPT="-i $PIP_INDEX_URL"; else PIPOPT=""; fi \
 && pip install --no-cache-dir $PIPOPT 'fd-cn-gov>=0.1.3' \
 && pip install --no-cache-dir $PIPOPT ".[server]" \
 && pip install --no-cache-dir $PIPOPT 'scrapling[fetchers]>=0.4,<0.5' \
 && useradd -m -u 1000 appuser \
 && mkdir -p /data/spool && chown -R appuser /data

USER appuser

EXPOSE 8080 8081
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "fd_info.server", "--http", "8080"]
