# Fitbit 健康事件与睡眠状态

插件只组合已有能力：monitor 是 Fitbit 公网采集的唯一 owner，adapter 每个轮询周期读一次本机 `/api/agent`，把结果交给值班（`plugins.watch.contract.WATCH_EVENTS`）。值班服务不存在时，来源子 Fiber 不启动，MCP 工具照常可用。

```text
monitor /api/agent（一次快照）
    ├── health_events ──▶ emit("fitbit-health-alerts", key=事件 id) ──▶ 成功后立刻向 monitor 确认
    └── sleep         ──▶ emit("fitbit-sleep", "睡眠状态：...", state=True)
```

## 不变量

1. `TIMERS` 只等待一次；一轮全部交付后才持久化 `next_due` 并重新登记。
2. 健康事件先 emit、后确认。emit 失败时事件留在 monitor 队列，下一轮用同一个 key 重交，值班按 key 去重。
3. 睡眠状态每轮只交一次；`unknown`（数据还没同步）不交；文本不带每次都变的数值，状态没变时值班不记新事件。
4. 确认以"不再 pending"为成功事实；确认 HTTP 返回后进程崩溃，下一轮会再确认一次。
5. candidate 不会收到 `RUNTIME_STARTED`，不会轮询、确认或写正式数据。

## 私有持久状态

`adapter.sqlite3/source_state` 只有一行：`next_due`（下一次采集时间）与 `cutover_pending`。

从 EventMail 版本（3.3.x）升级时，存储迁移会删掉睡眠缓存列并置 `cutover_pending = 1`。第一轮 tick 对 monitor 队列里的事件只确认、不 emit（它们属于旧流程，可能已经交过），成功提交后清除标记；之后的事件才逐条交给值班。

## 用户工具发现

`plugin.py` 向 `TOOLS` 注册 `mcp_fitbit__fitbit_health_snapshot` 和 `mcp_fitbit__fitbit_sleep_report`。`tool_catalog.json` 保存 MCP `tools/list` 的描述和参数；更新 MCP 签名或描述时同步更新目录。两个工具只读，可重试。
