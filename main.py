"""
astrbot_plugin_emojis_tag

通过提示词 + 情绪标签发送表情包：

1. 每次 LLM 请求前向 system prompt 注入一段说明，告诉模型当前可用的情绪
   表情包（占位符 {emotions} 自动替换为表情包根目录下的情绪子文件夹名）。
2. 模型想发表情包时在回复里插入 ``<情绪名>`` 标签（如 ``<happy>``）。
3. 发送给用户时，插件过滤掉标签，并在标签所在位置发送该情绪文件夹中随机
   挑选的一张表情包图片。
4. 写入对话历史时，标签被转换成一段可自定义的文字（如
   ``[发送了表情包：happy-xxx.jpg]``），保证对话数据中不出现原始标签。

提示词注入支持两种模式：

- ``always``：每轮都注入提示词，由模型自己决定要不要发表情包。
- ``probability``：仅在 roll 命中时才注入提示词，未命中则该轮模型看不到
  表情包能力。
"""

from __future__ import annotations

import os
import random
import re
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

try:  # ProviderRequest / LLMResponse 仅用于类型标注，缺失时不影响运行
    from astrbot.core.provider.entities import LLMResponse, ProviderRequest
except Exception:  # pragma: no cover - 兼容旧版本
    LLMResponse = Any  # type: ignore
    ProviderRequest = Any  # type: ignore


@register(
    "astrbot_plugin_emojis_tag",
    "L1ke40oz",
    "通过提示词注入 + 情绪标签让 LLM 自主发送表情包。",
    "1.0.0",
)
class EmojisTagPlugin(Star):
    IMAGE_EXTENSIONS: tuple[str, ...] = (
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
    )

    # event extra 键名
    _EXTRA_PICKS = "_emojis_tag_picks"
    _EXTRA_ORIGINAL = "_emojis_tag_original_text"

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.config: dict[str, Any] = config or {}
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))

        # 情绪 -> 该情绪文件夹下的图片绝对路径列表
        self.emoji_map: dict[str, list[str]] = {}
        # 小写情绪名 -> 规范情绪名（用于大小写不敏感匹配标签）
        self._emotion_lookup: dict[str, str] = {}
        # 匹配已知情绪标签的正则（按当前 emoji_map 动态构建）
        self._open_tag_re: re.Pattern[str] | None = None
        self._close_tag_re: re.Pattern[str] | None = None

        self._load_runtime_config()

    # ----------------------------------------------------------- 生命周期

    async def initialize(self) -> None:
        self._load_runtime_config()
        self._scan_emojis()
        logger.info(
            f"[emojis_tag] 初始化完成: 注入模式={self.inject_mode}, "
            f"概率={self.inject_probability}, 情绪数={len(self.emoji_map)}, "
            f"情绪={list(self.emoji_map.keys())}"
        )

    async def terminate(self) -> None:
        logger.info("[emojis_tag] 插件已停止")

    # ----------------------------------------------------------------- 配置

    def _load_runtime_config(self) -> None:
        cfg = self.config

        raw_path = str(cfg.get("emojis_path", "") or "").strip()
        if not raw_path:
            self.emojis_path = os.path.join(self.plugin_dir, "emojis")
        elif os.path.isabs(raw_path):
            self.emojis_path = raw_path
        else:
            self.emojis_path = os.path.normpath(
                os.path.join(self.plugin_dir, raw_path)
            )

        inject_cfg = cfg.get("inject", {}) or {}
        self.inject_enable: bool = bool(inject_cfg.get("enable", True))
        mode = str(inject_cfg.get("mode", "always") or "always").strip().lower()
        self.inject_mode: str = mode if mode in ("always", "probability") else "always"
        self.inject_probability: float = max(
            0.0, min(1.0, float(inject_cfg.get("probability", 0.3) or 0.0))
        )
        self.prompt_template: str = str(
            inject_cfg.get("prompt_template", "") or ""
        ).strip() or self._default_prompt_template()

        history_cfg = cfg.get("history", {}) or {}
        # True: 把标签转写成文字记入对话历史; False: 直接从历史中剥离标签。
        self.history_record: bool = bool(history_cfg.get("record", True))
        self.history_template: str = str(
            history_cfg.get("template", "") or ""
        ).strip() or "[发送了表情包：{emotion}-{filename}]"

    @staticmethod
    def _default_prompt_template() -> str:
        return (
            "你可以在回复中插入情绪标签来给用户发送对应情绪的表情包。\n"
            "当前可用的情绪表情包有：{emotions}。\n"
            "当你想发某种情绪的表情包时，在回复文本中插入 <情绪名> 形式的标签即可，"
            "例如想发开心的表情包就写 <happy>。\n"
            "规则：\n"
            "1. 只能使用上面列出的情绪名，不要编造列表之外的情绪。\n"
            "2. 标签可以放在回复的任意位置，表情包会出现在标签所在的位置；"
            "不想发表情包时不要写任何标签。\n"
            "3. 标签本身不会展示给用户，用户只会看到对应的表情包图片，"
            "因此不要额外解释这个标签。"
        )

    # --------------------------------------------------------- 表情包扫描

    def _scan_emojis(self) -> None:
        emoji_map: dict[str, list[str]] = {}

        if not os.path.isdir(self.emojis_path):
            try:
                os.makedirs(self.emojis_path, exist_ok=True)
                logger.warning(
                    f"[emojis_tag] 表情包目录不存在，已创建: {self.emojis_path}"
                )
            except Exception as e:
                logger.error(
                    f"[emojis_tag] 无法创建表情包目录: {self.emojis_path} -> {e}"
                )
            self._set_emoji_map({})
            return

        for entry in sorted(os.listdir(self.emojis_path)):
            sub_dir = os.path.join(self.emojis_path, entry)
            if not os.path.isdir(sub_dir):
                continue
            files = [
                os.path.join(sub_dir, f)
                for f in sorted(os.listdir(sub_dir))
                if os.path.isfile(os.path.join(sub_dir, f))
                and f.lower().endswith(self.IMAGE_EXTENSIONS)
            ]
            if files:
                emoji_map[entry] = files

        self._set_emoji_map(emoji_map)

        if not emoji_map:
            logger.warning(f"[emojis_tag] 未在 {self.emojis_path} 下发现表情包。")

    def _set_emoji_map(self, emoji_map: dict[str, list[str]]) -> None:
        """更新 emoji_map 并重建标签匹配正则与查找表。"""
        self.emoji_map = emoji_map
        self._emotion_lookup = {name.lower(): name for name in emoji_map}

        if not emoji_map:
            self._open_tag_re = None
            self._close_tag_re = None
            return

        # 较长的情绪名优先匹配，避免前缀问题。
        names = sorted(emoji_map.keys(), key=len, reverse=True)
        alt = "|".join(re.escape(n) for n in names)
        self._open_tag_re = re.compile(rf"<\s*({alt})\s*>", re.IGNORECASE)
        self._close_tag_re = re.compile(rf"<\s*/\s*(?:{alt})\s*>", re.IGNORECASE)

    # --------------------------------------------------------------- 工具

    def _own_plugin_name(self) -> str:
        try:
            from astrbot.core.star.star import star_map

            meta = star_map.get(self.__class__.__module__)
            if meta and meta.name:
                return meta.name
        except Exception:
            pass
        return "astrbot_plugin_emojis_tag"

    async def _session_inactive(self, event: AstrMessageEvent) -> bool:
        """本会话是否通过「会话自定义规则」禁用了本插件。

        AstrBot 的会话级插件禁用只拦截指令 / 消息处理器，不拦截
        on_llm_request / on_decorating_result 等生命周期钩子，因此这里主动
        查询并据此跳过。API 不可用时返回 False（fail-open），与旧版本一致。
        """
        try:
            from astrbot.core.star.session_plugin_manager import (
                SessionPluginManager,
            )
        except Exception:
            return False
        try:
            enabled = await SessionPluginManager.is_plugin_enabled_for_session(
                event.unified_msg_origin, self._own_plugin_name()
            )
            return not enabled
        except Exception:
            return False

    def _resolve_emotion(self, raw_name: str) -> str | None:
        return self._emotion_lookup.get(raw_name.strip().lower())

    def _pick_file(self, emotion: str) -> str | None:
        files = self.emoji_map.get(emotion)
        if not files:
            return None
        return random.choice(files)

    def _pick_for_text(self, text: str) -> list[dict[str, str]]:
        """按出现顺序解析 text 中的情绪标签，为每个标签随机挑一张图片。

        返回 [{"emotion": 规范情绪名, "path": 绝对路径, "filename": 文件名}, ...]
        """
        if not text or self._open_tag_re is None:
            return []
        picks: list[dict[str, str]] = []
        for match in self._open_tag_re.finditer(text):
            emotion = self._resolve_emotion(match.group(1))
            if emotion is None:
                continue
            path = self._pick_file(emotion)
            if not path:
                continue
            picks.append(
                {
                    "emotion": emotion,
                    "path": path,
                    "filename": os.path.basename(path),
                }
            )
        return picks

    def _build_history_text(self, text: str, picks: list[dict[str, str]]) -> str:
        """把 text 中的情绪标签替换为历史摘要文字（或直接剥离）。"""
        if self._open_tag_re is None:
            return text

        index = 0

        def _repl(match: re.Match[str]) -> str:
            nonlocal index
            emotion = self._resolve_emotion(match.group(1))
            if emotion is None:
                return match.group(0)
            pick = picks[index] if index < len(picks) else None
            index += 1
            if not self.history_record or pick is None:
                return ""
            try:
                return self.history_template.format(
                    emotion=pick["emotion"], filename=pick["filename"]
                )
            except (KeyError, IndexError, ValueError):
                return self.history_template

        result = self._open_tag_re.sub(_repl, text)
        # 去掉可能残留的闭合标签（如 </happy>）。
        if self._close_tag_re is not None:
            result = self._close_tag_re.sub("", result)
        return re.sub(r"[ \t]{2,}", " ", result).strip()

    # ----------------------------------------------------- LLM 请求：注入提示词

    @filter.on_llm_request()
    async def inject_prompt(self, event: AstrMessageEvent, request: ProviderRequest):
        if not self.inject_enable:
            return
        if not self.emoji_map:
            return
        if await self._session_inactive(event):
            return

        if self.inject_mode == "probability":
            if self.inject_probability <= 0.0:
                return
            if random.random() >= self.inject_probability:
                return

        emotions_str = "、".join(self.emoji_map.keys())
        try:
            suffix = self.prompt_template.format(emotions=emotions_str)
        except (KeyError, IndexError, ValueError):
            suffix = self.prompt_template
        if suffix:
            if request.system_prompt:
                request.system_prompt += "\n\n" + suffix
            else:
                request.system_prompt = suffix

    # ------------------------------------------- LLM 响应：选图 + 改写历史文本

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        if not self.emoji_map:
            return
        if await self._session_inactive(event):
            return

        text = getattr(response, "completion_text", "") if response else ""
        if not text or self._open_tag_re is None:
            return
        if not self._open_tag_re.search(text):
            return

        # 为每个标签随机选图，缓存到 event，供发送阶段复用，保证历史里记录
        # 的文件名与实际发送的图片一致。
        picks = self._pick_for_text(text)
        event.set_extra(self._EXTRA_ORIGINAL, text)
        event.set_extra(self._EXTRA_PICKS, picks)

        # 改写写入对话历史的文本：标签 -> 摘要文字（或剥离）。
        history_text = self._build_history_text(text, picks)
        try:
            response.completion_text = history_text
        except Exception:
            pass
        self._patch_last_assistant_message(event, history_text)

        logger.info(
            f"[emojis_tag] 检测到 {len(picks)} 个情绪标签，"
            f"情绪={[p['emotion'] for p in picks]}"
        )

    def _patch_last_assistant_message(
        self, event: AstrMessageEvent, new_text: str
    ) -> None:
        """把 run_context 中最后一条 assistant 消息文本改写为 new_text。

        agent runner 在 on_llm_response 之前就把 assistant 消息追加进了
        run_context.messages，这里改写它以控制最终持久化到对话历史的内容。
        """
        try:
            from astrbot.core.pipeline.process_stage.follow_up import (
                _ACTIVE_AGENT_RUNNERS,
            )

            runner = _ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
            if not runner or not hasattr(runner, "run_context"):
                return
            messages = runner.run_context.messages
            if not messages:
                return
            last_msg = messages[-1]
            if getattr(last_msg, "role", None) != "assistant":
                return
            content = getattr(last_msg, "content", None)
            if not content:
                return
            if isinstance(content, list):
                for part in content:
                    if (
                        hasattr(part, "type")
                        and part.type == "text"
                        and hasattr(part, "text")
                    ):
                        part.text = new_text
                        break
            elif isinstance(content, str):
                last_msg.content = new_text
        except Exception as e:
            logger.debug(f"[emojis_tag] 改写 assistant 历史消息失败: {e}")

    # ----------------------------------- 发送阶段：过滤标签 + 插入表情包图片

    @filter.on_decorating_result(priority=5)
    async def decorate_result(self, event: AstrMessageEvent):
        if not self.emoji_map or self._open_tag_re is None:
            return
        if await self._session_inactive(event):
            return

        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return

        # 复用 on_llm_response 阶段挑好的图片，保证与历史记录一致；
        # 若没有（例如该结果未经过 on_llm_response），则在此基于 chain 文本现挑。
        picks = event.get_extra(self._EXTRA_PICKS)
        if not picks:
            chain_text = self._extract_text_from_chain(result.chain)
            if not chain_text or not self._open_tag_re.search(chain_text):
                return
            picks = self._pick_for_text(chain_text)

        new_chain = self._build_chain(result.chain, picks)
        if new_chain is not None:
            result.chain = new_chain

    @staticmethod
    def _extract_text_from_chain(chain: list) -> str:
        parts: list[str] = []
        for comp in chain:
            txt = getattr(comp, "text", None)
            if isinstance(txt, str) and txt:
                parts.append(txt)
        return "".join(parts)

    def _build_chain(self, chain: list, picks: list[dict[str, str]]) -> list | None:
        """重建消息链：剥离情绪标签，并在标签位置插入对应表情包图片。

        ``picks`` 是按标签出现顺序排好的选图结果，逐个消费。
        """
        if self._open_tag_re is None:
            return None

        index = 0
        new_chain: list = []
        changed = False

        for comp in chain:
            text = getattr(comp, "text", None)
            if not isinstance(comp, Plain) or not isinstance(text, str) or not text:
                new_chain.append(comp)
                continue

            if not self._open_tag_re.search(text):
                # 仍可能含残留闭合标签，顺手清掉。
                cleaned = self._strip_close_tags(text)
                if cleaned != text:
                    changed = True
                    if cleaned:
                        new_chain.append(Plain(cleaned))
                else:
                    new_chain.append(comp)
                continue

            changed = True
            last_end = 0
            for match in self._open_tag_re.finditer(text):
                segment = text[last_end : match.start()]
                segment = self._strip_close_tags(segment)
                if segment:
                    new_chain.append(Plain(segment))

                emotion = self._resolve_emotion(match.group(1))
                last_end = match.end()
                if emotion is None:
                    continue
                pick = picks[index] if index < len(picks) else None
                index += 1
                if pick is None:
                    # 没有可用图片：标签已被剥离，不插入任何内容。
                    continue
                new_chain.append(Image(file=pick["path"]))
                logger.info(
                    f"[emojis_tag] 发送表情包: {pick['emotion']}/{pick['filename']}"
                )

            tail = self._strip_close_tags(text[last_end:])
            if tail:
                new_chain.append(Plain(tail))

        if not changed:
            return None
        return new_chain

    def _strip_close_tags(self, text: str) -> str:
        if self._close_tag_re is None:
            return text
        return self._close_tag_re.sub("", text)

    # --------------------------------------------------------------- 指令

    @filter.command("reload_emojis_tag")
    async def cmd_reload(self, event: AstrMessageEvent):
        """重新加载配置并扫描表情包目录。"""
        self._load_runtime_config()
        self._scan_emojis()
        emotions = (
            ", ".join(f"{k}({len(v)})" for k, v in self.emoji_map.items()) or "无"
        )
        yield event.plain_result(f"表情包已重载\n情绪: {emotions}")

    @filter.command("emojis_tag_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前插件状态。"""
        emotions = (
            ", ".join(f"{k}({len(v)})" for k, v in self.emoji_map.items()) or "无"
        )
        total = sum(len(v) for v in self.emoji_map.values())
        yield event.plain_result(
            "🖼️ 表情包标签插件状态\n"
            f"- 目录: {self.emojis_path}\n"
            f"- 注入: {'开' if self.inject_enable else '关'} ({self.inject_mode}"
            f"{f' p={self.inject_probability}' if self.inject_mode == 'probability' else ''})\n"
            f"- 历史记录: {'转文字' if self.history_record else '剥离'}\n"
            f"- 情绪: {emotions}\n"
            f"- 总数: {total}"
        )
