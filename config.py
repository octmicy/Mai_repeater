"""麦麦的本质就是复读机（Mai_repeater）WebUI 配置模型。

配置项分组：
- plugin: 插件总开关
- repeat: 复读触发参数（阈值、窗口、冷却、延迟、归一化）
- once: 一次复读记忆（同一句话只复读一次）
- planner_guard: planner 防复读守卫（复读后向 planner 注入状态提示）
- filter: 发送人过滤（其他 bot、麦麦自己）
- debug: 调试选项（消息结构 dump、冗余日志）
"""

from __future__ import annotations

from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase

CONFIG_SCHEMA_VERSION = "1.0.0"


class PluginConfig(PluginConfigBase):
    """插件总开关。"""

    __ui_label__: ClassVar[str] = "插件设置"
    __ui_order__: ClassVar[int] = 0

    config_version: str = Field(
        default=CONFIG_SCHEMA_VERSION,
        description="配置 schema 版本，请勿手动修改。",
        json_schema_extra={
            "disabled": True,
            "hidden": True,
            "label": "配置版本",
            "order": 99,
        },
    )
    enabled: bool = Field(
        default=True,
        description="是否启用复读插件。关闭后不检测也不复读。",
        json_schema_extra={
            "label": "启用插件",
            "hint": "总开关，关闭后消息监听与 planner 守卫全部停用。",
        },
    )


class RepeatConfig(PluginConfigBase):
    """复读触发参数。"""

    __ui_label__: ClassVar[str] = "复读触发"
    __ui_order__: ClassVar[int] = 1

    min_count: int = Field(
        default=3,
        description="触发复读所需的相同消息条数（含触发那条）",
        json_schema_extra={
            "label": "复读条数阈值",
            "hint": "滑动窗口内同一文本达到该条数才判定为复读，默认 3。",
        },
    )
    min_senders: int = Field(
        default=3,
        description="触发复读所需的不同发送人数",
        json_schema_extra={
            "label": "不同发送人数",
            "hint": "相同消息必须来自至少 N 个不同的人才算群友复读，默认 3。",
        },
    )
    window_seconds: float = Field(
        default=120.0,
        description="滑动窗口时长（秒），窗口外的消息不参与判定",
        json_schema_extra={
            "label": "检测窗口（秒）",
            "hint": "复读判定的时间范围，默认 120 秒。",
        },
    )
    max_text_length: int = Field(
        default=300,
        description="超过该长度的消息不参与复读（防灌长文诱导）",
        json_schema_extra={
            "label": "最大复读长度",
            "hint": "0 表示不限制。发送本身不受影响（直发原样单条），仅限制复读触发。",
        },
    )
    cooldown_seconds: float = Field(
        default=90.0,
        description="同一群聊两次复读之间的最小间隔（秒）",
        json_schema_extra={
            "label": "复读冷却（秒）",
            "hint": "冷却期内即使有新的复读潮也不跟，防止连续刷屏。",
        },
    )
    delay_min: float = Field(
        default=0.5,
        description="复读前随机延迟下限（秒）",
        json_schema_extra={
            "label": "延迟下限（秒）",
            "hint": "让复读显得自然，设为 0 可立即发送。",
        },
    )
    delay_max: float = Field(
        default=2.0,
        description="复读前随机延迟上限（秒）",
        json_schema_extra={
            "label": "延迟上限（秒）",
            "hint": "实际延迟在上下限间随机取值。",
        },
    )
    case_insensitive: bool = Field(
        default=False,
        description="判定相同文本时是否忽略大小写",
        json_schema_extra={
            "label": "忽略大小写",
            "hint": "开启后 ABC 与 abc 视为同一句话。",
        },
    )
    normalize_whitespace: bool = Field(
        default=True,
        description="判定时合并连续空白并去首尾空白",
        json_schema_extra={
            "label": "空白归一化",
            "hint": "开启后「哈哈  哈哈」与「哈哈 哈哈」视为同一句话；复读时仍发送原文。",
        },
    )


class OnceConfig(PluginConfigBase):
    """一次复读记忆。"""

    __ui_label__: ClassVar[str] = "一次复读记忆"
    __ui_order__: ClassVar[int] = 2

    repeated_memory_ttl: float = Field(
        default=300.0,
        description="同一句话的已复读记忆时长（秒），0 表示本次运行内永久记忆",
        json_schema_extra={
            "label": "已复读记忆时长（秒）",
            "hint": "记忆期内同一句话绝不复读第二次（需求：上下文里同一句话只复读一次）。默认 5 分钟，只覆盖当次复读潮的上下文；设太长会导致群友过一阵再刷同一个梗时麦麦不跟，显得反应迟钝。",
        },
    )


class PlannerGuardConfig(PluginConfigBase):
    """planner 防复读守卫。"""

    __ui_label__: ClassVar[str] = "planner 守卫"
    __ui_order__: ClassVar[int] = 3

    enabled: bool = Field(
        default=True,
        description="复读后向 planner 注入一次状态提示，防止其自主决定再次复读",
        json_schema_extra={
            "label": "启用 planner 守卫",
            "hint": "通过 maisaka.planner.before_request 的 extra_prompt 追加（官方注入点，不覆盖其他插件内容）。只注入一次——planner 默认动作是 no_reply，持续注入抑制性提示会压低发言命中。",
        },
    )
    pending_ttl: float = Field(
        default=180.0,
        description="待注入提示的有效期（秒）。超过该时间仍未被 planner 请求消费则作废",
        json_schema_extra={
            "label": "提示有效期（秒）",
            "hint": "复读后只等最近一次 planner 决策：期间命中就注入一次并立即消费；超时说明该复读潮已结束，提示作废不再注入。默认 3 分钟。",
        },
    )


class FilterConfig(PluginConfigBase):
    """发送人过滤。"""

    __ui_label__: ClassVar[str] = "过滤设置"
    __ui_order__: ClassVar[int] = 4

    ignored_user_ids: list[str] = Field(
        default_factory=list,
        description="不参与复读判定的发送人 user_id 列表（如其他机器人）",
        json_schema_extra={
            "label": "忽略的发送人",
            "hint": "这些账号的消息既不计数也不触发复读，可填其他复读 bot 的 QQ 防止对撞。",
        },
    )
    bot_user_ids: list[str] = Field(
        default_factory=list,
        description="麦麦自己的 user_id 列表（防御性过滤，正常情况入站流不含自己）",
        json_schema_extra={
            "label": "机器人自身 ID",
            "hint": "若某些环境下入站消息包含机器人自己的消息，填入其 user_id 以排除。",
        },
    )
    exclude_non_text: bool = Field(
        default=True,
        description="图片/表情/语音/视频/文件等非文本消息不参与复读",
        json_schema_extra={
            "label": "排除非文本消息",
            "hint": "按消息组件类型判定。插件只能发文字，复读表情包会变成发一段占位文本，所以默认排除。",
        },
    )
    exclude_keywords: list[str] = Field(
        default_factory=lambda: ["表情包", "[图片]", "[动画表情]", "[语音]", "[视频]", "[文件]"],
        description="消息文本命中任一关键词时不参与复读",
        json_schema_extra={
            "label": "排除关键词",
            "hint": "MaiBot 会把表情包消息文本化成「[表情包：xxx]」这类占位，默认已排除。留空表示不按关键词排除。",
        },
    )


class DebugConfig(PluginConfigBase):
    """调试选项。"""

    __ui_label__: ClassVar[str] = "调试"
    __ui_order__: ClassVar[int] = 5

    dump_message_structure: bool = Field(
        default=False,
        description="首条入站消息时输出完整序列化结构，便于核对 stream_id/user_id 字段名",
        json_schema_extra={
            "label": "dump 消息结构",
            "hint": "首次部署建议开启，确认字段提取正确后关闭。仅输出一次。",
        },
    )
    verbose_log: bool = Field(
        default=False,
        description="输出窗口计数等冗余调试日志",
        json_schema_extra={
            "label": "冗余日志",
            "hint": "排查触发问题时开启。",
        },
    )


class RepeaterSettings(PluginConfigBase):
    """麦麦复读机完整配置。"""

    plugin: PluginConfig = Field(default_factory=PluginConfig)
    repeat: RepeatConfig = Field(default_factory=RepeatConfig)
    once: OnceConfig = Field(default_factory=OnceConfig)
    planner_guard: PlannerGuardConfig = Field(default_factory=PlannerGuardConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
