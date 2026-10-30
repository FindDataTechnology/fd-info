# fd-info 部署

两件东西：一个共享 MCP 实例（索引只读）、一条 5 分钟 CronJob（拉取）。

## 前置

- 镜像先构建并推到集群可达的 registry，替换 `k8s/*.yaml` 里的 `REGISTRY/finddata/fd-info:0.1.0`。
  **不要用 `:latest`**（可变 tag + IfNotPresent = 新镜像永远进不了 Pod，见既有教训）。
- 确认 NodePort `31905` 与集群内既有分配不冲突（`PORTS.md` 已登记为预留；31897 属 lex-ops-mcp，勿复用）。

## 应用

```bash
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/cronjob-fetch.yaml
kubectl -n fd-prod rollout status deploy/fd-info
kubectl -n fd-prod get cronjob fd-info-fetch
kubectl -n fd-prod create job --from=cronjob/fd-info-fetch fd-info-fetch-manual
kubectl -n fd-prod logs -l job-name=fd-info-fetch-manual --tail=50
```

## 验收

- `GET http://<node>:31905/health` 返回 `{"status":"ok","tools":5}`。
- 手动 Job 输出 `runs[].status` 为 `ok`/`partial`；`unavailable` 时必须带原因。
- 连续跑 24h 后：重复条目零增长；`fd-info health` 的 `lag_p95_seconds` ≤ 600。

## 停止与回滚

```bash
kubectl -n fd-prod patch cronjob fd-info-fetch -p '{"spec":{"suspend":true}}'
kubectl -n fd-prod scale deploy/fd-info --replicas=0
```

索引库与 spool 都在 PVC 上；停服不删数据。回滚只回镜像 tag，不回滚库（表结构为新增）。

## 证据门（fd-wire 侧）

`fd-wire` 发布带 `evidence` 的产物时核对 fd-info。门控由 `EVIDENCE_GATE_MODE` 控制：

| 值 | 行为 |
|---|---|
| `off` | 跳过核对（排障用，勿长期） |
| `warn`（默认） | 失败只记结构化日志 `evidence_gate ...`，不阻断发布 |
| `enforce` | 失败返回 422，阻止发布 |

```bash
# 查看告警（灰度期）
kubectl -n fd-prod logs deploy/fd-wire-api --since=24h | grep evidence_gate
# 回滚到仅告警
kubectl -n fd-prod set env deploy/fd-wire-api EVIDENCE_GATE_MODE=warn
```

切 `enforce` 前必须满足：`FD_INFO_URL` 指向可达的 fd-info、`stream_health` 为 `ok`、
灰度期零误伤记录。任何一条不满足就保持 `warn`。
