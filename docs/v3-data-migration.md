# Fitbit v3 数据迁移

Fitbit v3 只读写 `<workspace>/plugin-data/fitbit-<marketplace>`。旧数据不会在插件加载时自动移动，也不会被删除。

停用旧 Fitbit runtime 后，显式执行：

```bash
python scripts/migrate_v2_data.py \
  --workspace <workspace> \
  --marketplace github
```

迁移持有 workspace 实例锁，从 `mcp/fitbit-mcp/monitor` 复制已知数据文件，并在目标目录写入 `.fitbit-v2-migration.json` 内容回执。旧目录始终保留，作为恢复点。

进程内失败会删除本次新发布的目标文件；Core 进程崩溃后再次执行同一命令，会校验已发布内容并继续完成。目标已有不同内容或回执与文件不一致时，命令会明确失败，不覆盖数据。
