"""麦麦复读机自测脚本（mock ctx，不依赖真实 Host）。

覆盖：
- 复读判定（条数/人数/窗口/冷却/一次记忆）
- 直发内容一致性（发送原文而非归一化文本）
- planner 守卫注入（追加语义、TTL、会话匹配）
- 过滤规则（命令、超长、忽略名单）
- /rp_stat 命令与工厂函数
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import re
import sys
import time
import traceback
from typing import Any

# 留空（MAIBOT_SDK_PATH=""）可让测试跑在当前解释器的 site-packages 版本上，
# 用于验证部署环境自带的 SDK（如 MaiBotOneKeyDesktop 的 2.8.0）是否兼容
SDK_PATH = os.environ.get("MAIBOT_SDK_PATH", r"D:\MaiBot\plugin\maibot-plugin-sdk-2.7.0")
PLUGIN_ROOT = r"D:\workdoc\plugin"

for p in (SDK_PATH, PLUGIN_ROOT):
    if p and p not in sys.path:
        sys.path.insert(0, p)


class FakeLogger:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    def log(self, level: int, msg: str) -> None:
        self.messages.append((level, msg))

    def debug(self, msg: str) -> None:
        self.log(logging.DEBUG, msg)

    def info(self, msg: str) -> None:
        self.log(logging.INFO, msg)

    def warning(self, msg: str) -> None:
        self.log(logging.WARNING, msg)

    def error(self, msg: str) -> None:
        self.log(logging.ERROR, msg)


class FakeSend:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.sent: list[dict[str, Any]] = []

    async def text(self, text: str, stream_id: str, **kwargs: Any) -> Any:
        self.sent.append({"text": text, "stream_id": stream_id})
        return self.result


class FakeCtx:
    def __init__(self, send: FakeSend | None = None) -> None:
        self.logger = FakeLogger()
        self.send = send or FakeSend()


def make_plugin(overrides: dict[str, Any] | None = None) -> tuple[Any, FakeCtx]:
    from repeater.config import RepeaterSettings
    from repeater.plugin import RepeaterPlugin

    cfg_data: dict[str, Any] = {
        "plugin": {"enabled": True},
        "repeat": {
            "min_count": 3,
            "min_senders": 3,
            "window_seconds": 120.0,
            "max_text_length": 300,
            "cooldown_seconds": 90.0,
            "delay_min": 0.0,
            "delay_max": 0.0,
            "case_insensitive": False,
            "normalize_whitespace": True,
        },
        "once": {"repeated_memory_ttl": 300.0},
        "planner_guard": {"enabled": True, "pending_ttl": 180.0},
        "filter": {"ignored_user_ids": [], "bot_user_ids": []},
        "debug": {"dump_message_structure": False, "verbose_log": False},
    }
    if overrides:
        for section, values in overrides.items():
            cfg_data.setdefault(section, {}).update(values)

    plugin = RepeaterPlugin()
    ctx = FakeCtx()
    plugin._set_context(ctx)
    plugin._plugin_config_instance = RepeaterSettings(**cfg_data)
    return plugin, ctx


def make_msg(stream: str, uid: str, text: str) -> dict[str, Any]:
    return {
        "message": {
            "message_info": {"user_info": {"user_id": uid}, "chat_id": stream},
            "processed_plain_text": text,
        }
    }


async def feed(plugin: Any, stream: str, uid: str, text: str) -> None:
    await plugin.on_message(**make_msg(stream, uid, text))
    await asyncio.sleep(0.02)


def check(cond: bool, label: str) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise AssertionError(label)


async def case_trigger_and_content() -> None:
    p, ctx = make_plugin()
    for uid in ("u1", "u2", "u3"):
        await feed(p, "s1", uid, "哈哈哈哈哈")
    check(len(ctx.send.sent) == 1, "3人3条 → 触发且仅触发一次")
    check(ctx.send.sent[0]["text"] == "哈哈哈哈哈", "复读内容与原文完全一致")
    check(ctx.send.sent[0]["stream_id"] == "s1", "发送到正确会话")

    # 第4/5条同文本：一次记忆，不再触发（需求1）
    await feed(p, "s1", "u4", "哈哈哈哈哈")
    await feed(p, "s1", "u5", "哈哈哈哈哈")
    check(len(ctx.send.sent) == 1, "同一句话后续复读潮不再跟（一次记忆）")


async def case_not_enough() -> None:
    p, ctx = make_plugin()
    await feed(p, "s2", "u1", "想要")
    await feed(p, "s2", "u2", "想要")
    check(len(ctx.send.sent) == 0, "只有2条 → 不触发")

    p, ctx = make_plugin()
    await feed(p, "s3", "u1", "想要")
    await feed(p, "s3", "u1", "想要")
    await feed(p, "s3", "u1", "想要")
    check(len(ctx.send.sent) == 0, "3条但同一人 → 不触发（人数不足）")


async def case_memory_ttl_expiry() -> None:
    p, ctx = make_plugin({"once": {"repeated_memory_ttl": 50.0}})
    for uid in ("u1", "u2", "u3"):
        await feed(p, "s4", uid, "冲！")
    check(len(ctx.send.sent) == 1, "TTL 内第一波触发")
    # 伪造记忆过期
    norm = p._normalize("冲！")
    p._repeated["s4"][norm] = time.time() - 100.0
    p._cooldown_until["s4"] = 0.0
    for uid in ("u1", "u2", "u3"):
        await feed(p, "s4", uid, "冲！")
    check(len(ctx.send.sent) == 2, "记忆过期后新的一波可再次触发")


async def case_cooldown_blocks_other_text() -> None:
    p, ctx = make_plugin()
    for uid in ("u1", "u2", "u3"):
        await feed(p, "s5", uid, "第一句")
    check(len(ctx.send.sent) == 1, "第一波触发")
    for uid in ("u1", "u2", "u3"):
        await feed(p, "s5", uid, "第二句")
    check(len(ctx.send.sent) == 1, "冷却期内新文本凑齐阈值 → 不触发")
    p._cooldown_until["s5"] = 0.0
    for uid in ("u1", "u2", "u3"):
        await feed(p, "s5", uid, "第二句")
    check(len(ctx.send.sent) == 2, "冷却过后新文本可触发")


async def case_filters() -> None:
    # 命令消息不参与
    p, ctx = make_plugin()
    for uid in ("u1", "u2", "u3"):
        await feed(p, "s6", uid, "/help")
    check(len(ctx.send.sent) == 0, "命令消息（/开头）不参与复读")

    # 超长文本不参与
    p, ctx = make_plugin()
    long_text = "超" * 301
    for uid in ("u1", "u2", "u3"):
        await feed(p, "s7", uid, long_text)
    check(len(ctx.send.sent) == 0, "超长文本不参与复读")

    # 忽略名单不参与
    p, ctx = make_plugin({"filter": {"ignored_user_ids": ["bot1"]}})
    await feed(p, "s8", "bot1", "别学我")
    await feed(p, "s8", "u2", "别学我")
    await feed(p, "s8", "u3", "别学我")
    check(len(ctx.send.sent) == 0, "忽略名单内的发送人不计数不触发")


async def case_normalize() -> None:
    p, ctx = make_plugin()
    await feed(p, "s9", "u1", "哈哈  哈哈")
    await feed(p, "s9", "u2", "哈哈 哈哈 ")
    await feed(p, "s9", "u3", " 哈哈 哈哈")
    check(len(ctx.send.sent) == 1, "空白归一化后视为同一句话")
    check(ctx.send.sent and ctx.send.sent[0]["text"] == " 哈哈 哈哈", "复读发送最后一条的原文（非归一化文本）")


async def case_planner_guard_once() -> None:
    """守卫语义：复读后只注入一次，注入即消费，不持续干扰 planner。"""
    p, ctx = make_plugin()
    base_prompt = "已有的人格提示"
    kwargs: dict[str, Any] = {
        "task_name": "planner",
        "extra_prompt": base_prompt,
        "reply_tool_args": {"chat_id": "s10"},
    }
    result = await p.guard_planner(**kwargs)
    check(result["action"] == "continue", "无待注入提示时放行")
    check(result["modified_kwargs"]["extra_prompt"] == base_prompt, "无待注入提示时不注入")

    # 触发一次复读 → 产生一条待注入提示
    for uid in ("u1", "u2", "u3"):
        await feed(p, "s10", uid, "一起来复读")
    check(len(ctx.send.sent) == 1, "复读已触发")
    check("s10" in p._pending_note, "复读后生成待注入提示")

    # 第一次 planner 请求 → 注入
    kwargs2: dict[str, Any] = {
        "task_name": "planner",
        "extra_prompt": base_prompt,
        "reply_tool_args": {"chat_id": "s10"},
    }
    result2 = await p.guard_planner(**kwargs2)
    injected = result2["modified_kwargs"]["extra_prompt"]
    check(injected.startswith(base_prompt), "注入为追加语义，不覆盖原有 extra_prompt")
    check("刚才跟风复读过一次" in injected, "提示内容告知 planner 已复读过（需求2）")
    check("一起来复读" in injected, "提示包含复读原文预览")
    check("按正常聊天继续" in injected, "提示措辞不压制后续发言意愿")
    check("s10" not in p._pending_note, "注入后立即消费（pending 已清空）")

    # 后续 planner 请求 → 不再注入（关键：不持续注入）
    kwargs3: dict[str, Any] = {
        "task_name": "planner",
        "extra_prompt": base_prompt,
        "reply_tool_args": {"chat_id": "s10"},
    }
    result3 = await p.guard_planner(**kwargs3)
    check(result3["modified_kwargs"]["extra_prompt"] == base_prompt, "第二次 planner 请求不再注入（只注入一次）")


async def case_planner_guard_edge() -> None:
    """守卫边界：会话隔离、pending 过期、无会话标识兜底。"""
    p, ctx = make_plugin({"planner_guard": {"pending_ttl": 60.0}})
    for uid in ("u1", "u2", "u3"):
        await feed(p, "s11", uid, "边界测试")
    check("s11" in p._pending_note, "复读后存在待注入提示")

    # 其他会话的 planner 请求 → 不注入，也不消费 s11 的 pending
    result = await p.guard_planner(extra_prompt="x", reply_tool_args={"chat_id": "s_other"})
    check(result["modified_kwargs"]["extra_prompt"] == "x", "其他会话不受影响")
    check("s11" in p._pending_note, "其他会话的请求不会消费本会话的待注入提示")

    # pending 过期 → 作废不注入
    p._pending_note["s11"]["ts"] = time.time() - 120.0
    result2 = await p.guard_planner(extra_prompt="y", reply_tool_args={"chat_id": "s11"})
    check(result2["modified_kwargs"]["extra_prompt"] == "y", "待注入提示过期后不再注入")
    check("s11" not in p._pending_note, "过期提示已清理")

    # 无会话标识 → 兜底注入一次
    for uid in ("u1", "u2", "u3"):
        await feed(p, "s12", uid, "兜底测试")
    p._cooldown_until["s12"] = 0.0
    result3 = await p.guard_planner(extra_prompt="z", task_name="planner")
    check("刚才跟风复读过一次" in result3["modified_kwargs"]["extra_prompt"], "kwargs 无会话标识时用最近记录兜底注入一次")
    check(not p._pending_note, "兜底注入后所有 pending 已消费")


async def case_disabled() -> None:
    p, ctx = make_plugin({"plugin": {"enabled": False}})
    for uid in ("u1", "u2", "u3"):
        await feed(p, "s11", uid, "禁用测试")
    check(len(ctx.send.sent) == 0, "插件禁用时不检测不复读")
    result = await p.guard_planner(extra_prompt="keep")
    check(result["modified_kwargs"]["extra_prompt"] == "keep", "插件禁用时守卫不注入")


async def case_lifecycle() -> None:
    """生命周期回调必须能真实跑通（曾漏测导致 on_load 里的字段改名残留未被发现）。"""
    p, ctx = make_plugin()
    await p.on_load()
    await p.on_unload()
    await p.on_config_update(scope="self", config_data={}, version="1.0.0")
    logs = " ".join(m for _, m in ctx.logger.messages)
    check("已加载" in logs, "on_load 正常执行并输出配置摘要")
    check("只注入一次" in logs, "on_load 摘要含 planner 守卫信息（字段名与配置模型一致）")
    check("已卸载" in logs, "on_unload 正常执行")
    check("配置已热更新" in logs, "on_config_update(self) 正常执行")


async def case_config_field_consistency() -> None:
    """静态校验：plugin.py 引用的配置字段必须在配置模型中存在（防字段改名残留）。"""
    import repeater
    from repeater.config import RepeaterSettings

    src = (pathlib.Path(repeater.__file__).parent / "plugin.py").read_text(encoding="utf-8")
    sections = {
        name: set(info.annotation.model_fields.keys())
        for name, info in RepeaterSettings.model_fields.items()
        if hasattr(info.annotation, "model_fields")
    }
    used = set(re.findall(r"(?:self\.config|c)\.(\w+)\.(\w+)", src))
    missing = sorted(f"{sec}.{fld}" for sec, fld in used if sec in sections and fld not in sections[sec])
    check(not missing, f"plugin.py 引用的配置字段均存在（缺失: {missing}）")


async def case_rp_stat() -> None:
    p, ctx = make_plugin()
    ok, resp, intercept = await p.cmd_stat(text="/rp_stat", stream_id="s12")
    check(ok and intercept == 2, f"/rp_stat 返回三元组 (ok={ok}, intercept={intercept})")
    check(ctx.send.sent and "[复读机状态]" in ctx.send.sent[-1]["text"], "/rp_stat 输出状态摘要")
    check("最近复读: 无记录" in ctx.send.sent[-1]["text"], "无记录时输出占位信息")


async def main_async() -> None:
    await case_trigger_and_content()
    await case_not_enough()
    await case_memory_ttl_expiry()
    await case_cooldown_blocks_other_text()
    await case_filters()
    await case_normalize()
    await case_planner_guard_once()
    await case_planner_guard_edge()
    await case_disabled()
    await case_lifecycle()
    await case_config_field_consistency()
    await case_rp_stat()

    from repeater.plugin import RepeaterPlugin, create_plugin

    instance = create_plugin()
    check(isinstance(instance, RepeaterPlugin), "create_plugin() 工厂函数")
    check(RepeaterPlugin._preview("这是一条特别长的复读内容需要被截断展示出来哈哈", 10) == "这是一条特别长的复读…", "_preview 截断正确")

    print("\n全部用例通过 ✅")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    try:
        main()
    except AssertionError:
        traceback.print_exc()
        sys.exit(1)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
