# fd-info Architecture

## 双写分工

```
  fetcher (5min CronJob)
      │  SourceResult(ok|unavailable|partial)
      ▼
  ingest_result ──► info_items (本包, SQLite/PG)   索引/游标/健康度
      │                    │
      │                    └── item_id = sha1(source:source_id)
      ▼
  BodyStore ──► JSONL spool ──► fd-text-core corpus_documents
                                 (news 垂域, 去重/版本/切块/检索)
```

- **本包不存正文**：避免两套真相。`doc_id` 与 `source:source_id` 同形，text-core
  的 `(domain, source, source_id)` 唯一约束直接复用。
- **身份与版本**：`(source, source_id)` 决定 `item_id`；`raw_hash` 判内容变化。
  标题/正文修订更新同一行并触发新正文版本；撤回置 `withdrawn`，不删除。
- **游标**：`(published_at, item_id)` 键集分页，不用 OFFSET。翻页途中插入新条目
  不会造成第二页重复或遗漏。
- **三态**：`ok` 必须显式 `window_checked=True`；空列表不能被默认当成成功。
  `partial` 必须列缺失项，`unavailable` 必须给原因。

## 失败语义

| 情况 | 行为 |
|---|---|
| 来源不可用 | 记 `source_state.consecutive_failures`，返回 `unavailable`，不中断其他来源 |
| 正文不可取 | 条目保留，`body_status=failed`；分析面必须标注"正文未核实" |
| 部分板块失败 | `partial` + `missing[]`；健康度可见 |
| 区间越界 | `locate_evidence` 返回结构化错误，不返回空片段 |

## 检索降级

`search_info` 返回 `mode`：

- `text-core`：配置了 `FD_TEXT_CORE_URL`，走混合检索（语义 + 关键词）。
- `index`：未配置或 text-core 不可用，只搜标题/摘要。**这是显式降级，不是等价替代。**

## 部署形态

- 一轮拉取 = 一条 k8s CronJob（`*/5 * * * *`），非常驻服务。
- MCP 面 = 一个共享实例（`/fd-info`），只读工具五个。
- 正文 spool 与索引库都挂持久卷；spool 由 text-core 摄取后可按保留策略清理。
