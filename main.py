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
    _EXTRA_SUMMARIES = "_emojis_tag_summaries"
    # active_function 插件在 on_llm_response 里缓存的原始文本快照，其引用回复/
    # 戳一戳/撤回的「接管发送」会以此为文本来源；需在装饰阶段清掉本插件的标签。
    _ACTIVE_FUNC_EXTRA = "_active_func_original_text"

    # 句末标点，用于把文本切分成可随机穿插表情包的片段。
    _SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?~…\n])")

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.config: dict[str, Any] = config or {}
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        # 压缩后图片的缓存目录（按 原图mtime+尺寸+质量 命名，命中即复用）
        self._resize_cache_dir = os.path.join(self.plugin_dir, ".resized_cache")

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

        image_cfg = cfg.get("image", {}) or {}
        # 发送前把图片最长边等比压缩到 max_side，避免原图分辨率过大。
        self.image_resize: bool = bool(image_cfg.get("resize", True))
        self.image_max_side: int = max(1, int(image_cfg.get("max_side", 512) or 512))
        self.image_quality: int = max(
            1, min(100, int(image_cfg.get("quality", 85) or 85))
        )

    @staticmethod
    def _default_prompt_template() -> str:
        return (
            "你可以在回复中插入情绪标签来给用户发送对应情绪的表情包。\n"
            "当前可用的情绪表情包有：{emotions}。\n"
            "当你想发某种情绪的表情包时，在回复文本中插入 <情绪名> 形式的标签即可，"
            "例如想发开心的表情包就写 <happy>。\n"
            "规则：\n"
            "1. 只能使用上面列出的情绪名，不要编造列表之外的情绪。\n"
            "2. 想发某种情绪的表情包时，在回复里写一个对应标签即可；"
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

    def _summaries_for(self, picks: list[dict[str, str]]) -> list[str]:
        """为每个选中的表情包生成历史摘要文字。"""
        summaries: list[str] = []
        for pick in picks:
            try:
                summaries.append(
                    self.history_template.format(
                        emotion=pick["emotion"], filename=pick["filename"]
                    )
                )
            except (KeyError, IndexError, ValueError):
                summaries.append(self.history_template)
        return summaries

    def _build_history_text(
        self, text: str, summaries: list[str]
    ) -> str:
        """构造写入对话历史的文本：先剥离所有情绪标签，再把摘要追加到正文末尾。

        与原位替换不同，摘要统一放在 bot 正文之后并换行，例如：

            好呀，今天天气不错\n[发送了表情包：happy-xxx.jpg]
        """
        body = self._strip_all_tags(text)
        if not self.history_record or not summaries:
            return body
        tail = "\n".join(summaries)
        return f"{body}\n{tail}" if body else tail

    def _strip_all_tags(self, text: str) -> str:
        """剥离文本中所有已知情绪的开/闭标签，并归一空白。"""
        if self._open_tag_re is not None:
            text = self._open_tag_re.sub("", text)
        if self._close_tag_re is not None:
            text = self._close_tag_re.sub("", text)
        return re.sub(r"[ \t]{2,}", " ", text).strip()

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
        summaries = self._summaries_for(picks)
        event.set_extra(self._EXTRA_PICKS, picks)
        event.set_extra(self._EXTRA_SUMMARIES, summaries)

        # completion_text 决定「发送给用户」的内容，因此只放剥离情绪标签后的干净
        # 正文，绝不能塞进摘要——否则当模型只发表情包（正文为空、仅含别的插件的
        # 标签如 [NEXT]）时，摘要会被当成正文发出去（与 proactive_message 冲突）。
        clean_body = self._strip_all_tags(text)
        try:
            response.completion_text = clean_body
        except Exception:
            pass

        # 摘要只写入持久化的对话历史（run_context），让模型记得自己发过表情包，
        # 不影响实际发送的消息。
        history_text = self._build_history_text(text, summaries)
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

    # ----------------------------------- 发送阶段：过滤标签 + 随机穿插表情包

    # 优先级高于 active_function 的 poke(12)/reply(11)/recall(10) 及 TTS(13)，
    # 这样在它们「接管发送」之前，表情包图片已作为非文本组件存在于消息链中，
    # 会被它们的接管逻辑一并保留并发送，避免引用回复时表情包丢失。
    @filter.on_decorating_result(priority=20)
    async def decorate_result(self, event: AstrMessageEvent):
        if not self.emoji_map or self._open_tag_re is None:
            return
        if await self._session_inactive(event):
            return

        result = event.get_result()
        if result is None:
            return
        chain = list(getattr(result, "chain", None) or [])

        # 复用 on_llm_response 阶段挑好的图片，保证与历史记录一致；
        # 若没有（例如该结果未经过 on_llm_response），则在此基于 chain 文本现挑。
        picks = event.get_extra(self._EXTRA_PICKS)
        if not picks:
            chain_text = self._extract_text_from_chain(chain)
            if not chain_text or not self._open_tag_re.search(chain_text):
                # 没有本插件的标签，无需处理。
                return
            picks = self._pick_for_text(chain_text)
        if not picks:
            return

        # 注意：completion_text 已被 on_llm_response 剥离情绪标签，chain 里通常已无
        # 标签；图片靠 picks 插入，即使正文为空也要把表情包发出去。
        images = [Image(file=self._maybe_resize(p["path"])) for p in picks]
        new_chain = self._build_chain(chain, images)
        if new_chain is not None:
            result.chain = new_chain
        for p in picks:
            logger.info(
                f"[emojis_tag] 发送表情包: {p['emotion']}/{p['filename']}"
            )

        # 清理 active_function 缓存的文本快照，避免其引用回复/戳一戳/撤回的
        # 接管发送把本插件的标签或摘要当作文本发给用户。
        self._sanitize_active_function_extra(event)

    @staticmethod
    def _extract_text_from_chain(chain: list) -> str:
        parts: list[str] = []
        for comp in chain:
            txt = getattr(comp, "text", None)
            if isinstance(txt, str) and txt:
                parts.append(txt)
        return "".join(parts)

    def _build_chain(self, chain: list, images: list) -> list | None:
        """重建消息链：剥离残留情绪标签，再把表情包图片随机穿插进文本片段之间。

        - 有图片要插入时：始终把正文切成句子片段并随机穿插图片（即便 chain 里
          已无标签——标签通常已在 on_llm_response 阶段从 completion_text 剥离）。
        - 没有图片时：仅当确实剥离了残留标签才返回新链，否则返回 None 不改动。
        """
        if self._open_tag_re is None:
            return None

        segments, changed = self._segmentize(chain)
        if not images:
            return segments if changed else None
        return self._interleave(segments, images)

    def _segmentize(self, chain: list) -> tuple[list, bool]:
        """剥离残留情绪标签并把纯文本切成句子片段，便于随机穿插。

        返回 ``(segments, changed)``；``changed`` 表示是否剥离掉了情绪标签。
        非 Plain 组件（如 TTS 的 Record）以及含有其它 ``<...>`` 标签的文本片段
        保持原子，避免破坏别的插件的标签。
        """
        changed = False
        segments: list = []
        for comp in chain:
            text = getattr(comp, "text", None)
            if not isinstance(comp, Plain) or not isinstance(text, str) or not text:
                segments.append(comp)
                continue

            if (self._open_tag_re is not None and self._open_tag_re.search(text)) or (
                self._close_tag_re is not None and self._close_tag_re.search(text)
            ):
                changed = True
            cleaned = self._strip_all_tags(text)
            if not cleaned:
                continue
            # 含其它尖括号标签（如 <tts>）的文本不切分，避免破坏配对。
            if "<" in cleaned or ">" in cleaned:
                segments.append(Plain(cleaned))
                continue
            for piece in self._SENTENCE_SPLIT_RE.split(cleaned):
                if piece.strip():
                    segments.append(Plain(piece))

        return segments, changed

    def _interleave(self, segments: list, images: list) -> list:
        """把每张表情包图片随机插入到 segments 的片段边界之间。"""
        if not segments:
            return list(images)
        slots = len(segments) + 1
        positions = sorted(random.randrange(slots) for _ in images)
        new_chain: list = []
        img_idx = 0
        for i in range(slots):
            while img_idx < len(images) and positions[img_idx] == i:
                new_chain.append(images[img_idx])
                img_idx += 1
            if i < len(segments):
                new_chain.append(segments[i])
        return new_chain

    def _sanitize_active_function_extra(self, event: AstrMessageEvent) -> None:
        """从 active_function 的文本快照里移除本插件的标签与摘要文字。

        active_function 的引用回复/戳一戳/撤回在「接管发送」时以该快照为文本
        来源，若不清理，本插件的 <情绪> 标签或摘要会被当成正文发给用户。
        """
        orig = event.get_extra(self._ACTIVE_FUNC_EXTRA)
        if not isinstance(orig, str) or not orig:
            return
        cleaned = self._strip_all_tags(orig)
        for summary in event.get_extra(self._EXTRA_SUMMARIES) or []:
            if summary:
                cleaned = cleaned.replace(summary, "")
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        if cleaned != orig:
            event.set_extra(self._ACTIVE_FUNC_EXTRA, cleaned)

    # ------------------------------------------------- 发送前图片压缩
    # 仅压缩静态格式；gif/webp 可能是动图，缩放会破坏动画，故跳过。
    _RESIZABLE_EXTENSIONS: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".bmp")

    def _maybe_resize(self, path: str) -> str:
        """把图片最长边等比压缩到 ``image_max_side``，返回可发送的路径。

        失败、未启用、无需缩放或缺少 Pillow 时，原样返回 ``path``。
        """
        if not self.image_resize:
            return path
        ext = os.path.splitext(path)[1].lower()
        if ext not in self._RESIZABLE_EXTENSIONS:
            return path
        try:
            from PIL import Image as PILImage
        except Exception:
            logger.warning(
                "[emojis_tag] 未安装 Pillow，跳过图片压缩；如需压缩请 pip install pillow"
            )
            return path

        try:
            mtime = int(os.path.getmtime(path))
            stem = os.path.splitext(os.path.basename(path))[0]
            cache_name = f"{stem}_{self.image_max_side}_{self.image_quality}_{mtime}{ext}"
            cache_path = os.path.join(self._resize_cache_dir, cache_name)
            if os.path.isfile(cache_path):
                return cache_path

            with PILImage.open(path) as im:
                width, height = im.size
                longest = max(width, height)
                if longest <= self.image_max_side:
                    return path  # 本就不大，无需压缩

                scale = self.image_max_side / float(longest)
                new_size = (
                    max(1, int(round(width * scale))),
                    max(1, int(round(height * scale))),
                )
                resized = im.resize(new_size, PILImage.LANCZOS)

                os.makedirs(self._resize_cache_dir, exist_ok=True)
                save_kwargs: dict[str, Any] = {}
                if ext in (".jpg", ".jpeg"):
                    resized = resized.convert("RGB")
                    save_kwargs = {"quality": self.image_quality, "optimize": True}
                elif ext == ".png":
                    save_kwargs = {"optimize": True}
                resized.save(cache_path, **save_kwargs)
            logger.debug(
                f"[emojis_tag] 压缩图片 {os.path.basename(path)}: "
                f"{width}x{height} -> {new_size[0]}x{new_size[1]}"
            )
            return cache_path
        except Exception as e:
            logger.warning(f"[emojis_tag] 图片压缩失败，发送原图: {path} ({e})")
            return path

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
