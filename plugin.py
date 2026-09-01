"""麦麦的本质就是复读机（Mai_repeater）插件入口。

功能：
- 复读检测：`chat.receive.after_process` OBSERVE 只读旁路监听入站消息，
  每个会话维护滑动窗口；窗口内同一文本（归一化后）达到 min_count 条
  且来自 min_senders 个不同发送人，即判定群友在复读。
- 跟风复读：通过 `ctx.send.text` 直发通道发送相同文字（原样、单条），
  不经过 planner/replyer/表达方式分段器——长文本不会被拆分或改写，
  从根本上规避"自动分词导致复读失败"的问题。
- 一次复读记忆（需求1）：同一句话在记忆 TTL（默认 5 分钟）内只复读一次，
  之后无论同一段文本再来几波，都不再跟。
- planner 防复读守卫（需求2）：订阅 `maisaka.planner.before_request`，
  复读后只向 extra_prompt **追加一次**状态提示即消费掉，让 planner 知道
  "我刚复读过"，又不持续干扰其发言决策——planner 默认动作是 no_reply，
  持续注入抑制性提示会触发 no_action backoff，压低整体发言命中。

边界（不越权）：
- 消息监听为 OBSERVE 只读旁路，不拦截、不修改任何入站消息。
- 发送仅使用官方 ctx.send.text 能力（manifest 只声明 send.text）。
- planner 提示使用官方 extra_prompt 追加语义（陷阱8：追加不覆盖）。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections import deque
from typing import Any, ClassVar, Iterable

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .config import RepeaterSettings

_PLANNER_NOTE_PREFIX = "\n[复读插件状态] "
_MAX_NOTE_TEXT = 24


class RepeaterPlugin(MaiBotPlugin):
    """麦麦复读机主类。

    状态（均为内存态，插件重载后自然清零）：
    - _window:          stream_id -> deque[(ts, user_id, norm_text, raw_text)]
    - _repeated:        stream_id -> {norm_text: 首次复读时间}（一次复读记忆）
    - _last_repeat:     stream_id -> 最近一次复读信息（仅用于状态展示）
    - _pending_note:    stream_id -> 待注入 planner 的提示（注入一次后即消费）
    - _cooldown_until:  stream_id -> 冷却截止时间戳
    """

    config_model = RepeaterSettings

    # 仅订阅自身配置热更新；不订阅主程序 bot/model scope
    config_reload_subscriptions: ClassVar[Iterable[str]] = ()

    def __init__(self) -> None:
        super().__init__()
        self._window: dict[str, deque] = {}
        self._repeated: dict[str, dict[str, float]] = {}
        self._last_repeat: dict[str, dict[str, Any]] = {}
        self._pending_note: dict[str, dict[str, Any]] = {}
        self._cooldown_until: dict[str, float] = {}
        self._structure_dumped = False

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def on_load(self) -> None:
        """初始化插件：打印生效配置摘要。"""
        c = self.config
        self.ctx.logger.info(
            f"[复读机] 已加载: 阈值={c.repeat.min_count}条/{c.repeat.min_senders}人, "
            f"窗口={c.repeat.window_seconds}s, 冷却={c.repeat.cooldown_seconds}s, "
            f"一次记忆={c.once.repeated_memory_ttl}s, "
            f"planner守卫={'开' if c.planner_guard.enabled else '关'}"
            f"（只注入一次, 提示有效期{c.planner_guard.pending_ttl}s）, "
            f"延迟={c.repeat.delay_min}~{c.repeat.delay_max}s"
        )

    async def on_unload(self) -> None:
        """卸载插件。"""
        self.ctx.logger.info("[复读机] 已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, Any], version: str
    ) -> None:
        """配置热更新（scope="self"）。"""
        del config_data
        if scope == "self":
            c = self.config
            self.ctx.logger.info(
                f"[复读机] 配置已热更新: 阈值={c.repeat.min_count}条/{c.repeat.min_senders}人, "
                f"planner守卫={'开' if c.planner_guard.enabled else '关'}"
            )
        return None

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
        if not text:
            return None

        stripped = text.strip()
        if not stripped or stripped.startswith("/"):
            return None
        max_len = int(self.config.repeat.max_text_length)
        if max_len > 0 and len(stripped) > max_len:
            self._vlog(f"[复读机] 文本超长({len(stripped)}>{max_len})，不参与: {stream_id}")
            return None

        ignored = {str(u) for u in self.config.filter.ignored_user_ids if str(u).strip()}
        bots = {str(u) for u in self.config.filter.bot_user_ids if str(u).strip()}
        if user_id in ignored or user_id in bots:
            self._vlog(f"[复读机] 发送人 {user_id} 被过滤，不参与判定")
            return None

        self._handle_inbound(stream_id, user_id, text)
        return None

    def _handle_inbound(self, stream_id: str, user_id: str, text: str) -> None:
        """记录消息并判定是否触发复读；命中则安排发送任务。"""
        trigger_text = self._record_and_check(stream_id, user_id, text)
        if trigger_text is None:
            return
        # 触发即记账（一次记忆 + 冷却），后续同文本消息不会重复触发
        self._mark_repeated(stream_id, trigger_text)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._do_repeat(stream_id, trigger_text))
        except RuntimeError:
            self.ctx.logger.warning("[复读机] 无运行中的事件循环，放弃本次复读")

    def _record_and_check(self, stream_id: str, user_id: str, text: str) -> str | None:
        """更新滑动窗口；满足条件返回待复读原文，否则返回 None。"""
        cfg = self.config.repeat
        now = time.time()
        win = self._window.setdefault(stream_id, deque(maxlen=64))
        while win and now - win[0][0] > max(1.0, float(cfg.window_seconds)):
            win.popleft()

        norm = self._normalize(text)
        if not norm:
            return None
        win.append((now, user_id, norm, text))

        matches = [e for e in win if e[2] == norm]
        senders = {e[1] for e in matches}
        min_count = max(2, int(cfg.min_count))
        min_senders = max(1, int(cfg.min_senders))
        self._vlog(
            f"[复读机] {stream_id} 文本「{self._preview(text)}」窗口内 "
            f"{len(matches)}条/{len(senders)}人 (需 {min_count}条/{min_senders}人)"
        )
        if len(matches) < min_count or len(senders) < min_senders:
            return None

        # 需求1：同一句话只复读一次（记忆期内）
        memory = self._repeated.setdefault(stream_id, {})
        ttl = float(self.config.once.repeated_memory_ttl)
        if norm in memory and (ttl <= 0 or now - memory[norm] < ttl):
            self._vlog(f"[复读机] 「{self._preview(text)}」已复读过，本次不跟")
            return None
        if ttl > 0:
            expired = [k for k, t in memory.items() if now - t >= ttl]
            for k in expired:
                memory.pop(k, None)

        # 冷却期内不跟新的复读潮
        if now < self._cooldown_until.get(stream_id, 0.0):
            self._vlog(f"[复读机] {stream_id} 复读冷却中，不跟「{self._preview(text)}」")
            return None

        return text

    def _mark_repeated(self, stream_id: str, raw_text: str) -> None:
        """记账：一次复读记忆 + 待注入 planner 提示（一次性）+ 冷却。"""
        now = time.time()
        norm = self._normalize(raw_text)
        self._repeated.setdefault(stream_id, {})[norm] = now
        self._last_repeat[stream_id] = {
            "text": raw_text,
            "norm": norm,
            "ts": now,
            "sent": False,
        }
        self._cooldown_until[stream_id] = now + max(0.0, float(self.config.repeat.cooldown_seconds))
        self.ctx.logger.info(
            f"[复读机] 判定复读潮成立，准备跟读「{self._preview(raw_text)}」| stream={stream_id}"
        )

    async def _do_repeat(self, stream_id: str, text: str) -> None:
        """直发复读内容（原样单条，不经过 AI 链路）。"""
        delay_min = max(0.0, float(self.config.repeat.delay_min))
        delay_max = max(delay_min, float(self.config.repeat.delay_max))
        delay = random.uniform(delay_min, delay_max) if delay_max > 0 else 0.0
        if delay > 0:
            await asyncio.sleep(delay)

        info = self._last_repeat.get(stream_id) or {}
        try:
            ok = await self.ctx.send.text(text=text, stream_id=stream_id)
        except Exception as exc:
            self.ctx.logger.error(f"[复读机] 复读发送异常: {exc}")
            return

        if ok:
            if isinstance(info, dict):
                info["sent"] = True
            # 复读确实发出后才挂待注入提示：下一次 planner 请求消费一次即消失
            self._pending_note[stream_id] = {"text": text, "ts": time.time()}
            self.ctx.logger.info(f"[复读机] 复读已发送: {self._preview(text)} | stream={stream_id}")
        else:
            self.ctx.logger.warning(
                "[复读机] send.text 返回 False（可能被 output_blocker 等出站 Hook 拦截），本次复读未发出，"
                "不向 planner 注入提示"
            )

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
        - planner 的默认动作是 no_reply，且 HeartFChatting 有 no_action_backoff
          （连续不动作就递增退避 15s→300s），持续注入抑制性提示会压低发言命中。
        - 因此这里只在复读后最近的这一次 planner 请求里"告知事实"，
          注入后立即消费掉，后续 planner 决策完全不受影响。
        - extra_prompt 为 str，按追加语义注入（陷阱8：追加不覆盖其他插件内容）。
        """
        base = {"action": "continue", "modified_kwargs": kwargs}
        if not (self.config.plugin.enabled and self.config.planner_guard.enabled):
            return base

        info = self._take_pending_note(kwargs)
        if not info:
            return base

        note = (
            f"{_PLANNER_NOTE_PREFIX}你刚才跟风复读过一次「{self._preview(info.get('text', ''))}」"
            "（这是插件自动参与群友复读，不是你主动想说的），"
            "知道这件事就行，不用再重复这句话，按正常聊天继续。"
        )
        kwargs["extra_prompt"] = (kwargs.get("extra_prompt") or "") + note
        self.ctx.logger.info(
            f"[复读机] 已向 planner 注入一次防复读提示（stream={info.get('stream_id', '')}），已消费"
        )
        return {"action": "continue", "modified_kwargs": kwargs}

    def _take_pending_note(self, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        """取出待注入提示（取出即消费，保证只注入一次）。

        - 先丢弃超过 pending_ttl 的待注入提示（复读潮已过，注入陈旧信息无意义）；
        - 会话匹配：依次尝试 kwargs 顶层与 reply_tool_args/reference_info 中的
          会话键（stream_id/chat_id/session_id/chat_stream_id）；
        - 无法识别会话时退化为最近一条待注入提示（单活跃群场景下等价）。
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

        info = dict(self._pending_note.pop(stream_id))
        info["stream_id"] = stream_id
        return info

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
        lines.append(f"本会话已复读记忆: {len(memory)} 句")
        info = self._last_repeat.get(stream_id)
        if info:
            ago = max(0, int(now - float(info.get("ts", 0.0))))
            lines.append(
                f"最近复读: {ago}s 前「{self._preview(str(info.get('text', '')))}」"
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
