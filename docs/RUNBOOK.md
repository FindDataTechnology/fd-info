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
