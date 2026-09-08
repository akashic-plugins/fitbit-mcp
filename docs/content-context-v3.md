# Fitbit Content 与睡眠上下文

Fitbit v3.1 不再登记 proactive source，也不再用 MCP 工具搬运主动事件。插件只组合已有能力：

```text
Fitbit 公网
    │
    ▼
monitor（唯一采集 owner）
    │ 本机 /api/agent，一次快照
    ▼
FitbitContentRuntime
    ├── health events ──▶ Content.submit ──▶ Wake / Delivery
    │                                           │
    │                                           ▼
    │                    Content.unsettled ◀── delivered
    │                           │
    │                           ▼
    │                    monitor desired-state ACK
    │                           │
    │                           ▼
    │                       Content.ack
    │
    └── sleep ──▶ adapter.sqlite3 current cache
                         │
                         ▼
                before Turn：仅 channel=wake 且未过期时追加 hint
```

## 不变量

1. `TIMERS` 只等待一次；插件在成功 tick 后持久化 `next_due` 并重新登记。
2. monitor 仍是 Fitbit 公网采集的唯一 owner；adapter 只读本机 `/api/agent`。
3. 健康事件先提交 Content，随后才原子更新插件私有的睡眠缓存与 `next_due`。
4. 睡眠不进入 Content，也不因状态变化单独唤醒；它只给现有 Wake Turn 增加上下文。
5. 外部 ACK 以“不再 pending”为成功事实。即使 ACK HTTP 返回后进程崩溃，下一轮也会确认事件已不在队列，再执行 `Content.ack`。
6. candidate 可以启动隔离 monitor 并完成 MCP handshake，但不会收到 `RUNTIME_STARTED`，因此不会登记 Timer、轮询 `/api/agent`、ACK 或写正式数据。

## 私有持久状态

`adapter.sqlite3/source_state` 只有一行：

- `next_due`：下一次本地 monitor 采集时间；
- `sleep_json`：最近一次睡眠判断；
- `sleep_observed_at` 与 `sleep_expires_at`：上下文新鲜度。

该文件不会复制 Content 的 item、delivery 或 ACK ledger。Content 继续独占这些权威事实。

## 保留与减少合同

| 对象 | owner | 正常增加 | 允许原位更新 | 逻辑失效 | 物理减少条件 | 恢复证据 |
|---|---|---|---|---|---|---|
| 已提交的健康事件、delivery 与 source ACK 完成事实 | Content | 新 item/revision 与 submission receipt 只追加 | 状态推进、selection、`settlement_ref` | `invalidated`、`abandoned`、`expired` 等 Content 状态 | 本插件没有物理减少协议 | Content row、原 payload、`settlement_ref` 与 `settled` 状态 |
| monitor pending / acked-id 队列投影 | monitor | 检测到事件时加入 pending；ACK 后加入 acked-id | monitor 现有检测状态与 pending 内容 | 事件过期或 ACK 后不再可投递 | ACK/过期可移除 pending；acked-id 超过既有固定上限可轮转 | 尚未提交前依赖 monitor 现有 state；提交后由 Content 成为唯一全量历史 owner |
| Session、Message 与 Turn | Core | 按 Core 协议追加 | Core 已批准的 metadata/terminal 状态 | 用户撤销或 Core 已定义的失效状态 | 只允许用户主动删除会话等 Core 已批准路径 | `sessions.db`、Message 与 Turn ledger |
| sleep current cache | Fitbit adapter | 首次建立 singleton | 每次成功 snapshot 覆盖 current payload 与时间 | `sleep_expires_at` 到期后不再注入 | 新 current snapshot 可以覆盖旧投影；没有历史裁切任务 | `sleep_observed_at`、`sleep_expires_at` 与当前 JSON |
| 纯诊断日志 | 各产生日志的 owner | 本实现不新增持久诊断日志 | 不适用 | 不适用 | 未来若新增，只能按固定数量轮转 | 固定轮转配置与当前日志文件 |

因此，“外部 ACK 成功”不会删除已发生的健康事实：monitor 的 pending 项可以消失，但 Content 的 settled row 仍保留原 payload 与 settlement receipt。adapter 不另造第二份历史或 ACK ledger。

## 用户工具发现

`plugin.py` 向 `TOOLS` 注册 `mcp_fitbit__fitbit_health_snapshot` 和 `mcp_fitbit__fitbit_sleep_report`。`tool_catalog.json` 保存 MCP `tools/list` 的描述和参数；更新 MCP 签名或描述时同步更新目录。插件加载不会启动 MCP，实际调用通过本插件的 MCP 路由执行。两个工具只读，可重试；参数校验归 MCP 服务所有。
