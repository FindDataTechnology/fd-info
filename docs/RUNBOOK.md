# fd-info 运行手册

> wire-info-stream-program 5.5 交付物。按本手册可在干净环境复现一次启停与回滚。
> 单一事实源：`fd-infra-deploy/all-services/prod/fd-info.yaml`（ArgoCD app `all-services-prod`）。

## 拓扑（cheap 集群 fd-prod）

| 物件 | 名字 | 说明 |
|---|---|---|
| Deployment | `fd-info` | 查询面 MCP（单副本；SQLite 落 PVC） |
| Service | `fd-info` | NodePort **31905**（31890 被 ops-console 占用，勿改回） |
| PVC | `fd-info-data` | 2Gi local-path RWO；库 `/data/fd_info.db` + spool `/data/spool` |
| CronJob ×3 | `fd-info-fetch-{cninfo,cn-gov,sse-interaction}` | `*/5` 摄取；独立开关（suspend 即停单源） |
| CronJob | `fd-info-ship-spool` | `*/5` spool → text-core news 域（fd-text-core 镜像） |

镜像：`ccr.ccs.tencentyun.com/finddata/fd-info:sha-<sha>`（scs001/fd-info-images 公开快照线 → hkccr → tcr-relay → ccr）。
**出片单元 = 快照仓**：同级 `fd-cn-gov/` 目录是 cn-gov 源运行时依赖，随片携带。

## 启停

```bash
# 停某源摄取（独立失败面：一源挂不影响其他源）
kubectl --context cheap -n fd-prod patch cronjob fd-info-fetch-cninfo -p '{"spec":{"suspend":true}}'
# 恢复
kubectl --context cheap -n fd-prod patch cronjob fd-info-fetch-cninfo -p '{"spec":{"suspend":false}}'
# 整体下线（ArgoCD selfHeal 会还原手工删除，改 Git 才是正道）
cd fd-infra-deploy && git revert <引入 commit> && git push gitee main
```

## 看健康度

```bash
# 途径一：MCP stream_health（容器内）
kubectl --context cheap -n fd-prod exec deploy/fd-info -- python -c \
  "import urllib.request,json; print(json.load(urllib.request.urlopen('http://127.0.0.1:8081/health',timeout=5)))"
# 途径二：网关条目（registry 自带健康探测）
kubectl --context cheap -n mcp-registry exec deploy/registry -- curl -s 127.0.0.1:7860/api/servers/health
# 途径三：单轮摄取结果
kubectl --context cheap -n fd-prod create job --from=cronjob/fd-info-fetch-cninfo <name> && \
kubectl --context cheap -n fd-prod wait --for=condition=complete job/<name> && \
kubectl --context cheap -n fd-prod logs job/<name>
```

三态口径：`ok` / `partial(missing=[...])` / `unavailable(reason)`。SZSE 互动易上游不可用是**已知态**
（irm.cninfo 405/SPA 化，2026-10-23 实调），sse-interaction 每轮如实报 partial，不是故障。

## 回滚证据门（fd-wire）

`EVIDENCE_GATE_MODE=off|warn|enforce`（fd-wire api 配置，默认 warn）：

- **off** = 完全跳过（回滚位；无 evidence 产物三档行为不变）
- **warn** = 校验失败只记结构化告警（`evidence_gate warn {json}`），不阻断发布
- **enforce** = 缺证据强断言发布 422

观察期（4.3）一个发布周期无误伤后，才切 enforce（4.4）。回滚 = 改回 warn/off + 重启 fd-wire api。

## 已知坑（都咬过人）

1. **NodePort 双验**：Service 枚举无行 ≠ 可用；`nc -z <node-ip> <port>` 双节点实测（31901-31903 无 Service 行但被 hostNetwork 占用）。
2. **`FD_TEXT_CORE_URL` 必须带 `/mcp`**：不带则 404 且被 textcore_search 容错吞成空结果。
3. **嵌入端点集群内走 sub2api 内网**（`http://sub2api.sub2api.svc.cluster.local:8080/v1`）：router 公网域在集群内出 502 upstream_error（kg-lane 同款坑）；spool loader 的 CronJob 已显式覆盖该 env。
4. **scrapling 必须 `>=0.4,<0.5`**：0.5 移除 `AsyncDynamicSession`，11 部委脚本导入即断。
5. **迁移无 runner**：fd-text-core 生产库迁移手工执行
   `kubectl exec deploy/fd-text-core -- sh -c 'cd /app && alembic -c fd_text_core/alembic.ini upgrade head'`。
6. **本机 7892 代理**：任何回环/内网 httpx 调用前 `NO_PROXY='*'`。

## 正文回填 OOM 与 cninfo 全板块 504（2026-11-11 生产实证）

**回填 OOM（已修）**：`fd-info-backfill-bodies`（每日 22:30 UTC）曾用
`resources: {}`，吃命名空间 `fd-prod` 的 LimitRange 默认 `512Mi`。队头一份大 PDF
把容器打成 `OOMKilled / exitCode 137`，**且日志全空**——连取到哪一条都无从判断。
同镜像实测 pypdf 峰值：16MB→97.6MiB、22MB→126.8MiB、54MB→237.5MiB（约 4.4×体积）。
三处收口：①清单显式 `requests 256Mi / limits 1Gi`；②`MAX_PDF_BYTES=96MiB` 流式
硬限，超限判 `oversize` 终态落库（不重试、不占队头）；③每处理一条先打 `start`
再打 `done` 到 stderr。判据：一轮跑完 stdout 末尾是
`{"checked":N,...,"skipped":M}` 且 `status.succeeded=1, status.failed` 为空。

**cninfo 全板块 504（不改代码）**：`sse/szse/bse` 同时 504 是上游
`/new/hisAnnouncement/query` 的瞬时抖动，`CninfoSource.fetch` 正确判
`unavailable` 并带全部三块 reason，`cmd_fetch` 退出 1，由 **Job 层 backoff 重试**
自愈（实测 8 个 Error pod 之后连续 3 轮 `ok, total=1200`）。判据：先直连
`POST` 该端点看是否 200——上游恢复就别改重试语义，否则会把瞬时故障固化成"正常"。
