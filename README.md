# 麦麦的本质就是复读机（Mai_repeater）

> 群聊复读插件：检测到群友在复读，麦麦就跟着复读一句——**原文照发、单条发出**，不会被分词器拆散。

## 它做什么

群里短时间内连续出现 **3 句完全相同的话**，而且来自 **3 个不同的人**（不含麦麦自己），就判定为"群友在复读"，麦麦立刻跟风复读一次。

```
A：好好好
B：好好好
C：好好好
麦麦：好好好        ← 插件发出
```

## 特性

- **原样复读**：走 SDK 的 `send.text` 直发通道，不经过 planner / replyer / 分段器，长文本也不会被拆成多条气泡或被 LLM 改写
- **同一句话只跟一次**：记忆期内同一段文本不会再复读，后续不管来几波都不跟
- **不打扰 planner**：复读后只向 planner 注入**一次**状态提示（告诉它"我刚复读过"），不会持续压制麦麦的发言意愿
- **防干扰**：命令消息、超长文本、指定机器人账号都不参与判定
- **可自查**：`/rp_stat` 随时查看当前窗口、已复读记忆、守卫状态

## 安装

1. 把 `Mai_repeater` 整个文件夹复制到 MaiBot 的 `plugins/` 目录
   （一键包通常在 `.../MaiBotOneKeyDesktop/<随机串>/modules/MaiBot/plugins/`）
2. 重启 MaiBot
3. 群里发 `/rp_stat` 能收到状态，说明加载成功

> ⚠️ 如果你部署过旧版（目录名 `repeater`），**先删掉它**再复制新版，
> 否则两个插件会同时工作，麦麦会复读两次。

## 配置

编辑插件目录下的 `config.toml`，改完重启（或在 WebUI 里改，支持热更新）。

| 分组 | 配置项 | 默认 | 说明 |
|------|--------|------|------|
| plugin | `enabled` | `true` | 插件总开关 |
| repeat | `min_count` | `3` | 触发所需的相同消息条数 |
| repeat | `min_senders` | `3` | 触发所需的不同发送人数 |
| repeat | `window_seconds` | `120` | 统计窗口时长（秒） |
| repeat | `max_text_length` | `300` | 超过该长度不参与复读 |
| repeat | `cooldown_seconds` | `90` | 同一群两次复读的最小间隔（秒） |
| repeat | `delay_min` / `delay_max` | `0.5` / `2.0` | 复读前的随机延迟（秒），想秒回就都设 `0` |
| repeat | `case_insensitive` | `false` | 判定时是否忽略大小写 |
| repeat | `normalize_whitespace` | `true` | 判定时是否合并连续空白（复读仍发原文） |
| once | `repeated_memory_ttl` | `300` | 同一句话的"已复读"记忆时长（秒） |
| planner_guard | `enabled` | `true` | 是否启用 planner 防二次复读 |
| planner_guard | `pending_ttl` | `180` | 待注入提示的有效期（秒），超时作废 |
| filter | `ignored_user_ids` | `[]` | 不参与判定的发送人（可填其他复读 bot 的 QQ） |
| filter | `bot_user_ids` | `[]` | 麦麦自己的 ID（防御性过滤，通常不用填） |
| debug | `dump_message_structure` | `false` | 首条消息 dump 完整结构，用于核对字段名 |
| debug | `verbose_log` | `false` | 输出窗口计数等调试日志 |

## 命令

| 命令 | 作用 |
|------|------|
| `/rp_stat` | 查看当前状态：窗口消息数、已复读记忆、最近复读、守卫与冷却剩余时间 |

## 工作原理

```
群友消息 ──► chat.receive.after_process（OBSERVE 只读旁路）
              └─► 滑动窗口统计：同文本 ≥3 条 且 ≥3 个不同发送人
                    └─► ctx.send.text(原文)  ──► Platform IO ──► QQ
                          └─► 发送成功后挂一条 pending
                                └─► 下一次 planner 请求：extra_prompt 追加一次，立即消费
```

三个设计要点：

1. **为什么不会被分词器破坏**
   "分词/分段"发生在 replyer 侧（planner 决策 → replyer 生成 → 分段器拆条）。插件用 `ctx.send.text` 走独立的发送通道，全程不经过这三个环节，文本原样单条发出，多长都一样。

2. **为什么 planner 只注入一次**
   planner 的默认动作就是 `no_reply`，且连续不动作会触发 `no_action_backoff`（15s→300s 递增退避）。若持续注入"不要再复读"这类抑制性提示，麦麦会明显变沉默、掉发言命中。所以只在复读后注入一次、立即消费。

3. **不越权**
   消息监听用 OBSERVE 只读旁路，不拦截、不修改任何消息；manifest 只声明 `send.text` 一个能力。

## 常见问题

| 现象 | 排查 |
|------|------|
| 麦麦完全不复读 | 检查 `plugin.enabled`；只有 2 条或同一人重复不会触发；群里发 `/rp_stat` 看窗口计数 |
| 日志提示 `send.text 返回 False` | 复读消息被 `output_blocker` 等出站 Hook 拦截了（符合预期） |
| 复读慢半拍 | `delay_min/delay_max` 默认 0.5~2 秒，故意的；想秒回设成 `0` |
| 过一阵再刷同一个梗不跟了 | `once.repeated_memory_ttl`（默认 5 分钟）太长，调小即可 |
| 和其他复读 bot 互相复读 | 把对方 QQ 填进 `filter.ignored_user_ids` |
| 首次部署想确认字段名 | 开 `debug.dump_message_structure`，首条消息会 dump 完整结构（只输出一次），确认后关掉 |

## 开发

跑自测（mock ctx，不依赖真实 MaiBot）：

```bash
cd "D:/workdoc/plugin"
"C:/Users/octmicy/AppData/Local/Programs/Python/Python313/python.exe" Mai_repeater/tests/run_self_test.py
```

想用部署环境自带的 SDK 验证兼容性，加环境变量 `MAIBOT_SDK_PATH=""` 即可。

## 许可证

MIT
