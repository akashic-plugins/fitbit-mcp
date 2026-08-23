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
