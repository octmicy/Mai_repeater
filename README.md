# 麦麦的本质就是复读机（Mai_repeater）

> 群聊复读插件：检测到群友在复读，麦麦就跟着复读一句——**原文照发、单条发出**，不会被分词器拆散。文字、表情包、QQ 原生表情都能跟。

## 它做什么

群里短时间内连续出现 **3 条完全相同的内容**，而且来自 **3 个不同的人**（不含麦麦自己），就判定为"群友在复读"，麦麦立刻跟风复读一次。

```
A：好好好
B：好好好
C：好好好
麦麦：好好好        ← 插件发出

A：[猫猫表情包]
B：[猫猫表情包]
C：[猫猫表情包]
麦麦：[同款猫猫表情包]   ← 表情包复读（可开关）

A：[微笑]
B：[微笑]
C：[微笑]
麦麦：[微笑]        ← QQ原生表情复读（可开关）
```

## 特性

- **原样复读**：走 SDK 的 `send.text` 直发通道，不经过 planner / replyer / 分段器，长文本也不会被拆成多条气泡或被 LLM 改写
- **表情包复读**：纯表情消息按图片内容指纹（SHA256）判定同款，用 `send.emoji` 原样跟发同一张图（图片/GIF/商城大表情都支持）。图片数据在内存中有上限缓存（16 张 / 24MB，自动过期清理），不会无限吃内存
- **QQ原生表情复读**：QQ 小黄脸复读时跟发**真实的 QQ 表情**（不是 `[微笑]` 字面文本）。⚠️ **仅适配 snowluma-adapter**——发送失败直接放弃本次跟发（不会改发文字）；未检测到该适配器时启动日志会提醒
- **同一句话只跟一次**：记忆期内同一段内容不会再复读，后续不管来几波都不跟
- **不打扰 planner**：复读后只向 planner 注入**一次**状态提示（告诉它"我刚复读过"），不会持续压制麦麦的发言意愿
- **防干扰**：命令消息、超长文本、混合消息、指定机器人账号都不参与判定
- **可自查**：`/rp_stat` 随时查看当前窗口、已复读记忆、守卫状态

## 安装

1. 把 `Mai_repeater` 整个文件夹复制到 MaiBot 的 `plugins/`
2. 重启 MaiBot
3. 群里发 `/rp_stat` 能收到状态，说明加载成功

## 配置

配置定义在 `config.py` 的 config_model 中；插件目录下的 `config.toml` 是**运行时配置文件**，由主程序 Runner 依据配置模型自动生成和维护（首次加载写入全部默认值），不在本仓库版本控制内。

修改配置：在 WebUI 里改（支持热更新），或编辑插件目录下已生成的 `config.toml` 后重启。

| 分组 | 配置项 | 默认 | 说明 |
|------|--------|------|------|
| plugin | `enabled` | `true` | 插件总开关 |
| repeat | `min_count` | `3` | 触发所需的相同消息条数 |
| repeat | `min_senders` | `3` | 触发所需的不同发送人数 |
| repeat | `window_seconds` | `120` | 统计窗口时长（秒） |
| repeat | `max_text_length` | `300` | 超过该长度不参与复读 |
| repeat | `cooldown_seconds` | `90` | 同一群两次复读的最小间隔（秒），对文字/表情包/QQ表情统一生效 |
| repeat | `delay_min` / `delay_max` | `0.5` / `2.0` | 复读前的随机延迟（秒），想秒回就都设 `0` |
| repeat | `case_insensitive` | `false` | 判定时是否忽略大小写 |
| repeat | `normalize_whitespace` | `true` | 判定时是否合并连续空白（复读仍发原文） |
| once | `repeated_memory_ttl` | `300` | 同一内容的"已复读"记忆时长（秒） |
| planner_guard | `enabled` | `true` | 是否启用 planner 防二次复读 |
| planner_guard | `pending_ttl` | `180` | 待注入提示的有效期（秒），超时作废 |
| filter | `ignored_user_ids` | `[]` | 不参与判定的发送人（可填其他复读 bot 的 QQ） |
| filter | `bot_user_ids` | `[]` | 麦麦自己的 ID（防御性过滤，通常不用填） |
| filter | `auto_exclude_self` | `true` | 不复读自己说的话（读 `bot.qq_account`/`bot.platforms` + 消息自带账号标识） |
| filter | `exclude_bot_names` | `true` | 文本含机器人昵称/别名时不复读（读 `bot.nickname`/`bot.alias_names`） |
| filter | `bot_names_extra` | `[]` | 额外补充的机器人称呼 |
| filter | `exclude_non_text` | `true` | 图片/语音/视频等非文本消息不参与**文字**复读（表情包/QQ表情有独立开关） |
| filter | `exclude_keywords` | `["表情包", "[表情]", …]` | 文字复读通道的排除关键词 |
| sticker | `enabled` | `true` | **表情包复读开关**：群友复读同一张表情包时跟发同款 |
| qq_face | `enabled` | `true` | **QQ原生表情复读开关**：群友复读同一个QQ小黄脸时跟发；关闭后按普通文本复读 |
| qq_face | `send_native` | `true` | ⚠️ **发送真实QQ表情（仅适配 snowluma-adapter）**。其他适配器请关闭，将以 `[微笑]` 文本形式复读 |
| debug | `dump_message_structure` | `false` | 首条消息 dump 完整结构，用于核对字段名 |
| debug | `verbose_log` | `false` | 输出窗口计数等调试日志 |

### 关于 QQ 原生表情复读的适配器说明

QQ 小黄脸在消息入站时会被文本化成 `[微笑]`（表情 id 已丢失），插件通过内置映射表（来源 koishi QFace 表，与 snowluma-adapter 同源）反查表情 id，再通过 `send.custom("face", id)` 发送真实表情。**face 消息段的出站解包只有 snowluma-adapter 支持**：

- 使用 snowluma-adapter：无需任何配置，开箱即用
- 使用其他适配器：发送失败直接不跟发（不会改发文字）；启动日志检测不到 snowluma-adapter 时会输出提醒；想按 `[微笑]` 文本形式复读就把 `qq_face.send_native` 关掉

## 命令

| 命令 | 作用 |
|------|------|
| `/rp_stat` | 查看当前状态：窗口消息数、已复读记忆、最近复读（含种类）、守卫与冷却剩余时间 |

## 工作原理

```
群友消息 ──► chat.receive.after_process（OBSERVE 只读旁路）
              └─► 消息分类：文字 / 表情包（图片hash）/ QQ表情（名称反查id）
                    └─► 滑动窗口统计：同内容 ≥3 条 且 ≥3 个不同发送人
                          ├─► 文字     → ctx.send.text(原文)
                          ├─► 表情包   → ctx.send.emoji(缓存的原图 base64)
                          └─► QQ表情   → ctx.send.custom("face", id)，失败则不跟发
                                └─► 发送成功后挂一条 pending
                                      └─► 下一次 planner 请求：追加一次提示，立即消费
```

## 常见问题

| 现象 | 排查 |
|------|------|
| 麦麦完全不复读 | 检查 `plugin.enabled`；只有 2 条或同一人重复不会触发；群里发 `/rp_stat` 看窗口计数 |
| 日志提示 `send.text 返回 False` | 复读消息被出站 Hook 拦截了（符合预期） |
| 复读慢半拍 | `delay_min/delay_max` 默认 0.5~2 秒，故意的；想秒回设成 `0` |
| 过一阵再刷同一个梗不跟了 | `once.repeated_memory_ttl`（默认 5 分钟）太长，调小即可 |
| 和其他复读 bot 互相复读 | 把对方 QQ 填进 `filter.ignored_user_ids` |
| 表情包复读出的图跟原图有细微差别 | 入站时超大图片可能被主程序压缩后重新入库，跟发的是压缩后字节，肉眼无差异 |
| 某张超大表情包没跟 | 超过主程序 `emoji.max_emoji_size_mb`（默认 5MB）的表情不会入库，插件拿不到图片数据会跳过 |
| QQ表情复读出来是文字 `[微笑]` | `qq_face.send_native` 被关闭了（这是主动选择的文本模式）；原生发送失败时插件直接不跟发，不会改发文字，看启动日志的适配器提醒 |
| 麦麦会不会复读自己说的话 | 不会。启动时从主程序配置读取机器人账号，发送者命中即跳过；适配器回送自己消息时（`account_id` 与发送者相同）同样识别 |
| 复读内容里出现「麦麦」会怎样 | 不会复读。文本命中机器人昵称/别名（含 `bot_names_extra` 补充项）直接跳过 |
| 用户手打的 `[微笑]` 文本会被当成QQ表情吗 | 会。概率低且语义一致，属于接受的误报；介意的话关闭 `qq_face.enabled` |
| 麦麦升级后防复读提示还有效吗 | 有效。插件同时兼容两种 planner 契约：MaiBot 1.2.5+ 用 Context Items（注入到 SystemMessageItem），旧版用 extra_prompt；找不到注入目标时不消费提示，保留到下次请求 |
| 首次部署想确认字段名 | 开 `debug.dump_message_structure`，首条消息会 dump 完整结构（只输出一次），确认后关掉 |

## 许可证

MIT
