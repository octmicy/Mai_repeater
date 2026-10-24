"""麦麦的本质就是复读机（Mai_repeater）插件入口。

功能：
- 复读检测：`chat.receive.after_process` OBSERVE 只读旁路监听入站消息，
  每个会话维护滑动窗口；窗口内同一内容（归一化后）达到 min_count 条
  且来自 min_senders 个不同发送人，即判定群友在复读。
- 跟风复读：通过 `ctx.send.text` 直发通道发送相同文字（原样、单条），
  不经过 planner/replyer/表达方式分段器——长文本不会被拆分或改写，
  从根本上规避"自动分词导致复读失败"的问题。
- 表情包复读（v1.4.0）：纯表情消息（单条仅一个 emoji 组件）按图片内容
  指纹（SHA256 hash）判定同款，命中复读潮后用 `ctx.send.emoji` 原样跟发
  同一张图。图片 base64 在内存中有数量/字节双上限缓存，自动过期清理。
- QQ原生表情复读（v1.4.0）：QQ 小黄脸在入站时被适配器文本化成「[微笑]」，
  按内置映射表反查表情 id 后用 `ctx.send.custom("face", id)` 跟发真实
  QQ 表情（仅适配 snowluma-adapter，发送失败自动降级为文本复读）。
- 一次复读记忆（需求1）：同一内容在记忆 TTL（默认 5 分钟）内只复读一次，
  之后无论同一段内容再来几波，都不再跟。
- planner 防复读守卫（需求2）：订阅 `maisaka.planner.before_request`，
  复读后只向 extra_prompt **追加一次**状态提示即消费掉，让 planner 知道
  "我刚复读过"，又不持续干扰其发言决策——planner 默认动作是 no_reply，
  持续注入抑制性提示会触发 no_action backoff，压低整体发言命中。

边界（不越权）：
- 消息监听为 OBSERVE 只读旁路，不拦截、不修改任何入站消息。
- 发送仅使用官方 ctx.send.* 能力（manifest 逐项声明）。
- planner 提示使用官方 extra_prompt 追加语义（陷阱8：追加不覆盖）。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections import deque
from datetime import datetime
from typing import Any, ClassVar, Iterable
import uuid

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .config import RepeaterSettings
from .qq_face_data import QQ_FACE_IDS_BY_NAME

_PLANNER_NOTE_PREFIX = "\n[复读插件状态] "
_MAX_NOTE_TEXT = 24

# planner 提示的两种注入目标（主程序版本不同，插件同时兼容）
# - 新版（MaiBot 1.2.5+ / SDK 2.8.x）：Context Items（kwargs["items"]）
# - 旧版：extra_prompt 字符串
_ITEM_SYSTEM = "SystemMessageItem"

# 非文本组件类型（主程序 message_component_data_model.py 的 format_name）：
# 纯文本消息里混入这些组件时不参与文字复读——复读它们只会变成发占位文本。
# 注意：emoji（纯表情消息）与 face 文本（QQ表情）在 v1.4.0 起有独立复读通道，
# 会先于该排除被分类器接管；此集合用于文字通道的防御性排除。
_NON_TEXT_COMPONENT_TYPES = {
    "image",
    "emoji",
    "voice",
    "video",
    "file",
    "record",
    "face",
    "mface",
    "forward",
    "music",
    "poke",
    "xml",
    "json",
}

# 复读内容种类：text=文字 / sticker=表情包图片 / face=QQ原生表情
_KIND_TEXT = "text"
_KIND_STICKER = "sticker"
_KIND_FACE = "face"
_KIND_LABELS = {_KIND_TEXT: "文字", _KIND_STICKER: "表情包", _KIND_FACE: "QQ表情"}

# QQ原生表情入站文本形如「[微笑]」（适配器按映射表文本化，face_id 已丢失）
_FACE_TEXT_PATTERN = re.compile(r"^\[([^\[\]]{1,16})\]$")

# 表情包 base64 内存缓存上限（没有按 hash 回查图片的官方能力，必须自带缓存）。
# 数量与字节双上限，且缓存条目 TTL 与复读窗口绑定，超限挤掉最旧的。
_STICKER_CACHE_MAX_ENTRIES = 16
_STICKER_CACHE_MAX_BYTES = 24 * 1024 * 1024

# snowluma-adapter 的插件 id（QQ原生表情复读的适配目标，缺失时告警提示）
_SNOWLUMA_ADAPTER_ID = "maibot-team.snowluma-adapter"


class RepeaterPlugin(MaiBotPlugin):
    """麦麦复读机主类。

    状态（均为内存态，插件重载后自然清零）：
    - _window:          stream_id -> deque[(ts, user_id, kind, key, payload)]
    - _repeated:        stream_id -> {"kind:key": 首次复读时间}（一次复读记忆）
    - _sticker_cache:   图片hash -> (最近出现时间, base64)（表情包复读发送数据源）
    - _last_repeat:     stream_id -> 最近一次复读信息（仅用于状态展示）
    - _pending_note:    stream_id -> 待注入 planner 的提示（注入一次后即消费）
    - _cooldown_until:  stream_id -> 冷却截止时间戳
    """

    config_model = RepeaterSettings

    # 自身配置热更新 + 订阅主程序 bot 配置（昵称/别名/账号变化时刷新身份缓存）
    config_reload_subscriptions: ClassVar[Iterable[str]] = ("bot",)

    def __init__(self) -> None:
        super().__init__()
        self._window: dict[str, deque] = {}
        self._repeated: dict[str, dict[str, float]] = {}
        self._sticker_cache: dict[str, tuple[float, str]] = {}
        self._last_repeat: dict[str, dict[str, Any]] = {}
        self._pending_note: dict[str, dict[str, Any]] = {}
        self._cooldown_until: dict[str, float] = {}
        self._structure_dumped = False
        # 机器人自身身份（由 ctx.config 读取，见 _refresh_identity）
        self._self_ids: set[str] = set()
        self._bot_names: list[str] = []

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def on_load(self) -> None:
        """初始化插件：读取自身身份，打印生效配置摘要。"""
        await self._refresh_identity()
        c = self.config
        self.ctx.logger.info(
            f"[复读机] 已加载: 阈值={c.repeat.min_count}条/{c.repeat.min_senders}人, "
            f"窗口={c.repeat.window_seconds}s, 冷却={c.repeat.cooldown_seconds}s, "
            f"一次记忆={c.once.repeated_memory_ttl}s, "
            f"planner守卫={'开' if c.planner_guard.enabled else '关'}"
            f"（只注入一次, 提示有效期{c.planner_guard.pending_ttl}s）, "
            f"延迟={c.repeat.delay_min}~{c.repeat.delay_max}s, "
            f"表情包复读={'开' if c.sticker.enabled else '关'}, "
            f"QQ表情复读={'开' if c.qq_face.enabled else '关'}"
            f"（真实表情{'开' if c.qq_face.send_native else '关'}）"
        )
        self.ctx.logger.info(
            f"[复读机] 自身身份: 账号={sorted(self._self_ids) or '(无)'}, "
            f"称呼={self._bot_names or '(无)'}"
        )
        await self._probe_face_adapter()

    async def on_unload(self) -> None:
        """卸载插件。"""
        self.ctx.logger.info("[复读机] 已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, Any], version: str
    ) -> None:
        """配置热更新（self=插件自身配置；bot=主程序机器人配置）。"""
        del config_data
        if scope == "self":
            c = self.config
            self.ctx.logger.info(
                f"[复读机] 配置已热更新: 阈值={c.repeat.min_count}条/{c.repeat.min_senders}人, "
                f"planner守卫={'开' if c.planner_guard.enabled else '关'}, "
                f"表情包复读={'开' if c.sticker.enabled else '关'}, "
                f"QQ表情复读={'开' if c.qq_face.enabled else '关'}"
            )
        elif scope == "bot":
            # 昵称/别名/账号变了，重新加载自身身份
            await self._refresh_identity()
            self.ctx.logger.info(
                f"[复读机] 机器人配置已更新，身份缓存刷新: "
                f"账号={sorted(self._self_ids) or '(无)'}, 称呼={self._bot_names or '(无)'}"
            )
        return None

    async def _refresh_identity(self) -> None:
        """从主程序配置读取机器人自身账号与称呼（SDK 官方 ctx.config 能力）。

        读取项：
        - bot.qq_account / bot.platforms：机器人账号（platforms 形如 "qq:123456"）
        - bot.nickname / bot.alias_names：机器人昵称与别名
        读取失败只告警，不影响复读主流程。
        """
        cfg = self.config.filter
        if not (cfg.auto_exclude_self or cfg.exclude_bot_names):
            self._self_ids, self._bot_names = set(), []
            return

        ids: set[str] = set(str(u).strip() for u in cfg.bot_user_ids if str(u).strip())
        names: list[str] = []

        try:
            qq_account = await self.ctx.config.get("bot.qq_account", "")
            if isinstance(qq_account, str) and qq_account.strip() not in ("", "0"):
                ids.add(qq_account.strip())

            platforms = await self.ctx.config.get("bot.platforms", [])
            if isinstance(platforms, list):
                for entry in platforms:
                    text = str(entry or "").strip()
                    if ":" in text:
                        account = text.split(":", 1)[1].strip()
                        if account:
                            ids.add(account)
        except Exception as exc:
            self.ctx.logger.warning(f"[复读机] 读取机器人账号失败（不影响复读）: {exc}")

        try:
            nickname = await self.ctx.config.get("bot.nickname", "")
            if isinstance(nickname, str) and nickname.strip():
                names.append(nickname.strip())

            aliases = await self.ctx.config.get("bot.alias_names", [])
            if isinstance(aliases, list):
                names.extend(str(a).strip() for a in aliases if str(a).strip())
        except Exception as exc:
            self.ctx.logger.warning(f"[复读机] 读取机器人昵称失败（不影响复读）: {exc}")

        names.extend(str(n).strip() for n in cfg.bot_names_extra if str(n).strip())

        # 去重并去掉空串，保持稳定顺序
        self._self_ids = ids
        self._bot_names = list(dict.fromkeys(names))

    async def _probe_face_adapter(self) -> None:
        """QQ原生表情复读的适配器探测（提醒用户，不做硬性拦截）。

        真实 QQ 表情通过 face 消息段发送，依赖 snowluma-adapter 的 dict 组件
        解包逻辑；其他适配器可能不支持。发送失败时插件会自动降级为文本复读，
        但这里仍在加载时主动探测并提示，让用户提前知晓。
        """
        if not self.config.qq_face.enabled:
            return
        try:
            result = await self.ctx.component.list_loaded_plugins()
        except Exception as exc:
            self.ctx.logger.warning(f"[复读机] 探测适配器列表失败（不影响复读）: {exc}")
            return
        plugins: list[str] = []
        if isinstance(result, dict):
            raw = result.get("plugins")
            if isinstance(raw, list):
                plugins = [str(p) for p in raw]
        elif isinstance(result, list):
            plugins = [str(p) for p in result]
        found = any(_SNOWLUMA_ADAPTER_ID in p or "snowluma" in p.lower() for p in plugins)
        if not found:
            self.ctx.logger.warning(
                f"[复读机] 未检测到 {_SNOWLUMA_ADAPTER_ID}：QQ原生表情复读发送真实表情仅适配 "
                "snowluma-adapter，其他适配器将自动降级为文本复读；如不需要可关闭该功能"
            )

    # ── Hook 1: 入站消息检测（OBSERVE 只读旁路） ───────────────────────

    @HookHandler(
        "chat.receive.after_process",
        name="repeater_detector",
        description="检测群友复读（只读旁路，不拦截不修改）",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_message(self, **kwargs: Any) -> None:
        """入站消息旁路检测。OBSERVE 模式返回值被忽略。"""
        if not self.config.plugin.enabled:
            return None

        msg = kwargs.get("message")
        if not isinstance(msg, dict):
            return None

        self._maybe_dump_structure(msg)

        stream_id, user_id, text = self._extract_fields(msg)
        if not stream_id or not user_id:
            return None

        ignored = {str(u) for u in self.config.filter.ignored_user_ids if str(u).strip()}
        if user_id in ignored:
            self._vlog(f"[复读机] 发送人 {user_id} 在忽略名单，不参与判定")
            return None

        # 功能①：不复读自己说的话
        if self.config.filter.auto_exclude_self and self._is_self_message(msg, user_id):
            self._vlog(f"[复读机] 消息来自机器人自身（user_id={user_id}），不参与判定")
            return None

        classified = self._classify_message(msg, text)
        if classified is None:
            return None
        kind, key, payload = classified

        if kind == _KIND_TEXT:
            stripped = text.strip()
            if not stripped or stripped.startswith("/"):
                return None
            max_len = int(self.config.repeat.max_text_length)
            if max_len > 0 and len(stripped) > max_len:
                self._vlog(f"[复读机] 文本超长({len(stripped)}>{max_len})，不参与: {stream_id}")
                return None

            # 功能②：文本里出现机器人昵称/别名，不跟读
            hit_name = self._match_bot_name(stripped)
            if hit_name:
                self._vlog(f"[复读机] 文本含机器人称呼「{hit_name}」，不参与判定")
                return None

            excluded, reason = self._exclude_reason(msg, stripped)
            if excluded:
                self._vlog(f"[复读机] {reason}，不参与判定: {stream_id}")
                return None

            key = self._normalize(text)
            if not key:
                return None
            payload = text

        self._handle_inbound(stream_id, user_id, kind, key, payload)
        return None

    # ── 消息分类：text / sticker / face ────────────────────────────────

    def _classify_message(self, msg: dict[str, Any], text: str) -> tuple[str, str, Any] | None:
        """把入站消息分类为复读单元 (kind, key, payload)。

        - 纯表情消息（单条只有一个 emoji 组件）→ sticker，key=图片内容指纹
          （SHA256 hash），发送数据（base64）存入内存缓存；
        - 单文本组件且完整形如「[微笑]」且命中内置映射表 → face，key=表情名
          （casefold），payload={id, text}；
        - 其余一律走文字复读通道（key 由调用方归一化后填写）；
        - 返回 None 表示不参与复读（对应功能关闭、图片数据缺失等）。
        """
        components = self._component_list(msg)
        if components is not None and len(components) == 1 and isinstance(components[0], dict):
            comp = components[0]
            ctype = str(comp.get("type") or comp.get("format_name") or "").strip().lower()

            if ctype == "emoji":
                return self._classify_sticker(comp)
            if ctype == "text" and self.config.qq_face.enabled:
                return self._classify_face(text)

        return (_KIND_TEXT, "", None)

    def _classify_sticker(self, comp: dict[str, Any]) -> tuple[str, str, Any] | None:
        """纯表情消息 → sticker 复读单元；功能关闭或图片数据缺失时返回 None。

        Hook 载荷中 emoji 组件带 hash（SHA256）与 binary_data_base64（完整图片
        字节）。超过 emoji.max_emoji_size_mb 未入库等场景拿不到 base64，无法
        跟发，直接跳过（与旧版"非文本不参与"行为一致）。
        """
        if not self.config.sticker.enabled:
            return None
        binary_hash = str(comp.get("hash") or "").strip()
        binary_base64 = str(comp.get("binary_data_base64") or "").strip()
        if not binary_hash or not binary_base64:
            self._vlog("[复读机] 表情包缺少图片数据（hash/base64），无法跟发")
            return None
        self._cache_sticker(binary_hash, binary_base64)
        # payload 用文本化占位（如「[表情包: 描述]」）仅供展示/planner 提示，发送走缓存
        return (_KIND_STICKER, binary_hash, str(comp.get("data") or "[表情包]"))

    def _classify_face(self, text: str) -> tuple[str, str, Any]:
        """单文本组件 → 尝试识别为QQ原生表情文本（如「[微笑]」）。

        QQ 小黄脸在入站时被适配器文本化成「[名称]」，face_id 已丢失，按内置
        映射表（qq_face_data.py）反查。未命中（用户手打的方括号文本、未知
        表情占位「[表情]」等）回落文字复读通道，保持原有行为。
        """
        stripped = text.strip()
        match = _FACE_TEXT_PATTERN.match(stripped)
        if not match:
            return (_KIND_TEXT, "", None)
        name = match.group(1)
        face_id = self._lookup_face_id(name)
        if not face_id:
            return (_KIND_TEXT, "", None)
        return (_KIND_FACE, name.casefold(), {"id": face_id, "text": stripped})

    @staticmethod
    def _lookup_face_id(name: str) -> str:
        """按表情名反查 face_id（先精确匹配，再忽略大小写）。"""
        return QQ_FACE_IDS_BY_NAME.get(name) or QQ_FACE_IDS_BY_NAME.get(name.casefold()) or ""

    def _cache_sticker(self, binary_hash: str, binary_base64: str) -> None:
        """缓存表情包图片数据（hash -> base64），并做 TTL 与容量修剪。"""
        now = time.time()
        self._sticker_cache[binary_hash] = (now, binary_base64)
        self._prune_sticker_cache(now)

    def _prune_sticker_cache(self, now: float) -> None:
        """修剪表情包缓存：TTL 过期清理，数量/字节超限挤掉最旧的。"""
        cfg = self.config.repeat
        ttl = (
            max(1.0, float(cfg.window_seconds) + max(float(cfg.delay_min), float(cfg.delay_max)))
            + 10.0
        )
        expired = [h for h, (ts, _) in self._sticker_cache.items() if now - ts > ttl]
        for h in expired:
            self._sticker_cache.pop(h, None)

        # 触发必然发生在窗口期内，TTL 之外条目不会被用到；容量上限防内存膨胀
        while len(self._sticker_cache) > _STICKER_CACHE_MAX_ENTRIES:
            oldest = min(self._sticker_cache, key=lambda h: self._sticker_cache[h][0])
            self._sticker_cache.pop(oldest, None)
        total_bytes = sum(len(b64) for _, b64 in self._sticker_cache.values())
        while total_bytes > _STICKER_CACHE_MAX_BYTES and self._sticker_cache:
            oldest = min(self._sticker_cache, key=lambda h: self._sticker_cache[h][0])
            total_bytes -= len(self._sticker_cache.pop(oldest)[1])

    def _is_self_message(self, msg: dict[str, Any], user_id: str) -> bool:
        """判断这条消息是不是机器人自己发出的。

        两个依据，命中任一即认定为自己：
        1. 发送者命中已知机器人账号——手动配置 `filter.bot_user_ids`，
           或 on_load 时从 `ctx.config` 读到的 `bot.qq_account` / `bot.platforms`；
        2. 消息自带的账号标识（`account_id` / `self_id` / `bot_id`）与发送者相同
           —— 适配器把机器人自己的消息回送时这两个值会相等。
        """
        if not user_id:
            return False

        manual = {str(u).strip() for u in self.config.filter.bot_user_ids if str(u).strip()}
        if user_id in manual or user_id in self._self_ids:
            return True

        for key in ("account_id", "self_id", "bot_id"):
            val = msg.get(key)
            if val and str(val).strip() == user_id:
                return True
        return False

    def _match_bot_name(self, text: str) -> str | None:
        """文本中出现机器人昵称/别名时返回命中的称呼，否则 None。

        称呼来源：`ctx.config` 的 `bot.nickname` / `bot.alias_names`
        + 插件配置 `filter.bot_names_extra`。
        """
        cfg = self.config.filter
        if not cfg.exclude_bot_names or not self._bot_names:
            return None
        lowered = text.casefold()
        for name in self._bot_names:
            if name and name.casefold() in lowered:
                return name
        return None

    def _exclude_reason(self, msg: dict[str, Any], text: str) -> tuple[bool, str]:
        """判断消息是否应排除在复读之外，返回 (是否排除, 原因)。

        两道保险：
        1. 组件类型：图片/表情/语音等非文本消息——插件只能发文字，
           复读它们只会变成发一段占位文本，没有意义；
        2. 关键词：MaiBot 会把表情包文本化成「[表情包：xxx]」，
           这类占位文本即使是纯文本也要排除。
        """
        cfg = self.config.filter

        if bool(cfg.exclude_non_text):
            types = self._component_types(msg)
            hit = types & _NON_TEXT_COMPONENT_TYPES
            if hit:
                return True, f"非文本消息（组件类型: {'/'.join(sorted(hit))}）"

        keywords = [str(k).strip() for k in cfg.exclude_keywords if str(k).strip()]
        if keywords:
            lowered = text.casefold()
            for kw in keywords:
                if kw.casefold() in lowered:
                    return True, f"命中排除关键词「{kw}」"
        return False, ""

    def _component_list(self, msg: dict[str, Any]) -> list[Any] | None:
        """提取消息的组件列表（兼容 dict.components / 裸列表两种序列化）。"""
        raw = msg.get("raw_message")
        components: Any = None
        if isinstance(raw, dict):
            components = raw.get("components")
        elif isinstance(raw, list):
            components = raw
        return components if isinstance(components, list) else None

    def _component_types(self, msg: dict[str, Any]) -> set[str]:
        """提取消息的组件类型集合（兼容 type / format_name 两种序列化键）。"""
        types: set[str] = set()
        for comp in self._component_list(msg) or []:
            if not isinstance(comp, dict):
                continue
            name = comp.get("type") or comp.get("format_name")
            if name:
                types.add(str(name).strip().lower())
        return types

    def _handle_inbound(self, stream_id: str, user_id: str, kind: str, key: str, payload: Any) -> None:
        """记录消息并判定是否触发复读；命中则安排发送任务。"""
        if not self._record_and_check(stream_id, user_id, kind, key):
            return
        # 触发即记账（一次记忆 + 冷却），后续同内容消息不会重复触发
        self._mark_repeated(stream_id, kind, key, payload)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._do_repeat(stream_id, kind, key, payload))
        except RuntimeError:
            self.ctx.logger.warning("[复读机] 无运行中的事件循环，放弃本次复读")

    def _record_and_check(self, stream_id: str, user_id: str, kind: str, key: str) -> bool:
        """更新滑动窗口；满足条件返回 True。"""
        cfg = self.config.repeat
        now = time.time()
        win = self._window.setdefault(stream_id, deque(maxlen=64))
        while win and now - win[0][0] > max(1.0, float(cfg.window_seconds)):
            win.popleft()

        if not key:
            return False
        win.append((now, user_id, kind, key))

        matches = [e for e in win if e[2] == kind and e[3] == key]
        senders = {e[1] for e in matches}
        min_count = max(2, int(cfg.min_count))
        min_senders = max(1, int(cfg.min_senders))
        self._vlog(
            f"[复读机] {stream_id} 内容「{self._kind_preview(kind, key)}」窗口内 "
            f"{len(matches)}条/{len(senders)}人 (需 {min_count}条/{min_senders}人)"
        )
        if len(matches) < min_count or len(senders) < min_senders:
            return False

        # 需求1：同一内容只复读一次（记忆期内，key 带 kind 前缀）
        memory = self._repeated.setdefault(stream_id, {})
        memory_key = f"{kind}:{key}"
        ttl = float(self.config.once.repeated_memory_ttl)
        if memory_key in memory and (ttl <= 0 or now - memory[memory_key] < ttl):
            self._vlog(f"[复读机] 「{self._kind_preview(kind, key)}」已复读过，本次不跟")
            return False
        if ttl > 0:
            expired = [k for k, t in memory.items() if now - t >= ttl]
            for k in expired:
                memory.pop(k, None)

        # 冷却期内不跟新的复读潮
        if now < self._cooldown_until.get(stream_id, 0.0):
            self._vlog(f"[复读机] {stream_id} 复读冷却中，不跟「{self._kind_preview(kind, key)}」")
            return False

        return True

    def _mark_repeated(self, stream_id: str, kind: str, key: str, payload: Any) -> None:
        """记账：一次复读记忆 + 待注入 planner 提示（一次性）+ 冷却。"""
        now = time.time()
        self._repeated.setdefault(stream_id, {})[f"{kind}:{key}"] = now
        self._last_repeat[stream_id] = {
            "kind": kind,
            "text": self._kind_display(kind, key, payload),
            "norm": f"{kind}:{key}",
            "ts": now,
            "sent": False,
        }
        self._cooldown_until[stream_id] = now + max(0.0, float(self.config.repeat.cooldown_seconds))
        self.ctx.logger.info(
            f"[复读机] 判定复读潮成立，准备跟读{_KIND_LABELS.get(kind, kind)}"
            f"「{self._kind_preview(kind, key)}」| stream={stream_id}"
        )

    async def _do_repeat(self, stream_id: str, kind: str, key: str, payload: Any) -> None:
        """直发复读内容（原样单条，不经过 AI 链路）。"""
        delay_min = max(0.0, float(self.config.repeat.delay_min))
        delay_max = max(delay_min, float(self.config.repeat.delay_max))
        delay = random.uniform(delay_min, delay_max) if delay_max > 0 else 0.0
        if delay > 0:
            await asyncio.sleep(delay)

        try:
            ok = await self._send_repeat(stream_id, kind, key, payload)
        except Exception as exc:
            self.ctx.logger.error(f"[复读机] 复读发送异常: {exc}")
            return

        info = self._last_repeat.get(stream_id) or {}
        if ok:
            if isinstance(info, dict):
                info["sent"] = True
            # 复读确实发出后才挂待注入提示：下一次 planner 请求消费一次即消失
            self._pending_note[stream_id] = {
                "text": self._kind_display(kind, key, payload),
                "kind": kind,
                "ts": time.time(),
            }
            self.ctx.logger.info(
                f"[复读机] 复读已发送（{_KIND_LABELS.get(kind, kind)}）: "
                f"{self._kind_preview(kind, key)} | stream={stream_id}"
            )
        else:
            self.ctx.logger.warning(
                "[复读机] 复读发送未成功（可能被出站 Hook 拦截），本次复读未发出，"
                "不向 planner 注入提示"
            )

    async def _send_repeat(self, stream_id: str, kind: str, key: str, payload: Any) -> bool:
        """按内容种类选择发送通道；返回是否发送成功。

        - text: ctx.send.text 原样单条；
        - sticker: ctx.send.emoji 回发缓存中的同一张图（纯 base64），
          失败直接放弃（不改发文字）；
        - face: ctx.send.custom("face", id) 发送真实QQ表情（依赖 snowluma-adapter），
          失败直接放弃（不改发文字）。send_native 关闭时按文本复读属用户主动选择。
        """
        if kind == _KIND_STICKER:
            cached = self._sticker_cache.get(key)
            if not cached or not cached[1]:
                self.ctx.logger.warning("[复读机] 表情包图片数据已不在缓存中（过期或被挤出），放弃跟发")
                return False
            ok = bool(await self.ctx.send.emoji(cached[1], stream_id))
            if not ok:
                self.ctx.logger.warning("[复读机] send.emoji 未成功，本次不跟发")
            return ok

        if kind == _KIND_FACE:
            payload_data = payload if isinstance(payload, dict) else {}
            face_id = str(payload_data.get("id") or "")
            if not face_id:
                return False
            if self.config.qq_face.send_native:
                ok = bool(await self.ctx.send.custom("face", face_id, stream_id))
                if not ok:
                    self.ctx.logger.warning(
                        "[复读机] send.custom(face) 未成功（可能适配器不支持 face 段），本次不跟发"
                    )
                return ok
            # 用户主动关闭原生发送：按文本复读（非失败降级）
            fallback_text = str(payload_data.get("text") or f"[{key}]")
            return bool(await self.ctx.send.text(text=fallback_text, stream_id=stream_id))

        return bool(await self.ctx.send.text(text=str(payload or ""), stream_id=stream_id))

    # ── Hook 2: planner 防复读守卫（需求2） ────────────────────────────

    @HookHandler(
        "maisaka.planner.before_request",
        name="repeater_planner_guard",
        description="复读后仅向 planner 注入一次『已复读过』状态提示（一次性消费，不影响后续发言意愿）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def guard_planner(self, **kwargs: Any) -> dict[str, Any]:
        """planner 请求模型前注入状态提示（只注入一次）。

        设计依据（官方文档 + 主程序行为）：
        - planner 的默认动作是 no_reply，且连续不动作会触发 no_action_backoff
          （15s→300s 递增退避），持续注入抑制性提示会压低发言命中；
          因此只在复读后最近的一次 planner 请求里"告知事实"，注入即消费。

        注入目标随主程序版本而变，插件两种都支持：
        - MaiBot 1.2.5+ / SDK 2.8.x：kwargs 提供 `items`（Context Items），
          把提示追加到最后一条 SystemMessageItem 的 parts；没有 system item
          时在列表开头插入一条新的 SystemMessageItem；
        - 旧版：kwargs 提供 `extra_prompt`（str），追加写入。
        """
        base = {"action": "continue", "modified_kwargs": kwargs}
        if not (self.config.plugin.enabled and self.config.planner_guard.enabled):
            return base

        found = self._peek_pending_note(kwargs)
        if not found:
            return base
        stream_id, info = found

        kind = str(info.get("kind") or _KIND_TEXT)
        display = self._preview(info.get("text", ""))
        if kind == _KIND_STICKER:
            note = (
                f"{_PLANNER_NOTE_PREFIX}你刚才跟发了一张表情包「{display}」"
                "（这是插件自动参与群友复读，不是你主动想发的），"
                "知道这件事就行，不用再发表情包，按正常聊天继续。"
            )
        elif kind == _KIND_FACE:
            note = (
                f"{_PLANNER_NOTE_PREFIX}你刚才跟了一个QQ表情「{display}」"
                "（这是插件自动参与群友复读，不是你主动想发的），"
                "知道这件事就行，不用再发这个表情，按正常聊天继续。"
            )
        else:
            note = (
                f"{_PLANNER_NOTE_PREFIX}你刚才跟风复读过一次「{display}」"
                "（这是插件自动参与群友复读，不是你主动想说的），"
                "知道这件事就行，不用再重复这句话，按正常聊天继续。"
            )

        target = self._inject_note(kwargs, note)
        if target is None:
            self.ctx.logger.warning(
                "[复读机] planner 请求既无 items 也无 extra_prompt，提示暂不注入（保留待下次请求）"
            )
            return base

        # 注入成功才消费，确保"只注入一次"
        self._pending_note.pop(stream_id, None)
        self.ctx.logger.info(
            f"[复读机] 已向 planner 注入一次防复读提示（目标={target}, stream={stream_id}），已消费"
        )
        return {"action": "continue", "modified_kwargs": kwargs}

    def _inject_note(self, kwargs: dict[str, Any], note: str) -> str | None:
        """把提示写入 planner 请求参数；返回注入目标描述，无法注入时返回 None。"""
        items = kwargs.get("items")
        if isinstance(items, list):
            self._append_note_to_items(items, note)
            return "items/SystemMessageItem"

        if "extra_prompt" in kwargs:
            kwargs["extra_prompt"] = (kwargs.get("extra_prompt") or "") + note
            return "extra_prompt"
        return None

    @staticmethod
    def _append_note_to_items(items: list[Any], note: str) -> None:
        """把提示追加进 Context Items。

        主程序 1.2.5 的 planner hook 载荷是 Context Item 快照，每条形如
        {"item_type", "meta"{item_id, logical_turn_id, timestamp}, "parts"}。
        优先追加到已有 SystemMessageItem 的 parts（不改消息顺序）；
        没有 system item 时插入一条新的——item_id 必须唯一、timestamp 必须是
        ISO 时间，否则主程序反序列化会抛错。
        """
        for item in reversed(items):
            if isinstance(item, dict) and item.get("item_type") == _ITEM_SYSTEM:
                parts = item.get("parts")
                if not isinstance(parts, list):
                    parts = []
                    item["parts"] = parts
                parts.append({"type": "text", "text": note})
                return

        items.insert(
            0,
            {
                "item_type": _ITEM_SYSTEM,
                "meta": {
                    "item_id": f"mai-repeater-guard-{uuid.uuid4().hex}",
                    "logical_turn_id": None,
                    "timestamp": datetime.now().astimezone().isoformat(),
                },
                "parts": [{"type": "text", "text": note}],
            },
        )

    def _peek_pending_note(self, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        """查找待注入提示（只读不消费，注入成功后再由调用方 pop）。

        - 先丢弃超过 pending_ttl 的待注入提示（复读潮已过，注入陈旧信息无意义）；
        - 会话匹配：优先 kwargs 顶层的 session_id（MaiBot 1.2.5 直接提供），
          再回退旧版的 reply_tool_args / reference_info / tool_args；
        - 无法识别会话时退化为最近一条待注入提示（单活跃群场景下等价）。

        Returns:
            (stream_id, info) 元组；没有可注入的提示时返回 None。
        """
        ttl = max(0.0, float(self.config.planner_guard.pending_ttl))
        now = time.time()

        if ttl > 0:
            for sid in [
                s
                for s, inf in self._pending_note.items()
                if now - float(inf.get("ts", 0.0)) > ttl
            ]:
                self._pending_note.pop(sid, None)
                self._vlog(f"[复读机] 待注入提示已过期作废（stream={sid}）")

        if not self._pending_note:
            return None

        candidates = self._stream_candidates(kwargs)
        stream_id: str | None = None
        if candidates:
            for sid in candidates:
                if sid in self._pending_note:
                    stream_id = sid
                    break
        else:
            self._vlog("[复读机] planner kwargs 未含会话标识，取最近一条待注入提示")
            stream_id = max(
                self._pending_note,
                key=lambda sid: float(self._pending_note[sid].get("ts", 0.0)),
            )

        if stream_id is None:
            return None

        info = dict(self._pending_note[stream_id])
        info["stream_id"] = stream_id
        return stream_id, info

    def _stream_candidates(self, kwargs: dict[str, Any]) -> list[str]:
        """从 planner hook kwargs 中提取可能的会话标识。"""
        candidates: list[str] = []
        sources: list[dict[str, Any]] = [kwargs]
        for key in ("reply_tool_args", "reference_info", "tool_args"):
            val = kwargs.get(key)
            if isinstance(val, dict):
                sources.append(val)
        for source in sources:
            for key in ("stream_id", "chat_id", "session_id", "chat_stream_id"):
                val = source.get(key)
                if val:
                    candidates.append(str(val))
        return candidates

    # ── Command: 状态自查 ──────────────────────────────────────────────

    @Command(
        name="/rp_stat",
        pattern=r"^/rp_stat\b",
        description="查看复读插件当前状态（窗口/记忆/守卫/配置）",
    )
    async def cmd_stat(self, **kwargs: Any) -> tuple[bool, str, int]:
        """发送当前状态摘要到当前会话。"""
        stream_id = str(kwargs.get("stream_id") or "")
        now = time.time()
        c = self.config

        lines = ["[复读机状态]"]
        lines.append(
            f"启用: {c.plugin.enabled} | 阈值: {c.repeat.min_count}条/{c.repeat.min_senders}人 | "
            f"窗口: {c.repeat.window_seconds}s | 冷却: {c.repeat.cooldown_seconds}s"
        )
        win = self._window.get(stream_id)
        lines.append(f"本会话窗口: {len(win) if win else 0} 条消息")
        memory = self._repeated.get(stream_id) or {}
        lines.append(f"本会话已复读记忆: {len(memory)} 条")
        info = self._last_repeat.get(stream_id)
        if info:
            ago = max(0, int(now - float(info.get("ts", 0.0))))
            kind_label = _KIND_LABELS.get(str(info.get("kind") or _KIND_TEXT), "文字")
            lines.append(
                f"最近复读（{kind_label}）: {ago}s 前「{self._preview(str(info.get('text', '')))}」"
                f"（已发出: {'是' if info.get('sent') else '否'}）"
            )
        else:
            lines.append("最近复读: 无记录")

        if not c.planner_guard.enabled:
            lines.append("planner 守卫: 已关闭")
        else:
            pending = self._pending_note.get(stream_id)
            if pending:
                left = int(max(0.0, float(c.planner_guard.pending_ttl) - (now - float(pending.get("ts", 0.0)))))
                lines.append(f"planner 守卫: 待注入 1 次（剩余有效期 {left}s）")
            else:
                lines.append("planner 守卫: 无待注入提示（已注入或无需注入）")
        cooldown_left = float(self._cooldown_until.get(stream_id, 0.0)) - now
        if cooldown_left > 0:
            lines.append(f"复读冷却: 剩余 {int(cooldown_left)}s")

        report = "\n".join(lines)
        try:
            await self.ctx.send.text(text=report, stream_id=stream_id)
            return True, "复读机状态已发送", 2
        except Exception as exc:
            self.ctx.logger.error(f"[复读机] 状态发送异常: {exc}")
            return False, f"状态发送失败: {exc}", 2

    # ── 工具方法 ───────────────────────────────────────────────────────

    def _normalize(self, text: str) -> str:
        """归一化文本用于相同性判定（复读时仍发送原文）。"""
        t = (text or "").strip()
        if not t:
            return ""
        if self.config.repeat.normalize_whitespace:
            t = re.sub(r"\s+", " ", t)
        if self.config.repeat.case_insensitive:
            t = t.casefold()
        return t

    @staticmethod
    def _kind_preview(kind: str, key: str) -> str:
        """按内容种类生成日志/提示用的简短预览。"""
        if kind == _KIND_STICKER:
            return f"[表情包:{key[:8]}…]" if len(key) > 8 else "[表情包]"
        if kind == _KIND_FACE:
            return f"[{key}]"
        return key

    @staticmethod
    def _kind_display(kind: str, key: str, payload: Any) -> str:
        """按内容种类生成展示文本（rp_stat 与 planner 提示用）。"""
        if isinstance(payload, dict):
            text_val = payload.get("text")
            if isinstance(text_val, str) and text_val:
                return text_val
        if isinstance(payload, str) and payload:
            return payload
        return RepeaterPlugin._kind_preview(kind, key)

    def _extract_fields(self, msg: dict[str, Any]) -> tuple[str, str, str]:
        """从序列化 SessionMessage 提取 (stream_id, user_id, text)。

        字段名做多候选防御（陷阱4：kwargs 为嵌套结构）：
        - user_id: message_info.user_info.user_id
        - stream_id: message_info / 顶层的 stream_id|chat_id|session_id
        - text: 优先 processed_plain_text，否则从 raw_message 组件拼接
        """
        msg_info = msg.get("message_info")
        if not isinstance(msg_info, dict):
            msg_info = {}
        user_info = msg_info.get("user_info")
        if not isinstance(user_info, dict):
            user_info = {}

        user_id = str(user_info.get("user_id") or msg.get("user_id") or "")

        stream_id = ""
        for source in (msg_info, msg):
            for key in ("stream_id", "chat_id", "session_id"):
                val = source.get(key)
                if val:
                    stream_id = str(val)
                    break
            if stream_id:
                break

        text = str(msg.get("processed_plain_text") or "")
        if not text:
            text = self._text_from_components(msg.get("raw_message"))
        return stream_id, user_id, text

    def _text_from_components(self, raw_message: Any) -> str:
        """从 raw_message（段列表或 MessageSequence 序列化）拼接文本。"""
        components: Any = None
        if isinstance(raw_message, dict):
            components = raw_message.get("components")
        elif isinstance(raw_message, list):
            components = raw_message
        if not isinstance(components, list):
            return ""

        parts: list[str] = []
        for comp in components:
            if not isinstance(comp, dict):
                continue
            text_val = comp.get("text") or comp.get("data")
            if isinstance(text_val, str) and text_val.strip():
                parts.append(text_val)
            elif isinstance(text_val, dict):
                inner = text_val.get("data")
                if isinstance(inner, str) and inner.strip():
                    parts.append(inner)
        return "".join(parts)

    def _maybe_dump_structure(self, msg: dict[str, Any]) -> None:
        """调试：首条消息输出完整序列化结构（仅一次）。"""
        if not self.config.debug.dump_message_structure or self._structure_dumped:
            return
        self._structure_dumped = True
        try:
            dumped = json.dumps(msg, ensure_ascii=False, default=str)
        except Exception as exc:
            dumped = f"<序列化失败: {exc}>"
        self.ctx.logger.info(f"[复读机][结构dump] 首条入站消息完整结构: {dumped}")

    def _vlog(self, message: str) -> None:
        """冗余调试日志（verbose_log 开启时输出）。"""
        if self.config.debug.verbose_log:
            self.ctx.logger.info(message)

    @staticmethod
    def _preview(text: Any, limit: int = _MAX_NOTE_TEXT) -> str:
        """压缩文本用于日志/提示展示。"""
        t = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(t) <= limit:
            return t
        return t[:limit] + "…"


def create_plugin() -> MaiBotPlugin:
    """MaiBot 插件工厂函数。"""
    return RepeaterPlugin()
