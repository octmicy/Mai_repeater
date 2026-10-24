# 交接文档 — 麦麦的本质就是复读机（Mai_repeater）

> 写给接手的人（也包括未来的自己 / 下一个 AI 会话）。
> 最后更新：2026-10-24 ｜ 当前版本 **v1.4.0**

---

## 1. 这个项目是干什么的

MaiBot（麦麦）的第三方插件。**群友开始复读时，麦麦跟着复读一条**，而且必须"原文照发、单条发出"——不能经过 planner/replyer 被改写，也不能被分段器拆成好几条。v1.4.0 起支持三种内容：

```
A：好好好
B：好好好
C：好好好
麦麦：好好好        ← 文字复读：ctx.send.text 原样单条

A：[猫猫表情包]
B：[猫猫表情包]
C：[猫猫表情包]
麦麦：[同款猫猫表情包]   ← 表情包复读：ctx.send.emoji 回发同一张图

A：[微笑]
B：[微笑]
C：[微笑]
麦麦：[微笑]        ← QQ原生表情复读：ctx.send.custom("face", id)（仅适配 snowluma-adapter）
```

判定条件：滑动窗口内**同一内容 ≥ 3 条**且来自 **≥ 3 个不同的人**（不含麦麦自己）。阈值/窗口/冷却/一次记忆对三种内容统一生效。

## 2. 仓库与路径

| 项目 | 值 |
|------|-----|
| GitHub | https://github.com/octmicy/Mai_repeater |
| 本地源码 | `D:\workdoc\plugin\Mai_repeater` |
| 部署位置（真机） | `C:\Users\octmicy\AppData\Roaming\MaiBotOneKeyDesktop\0163381a22cb\modules\MaiBot\plugins\Mai_repeater` |
| 插件 id | `maibot-community.mai-repeater` |
| 许可证 | MIT |

## 3. 运行环境（写这份文档时的基线）

| 组件 | 版本 | 说明 |
|------|------|------|
| MaiBot 主程序 | **1.3.8** | 版本读 `<模块根>/pyproject.toml` 的 `project.version` |
| maibot-plugin-sdk | **2.13.0** | 部署环境自带；开发用副本在 `D:\MaiBot\plugin\maibot-plugin-sdk-2.13.0` |
| 主程序 Python | **3.12** | `...\0163381a22cb\python-env\python.exe`（注意 exe 在 python-env 根目录，**不是** `Scripts/`） |
| 开发/测试 Python | 3.13 | `C:\Users\octmicy\AppData\Local\Programs\Python\Python313\python.exe` |

> ⚠️ 主程序版本一变，Hook 契约就可能变（v1.2.0 就是因为 planner Hook 从 `extra_prompt` 改成 Context Items 而必须改代码）。升级后请按第 11 节核对。

## 4. 目录结构

```
Mai_repeater/
├── _manifest.json         插件清单（Host 严格校验，见第 9 节）
├── plugin.py              全部逻辑（单文件）
├── config.py              配置模型（8 个配置节）
├── config.toml            默认配置（config_version 必须存在）
├── qq_face_data.py        QQ表情「名称→face_id」映射表（503 项，生成自 snowluma-adapter，勿手改）
├── README.md              面向用户的说明（安装/配置/FAQ）
├── LICENSE                MIT
├── .gitignore             挡 __pycache__ / *.pyc / data / temp
├── assets/icon.jpg        WebUI 插件图标
├── +1.jpg                 图标的原始素材（保留未删）
└── tests/run_self_test.py 自测（mock ctx，不依赖真机）105 项
```

## 5. 架构：三个入口

### ① 入站检测 Hook（只读旁路）

```python
@HookHandler("chat.receive.after_process", mode=HookMode.OBSERVE)
async def on_message(self, **kwargs)
```

OBSERVE = 只读，**不拦截、不修改任何消息**（这是"不越权"的关键）。
处理顺序（任何一步命中就 return，不进入统计）：

1. 插件总开关
2. 提取 `stream_id` / `user_id` / `text`（`_extract_fields`，兼容多种序列化结构）
3. 忽略名单 `filter.ignored_user_ids`
4. **自己发的消息** `_is_self_message` → 跳过
5. **消息分类** `_classify_message`（v1.4.0）：
   - 单条只有一个 `emoji` 组件 → **sticker**（key=图片 SHA256，base64 入缓存；功能关闭或数据缺失 → 不参与）
   - 单个 `text` 组件且完整形如 `[微笑]` 且命中映射表 → **face**（key=表情名 casefold）
   - 其余 → **text**
6. text 类专属闸门（sticker/face 不经过）：空文本、`/` 开头命令、超长文本、
   **文本含机器人称呼** `_match_bot_name`、**非文本组件/排除关键词** `_exclude_reason`
7. 进入滑动窗口 `_record_and_check` → 命中则 `_mark_repeated` + `create_task(_do_repeat)`

### ② 复读发送（直发通道，按种类分流）

```python
text    → await self.ctx.send.text(text=原文, stream_id=stream_id)
sticker → await self.ctx.send.emoji(缓存base64, stream_id=stream_id)
face    → await self.ctx.send.custom("face", face_id, stream_id=stream_id)
          失败自动降级 send.text("[微笑]")
```

**为什么用直发而不是让 planner 说**：分段/改写发生在 replyer 侧（planner 决策 → replyer 生成 → 分段器拆条）。`ctx.send.*` 是独立发送通道，全程不过这三个环节，所以内容原样、单条、多长都一样。

发送成功后才挂一条 planner 待注入提示（`_pending_note`）；发送失败则不挂。

### ③ planner 防复读守卫 Hook

```python
@HookHandler("maisaka.planner.before_request", mode=HookMode.BLOCKING)
async def guard_planner(self, **kwargs)
```

目的：让 planner 知道"我刚复读过"，避免它自己再复读一遍。提示措辞按内容种类区分（文字/表情包/QQ表情）。
**关键设计见第 6 节第 3 条。**

### ④ 命令

`/rp_stat` — 查看当前会话的窗口计数、已复读记忆、最近复读（含种类标签）、守卫与冷却状态。

## 6. 五个关键设计（改代码前务必理解）

### 1. 同一内容只复读一次
`_repeated[stream_id]["kind:key"] = 时间戳`（key 带 kind 前缀，如 `text:好好好`、`sticker:<hash>`、`face:微笑`），TTL 默认 **300 秒**。记忆期内同内容任何复读潮都直接跳过。

> 为什么是 5 分钟而不是 6 小时：太长会导致群友过一阵再刷同一个梗时麦麦不跟，显得反应迟钝。只覆盖"当次复读潮"的上下文即可。

### 2. 判定用归一化内容，发送用原始数据
文字：`case_insensitive` / `normalize_whitespace` 只影响**判定**，发送用最后一条**原文**。表情包判定按图片字节 SHA256（同款表情天然同 hash），发送回放缓存里的同一张图。QQ表情判定按表情名，发送按映射表反查的 face_id。

### 3. planner 提示只注入一次（最重要）
- planner 的**默认动作就是 `no_reply`**；连续不动作会触发 `no_action_backoff`（15s→300s 递增退避）
- 这个 Hook **每次 LLM 调用前都会触发** → 如果在一个时间窗内持续灌"不要再复读"，等于一直劝它沉默，**直接掉发言命中**
- 所以做法是：复读成功后挂 pending → 下一次 planner 请求命中即注入**一次**并立即 `pop` 消费 → `pending_ttl`（180s）内没等到就作废
- 措辞用**陈述式**（"你刚才跟风复读过一次…知道就行，按正常聊天继续"），不用命令式
- **注入目标随主程序版本变**：MaiBot 1.2.5+ 走 `items`（Context Items，追加到最后一条 `SystemMessageItem` 的 `parts`；没有就插一条新的），旧版走 `extra_prompt`。两条路都保留，运行时按 kwargs 里实际存在的键判断
- 注入失败（两个键都没有）时**不消费** pending，留到下次请求

Context Item 结构（写错字段主程序会抛异常）：
```json
{"item_type":"SystemMessageItem",
 "meta":{"item_id":"唯一","logical_turn_id":null,"timestamp":"ISO时间"},
 "parts":[{"type":"text","text":"..."}]}
```

### 4. 表情包复读依赖内存缓存（v1.4.0）
SDK **没有** `emoji.get_by_hash` 这类按内容回查图片的能力，触发复读时只能用当时收到的数据：
- Hook 载荷的 emoji 组件自带 `hash`（SHA256）+ `binary_data_base64`（完整图片字节），分类时存入 `_sticker_cache[hash] = (时间, base64)`
- 缓存三重上限：TTL = 窗口 + 最大延迟 + 10s（触发必然发生在窗口期内）、**16 条**、**24MB**，超限挤最旧的
- 发送时缓存被挤出/过期 → 放弃本次跟发并告警
- base64 拿不到的场景（超 `emoji.max_emoji_size_mb`=5MB 未入库）直接不参与判定
- 发送侧事实：`send.emoji` 只收**纯 base64**（不支持路径/URL/data: 前缀）；出站到 snowluma-adapter 变成 OneBot `image sub_type=1` 动画表情段；hash 命中表情库时只累计使用次数，不会把图注册进库

### 5. QQ原生表情走 DictComponent 兜底通道（仅适配 snowluma-adapter）
- QQ 小黄脸入站时被适配器**文本化**成 `[微笑]`（face_id 丢失），核心组件模型里根本没有 face 组件
- 出站通路：`ctx.send.custom("face", id)` → Host 兜底成 `DictComponent({"type":"face","data":id})` → snowluma-adapter 的 dict 解包器还原成 OneBot face 段（**这段解包逻辑是 snowluma 特有的**）
- 映射表 `qq_face_data.py`（503 项）生成自 snowluma-adapter 的 `qq_face_map.py`（koishi QFace，2026-09-28 同步），规则与其出站编码器一致（十进制 id、同名取首个）——适配器更新后需重新生成
- 三重保护：`send.custom` 失败自动降级文本复读；`on_load` 探测不到 snowluma-adapter 时告警提醒；配置开关 `qq_face.send_native` 可整体关掉
- 误报边界：用户手打的 `[微笑]` 字面文本也会被当成表情复读（概率低、语义一致，README 已注明）

## 7. 配置项全表

| 分组 | 配置项 | 默认 | 说明 |
|------|--------|------|------|
| plugin | `enabled` | `true` | 总开关 |
| plugin | `config_version` | `1.1.0` | schema 版本，**不可删**（缺了插件被拒） |
| repeat | `min_count` | `3` | 触发所需相同消息条数 |
| repeat | `min_senders` | `3` | 触发所需不同人数 |
| repeat | `window_seconds` | `120` | 统计窗口（秒） |
| repeat | `max_text_length` | `300` | 超过不参与复读（仅文字类） |
| repeat | `cooldown_seconds` | `90` | 两次复读最小间隔（三种内容共享） |
| repeat | `delay_min` / `delay_max` | `0.5` / `2.0` | 复读前随机延迟（秒） |
| repeat | `case_insensitive` | `false` | 判定忽略大小写 |
| repeat | `normalize_whitespace` | `true` | 判定合并空白（发送仍用原文） |
| once | `repeated_memory_ttl` | `300` | 同内容"已复读"记忆时长（key 带 kind 前缀） |
| planner_guard | `enabled` | `true` | 是否注入守卫提示 |
| planner_guard | `pending_ttl` | `180` | 待注入提示有效期 |
| filter | `ignored_user_ids` | `[]` | 不参与判定的账号（其他 bot 填这里防对撞） |
| filter | `bot_user_ids` | `[]` | 手动补充机器人自身 id |
| filter | `auto_exclude_self` | `true` | 不复读自己说的话 |
| filter | `exclude_bot_names` | `true` | 文本含机器人称呼不复读 |
| filter | `bot_names_extra` | `[]` | 额外补充的称呼 |
| filter | `exclude_non_text` | `true` | 非文本组件不参与**文字**复读（emoji/face 有独立通道） |
| filter | `exclude_keywords` | `["表情包","[表情]","[图片]",…]` | 文字通道命中即跳过 |
| sticker | `enabled` | `true` | **表情包复读开关**（WebUI 按钮） |
| qq_face | `enabled` | `true` | **QQ原生表情复读开关**（WebUI 按钮） |
| qq_face | `send_native` | `true` | ⚠️ 发送真实QQ表情，**仅适配 snowluma-adapter**（WebUI 按钮带提示，失败自动降级文本） |
| debug | `dump_message_structure` | `false` | 首条消息 dump 完整结构（排查字段名用） |
| debug | `verbose_log` | `false` | 窗口计数等冗余日志 |

### 机器人自身身份怎么来的（两个新功能的实现依据）
- **账号**：`ctx.config.get("bot.qq_account")` + `bot.platforms`（形如 `qq:123456`）+ 手动 `filter.bot_user_ids`；另外消息自带的 `account_id` / `self_id` / `bot_id` 与发送者相同时，也认定为自己（适配器回送自身消息的特征）
- **称呼**：`ctx.config.get("bot.nickname")`（本机 `"麦麦"`）+ `bot.alias_names` + `filter.bot_names_extra`
- 加载时机：`on_load`；`config_reload_subscriptions = ("bot",)`，主程序 bot 配置变更时自动刷新

## 8. 开发工作流

```bash
cd "D:/workdoc/plugin"

# 1. 跑自测（72 项）
"C:/Users/octmicy/AppData/Local/Programs/Python/Python313/python.exe" Mai_repeater/tests/run_self_test.py

# 2. 用部署环境自带的 SDK 再跑一遍（验证兼容性）
MAIBOT_SDK_PATH="" "C:/Users/octmicy/AppData/Roaming/MaiBotOneKeyDesktop/0163381a22cb/python-env/python.exe" Mai_repeater/tests/run_self_test.py

# 3. 同步到真机 + 重启 MaiBot
cp -r "Mai_repeater/." ".../modules/MaiBot/plugins/Mai_repeater/"
```

自测里有两类"防复发"的静态校验，别删：

- `case_config_field_consistency`：正则扫 `plugin.py` 里的 `self.config.x.y` / `c.x.y`，逐个核对配置模型里是否真有这个字段（防字段改名残留）
- `case_manifest_capabilities`：扫 `ctx.<能力>.<方法>`，核对 `_manifest.json` 的 `capabilities` 是否都已声明（漏声明 = 运行时被拒）
- `case_lifecycle`：真跑 `on_load` / `on_unload` / `on_config_update`（mock 测试不会自动执行生命周期，这里补上）

## 9. 修改 `_manifest.json` 的注意事项

- 严格 JSON：**多余逗号、缺逗号都会直接注册失败**
- `id` 至少两段（`maibot-community.mai-repeater`），不能用下划线分隔成一段
- `version` 必须三段式 `X.Y.Z`
- `capabilities` 逐项声明，用了 `ctx.config.get` 就必须有 `"config.get"`
- `display.icon`：`type` 为 `lucide` / `emoji` / `local`；`local` 用插件内相对路径，仅 `.png/.jpg/.jpeg/.webp/.svg`，禁止在线 URL、绝对路径、`..`、符号链接
- `host_application.max_version` 现在是 `1.4.99`，麦麦升到 1.5.x 时要记得调（否则插件被版本号挡住）
- **改完必须验证**（用部署环境 python）：
  ```python
  sys.path.insert(0, r"<模块根>")
  from src.plugin_runtime.runner.manifest_validator import ManifestValidator
  ManifestValidator().validate(json.load(open("_manifest.json", encoding="utf-8")))  # -> True
  ```

## 10. 发版流程

```bash
# 1. 确认没有未提交改动（关键！）
git status

# 2. 升 _manifest.json 的 version
# 3. 提交（所有改动提交完，再打 tag！）
git add -A && git commit -m "feat: ..."

# 4. 打 annotated tag
git tag -a v1.4.0 -m "v1.4.0: ..."

# 5. 推送（本机 SSH 在沙箱内不可用，走 HTTPS + 代理）
git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 push origin main
git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 push origin v1.4.0

# 6. 验证 tag 指向的提交内容（别只看 push 成功）
git show v1.4.0:_manifest.json | head -6
```

**曾经踩过的坑**：v1.3.0 的 tag 打早了，指向的是功能提交，**里面漏掉了后一个提交的 manifest 改动**，导致 GitHub 上点开 tag 看到的是旧内容。后来用 `git tag -f` + `git push -f` 修正。教训：tag 必须在本版所有提交完成后再打，打完必须校验 tag 内的关键文件。

## 11. 主程序升级后的核对清单

1. 查版本：主程序 `pyproject.toml` 的 `project.version`；SDK 看部署环境 `maibot_plugin_sdk-*.dist-info`
2. 同步开发 SDK：把 `site-packages/maibot_sdk` 复制成 `maibot-plugin-sdk-<版本>/`，自测的 `SDK_PATH` 指过去
3. 核对用到的 Hook 是否还在（主程序 `src/` 下 Grep Hook 名）
4. **核对 Hook 入参结构**（最容易踩雷）：看 `HookSpec(parameters_schema=...)` 和触发点实际传的 kwargs
5. 核对 SDK API：装饰器签名、`ctx.*` 能力、`_BOOLEAN_SUCCESS_CAPABILITIES`
6. 跑自测（新旧 SDK 各一遍）+ 用 ManifestValidator 验 manifest

## 12. 已知问题与可改进方向

- `filter.bot_user_ids` 与 `auto_exclude_self` 有重叠：自动识别拿到账号后仍会合并手动列表，符合预期但配置项略显冗余
- QQ原生表情复读的 face 段出站解包依赖 snowluma-adapter（`DictComponent` 兜底通道）；其他适配器上发送失败会降级文本，但"发送成功但显示异常"的适配器无法识别——已有 `on_load` 探测告警兜底
- 复读判定用的是精确匹配，没有做"近似复读"（比如多了个标点）识别；表情包按字节 hash 判定，同图不同压缩版本会被视为两张
- 文字+表情混合消息（如"哈哈[表情包]"）不参与复读（分类器直接排除），未来可考虑 hybrid 发送
- `+1.jpg`（图标原始素材）目前也在仓库里，如果不想保留可以从版本控制移除（`assets/icon.jpg` 才是实际使用的）

## 13. 版本历史

| 版本 | 内容 |
|------|------|
| v1.0.0 | 首个版本：复读检测 + 直发 + 一次记忆 |
| v1.1.0 | 排除表情包等非文本消息（关键词 + 组件类型双保险） |
| v1.2.0 | 适配 MaiBot 1.2.5 的 planner Context Items 契约（原 `extra_prompt` 失效） |
| v1.3.0 | 不复读自己说的话；复读文本含机器人称呼时不复读；manifest 补 display 图标 / plugin_type=extension / max_version=1.4.99 |
| v1.4.0 | 表情包复读（send.emoji + hash 判定 + 内存缓存上限 16 条/24MB）与 QQ原生表情复读（send.custom face 段 + 内置映射表 503 项，仅适配 snowluma-adapter，失败降级文本、加载时探测告警）；消息分类重构为 text/sticker/face 三通道；两个新功能均为 WebUI 可开关配置项 |

## 14. 相关资源

- 开发技能：`C:\Users\octmicy\.workbuddy\skills\maibot-plugin-dev`（**改代码前先读，里面有 17 条踩坑记录**）
- 技能里的参考：`references/sdk-api-reference.md`（17 种 ctx 能力签名）、`references/common-pitfalls.md`（陷阱详解）
- 项目记忆：`D:\workdoc\plugin\.workbuddy\memory\2026-09-16.md`、`2026-10-17.md`（环境变更与事故复盘）
- 可行性调研：`D:\workdoc\plugin\repeat-plugin-feasibility.md`
- Git 入门指南：`D:\workdoc\plugin\git-学习与复读机发布指南.md`

## 15. 给接手者的三句话

1. **改代码前先读技能**（`maibot-plugin-dev`），里面的坑都是真金白银踩出来的。
2. **planner 相关的东西一律"少打扰"**——它的默认行为就是沉默，任何持续性的提示注入都会掉发言命中。
3. **发版顺序不能乱**：提交完 → 打 tag → 推送 → 校验 tag 内容。顺序错了 GitHub 上就会出现"版本号对了但内容是旧的"。
