# Fitbit 移动健康面板

## 目标

把适合随手查看的健康上下文带到 Akashic 手机端，同时保持 Fitbit 数据、接口和界面归插件所有。宿主只注册入口、加载资源并转发 `mobile_ui_call`，不新增 Fitbit 专用字段。

```text
插件抽屉
└── 健康状态
    ├── 当前睡眠状态 | 心率 / 血氧 / 步数
    ├── 最近 24 小时睡眠节律
    └── 最近 7 天睡眠摘要
```

## 设计约束

- 任务优先：第一屏先回答“我现在怎么样”，再提供节律和历史。
- 颜色承担领域语义：睡眠紫、心率珊瑚、血氧青蓝、步数绿；陈旧数据才使用警告色。
- 顶部指标是一个统一健康状态组；历史记录使用页面平面上的分隔列表，不做卡片墙。
- 只展示 monitor 已有事实，不根据单次读数生成医疗诊断。
- 首版只读；不在手机端复制桌面 Dashboard 的配置、调试和模型训练能力。
- 当前快照与七天历史是两个独立失败域；一侧不可用时另一侧继续显示，重试只刷新失败区域。
- 网络中断时保留明确错误并提供原位重试，不在后台无限轮询。

## 数据边界

`FitbitMobileDashboardReader` 位于插件入口模块中，以兼容宿主的隔离模块加载契约，并读取插件托管的本地 monitor：

- `/api/tool/fitbit_health_snapshot`
- `/api/sleep_report?days=7`

外部 HTTP payload 在 reader 中集中校验；边界之后，移动模块使用稳定投影。`fitbit.current` 只读取当前快照，`fitbit.sleep_history` 只读取七天报告，任何一侧失败都不会取消另一侧。24 小时节律只接受 `sleeping / awake / unknown`，不足 1440 分钟的区间以前置无数据补齐，超过时只保留最近 1440 分钟。插件不可用或 payload 损坏时明确失败，不返回假数据。

## 验收

```bash
npm install
npm run test:mobile
PYTHONPATH=/mnt/data/coding/akasic-agent:$PWD:<fitbit-cache-site-packages> \
  /mnt/data/coding/akasic-agent/.venv/bin/python -m pytest -q
PYTHONPATH=/mnt/data/coding/akasic-agent:$PWD \
  /mnt/data/coding/akasic-agent/.venv/bin/pyright plugin.py
```

Pixel7 隔离环境验收：

1. 从抽屉的插件入口进入“健康状态”。
2. 确认实时读数、24 小时节律和 7 天摘要来自隔离插件数据；血氧使用自己的延迟值，不借用整体新鲜度。
3. 只让七天报告返回 401，确认当前读数与 24 小时节律仍在，只有历史区原位报错。
4. 恢复报告并只点历史重试，确认当前区域不闪烁、WebSocket generation 和 epoch 不变化。
5. 确认返回键依次回到插件目录和聊天，无白屏，正式 workspace 没有写入。

## Pixel7 验收结果

- 正式安装链把插件热载入隔离 Mobile Lab，运行代次从 `d929ed7ba29a:9` 切换到 `d5db99c26e2b:10`；手机不需要重装 APK。
- 真机首先暴露宿主样式覆盖 HTML `hidden` 的问题；插件在自己的作用域恢复隐藏语义，加载态与内容不再同时显示。修复后完整面板截图为 `/tmp/pixel7-fitbit-hidden-fixed.png`。
- 临时移走隔离 Fitbit 令牌后，当前快照仍显示心率 95、血氧 93.8、步数 3483 和 24 小时节律；只有七天历史收到 401 并原位出现重试，截图为 `/tmp/pixel7-fitbit-history-isolated-error.png`。
- 血氧明确使用自己的 `spo2_lag_min=1121`，以红色文字标记陈旧；顶部整体数据延迟为 974 分钟，两者没有混用。
- 恢复令牌后只点“重试睡眠历史”，Android 仅新增一次 `fitbit.sleep_history` 调用，当前区域没有重新请求；七天列表恢复。整个过程保持 generation 14、epoch 276，没有 `device.proof`、`resume` 或全量历史同步。截图为 `/tmp/pixel7-fitbit-history-retry-restored.png`。
- 正式 workspace 全程未读写，隔离令牌已恢复。
