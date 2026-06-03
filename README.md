# astrbot_plugin_emojis_tag

通过**提示词注入 + 情绪标签**让 LLM 自主发送表情包的 AstrBot 插件。

模型在回复里写一个情绪标签（如 `<happy>`），插件会：

1. 把标签从发送给用户的内容里过滤掉；
2. 从对应情绪文件夹里**随机挑一张表情包**，**随机穿插**在回复的句子之间发给用户；
3. 把标签转写成一段文字（如 `[发送了表情包：happy-xxx.jpg]`）追加到 bot 回复
   末尾、记入对话历史，保证对话数据中不出现原始标签。

## 工作流程

```
on_llm_request   → 注入提示词（占位符 {emotions} = 扫描到的情绪文件夹名）
on_llm_response  → 解析 <情绪> 标签，为每个标签随机选图；剥离标签后把摘要追加到正文末尾写入历史
on_decorating_result → 从消息链剥离标签，并把表情包图片随机穿插进句子之间
发送给用户       → 干净文本 + 随机穿插的表情包图片，无标签
```

## 表情包目录结构

在配置的「表情包根目录」下，按情绪建立子文件夹，子文件夹名即情绪名：

```
emojis/
├── happy/      # <happy> 标签会从这里随机发一张
│   ├── a.jpg
│   └── b.png
├── sad/
│   └── c.gif
└── angry/
    └── d.webp
```

支持的图片格式：`.jpg .jpeg .png .gif .webp .bmp`。

> 留空「表情包根目录」时默认使用插件目录下的 `emojis/`。只有**包含图片的子文件夹**才会被视为有效情绪并注入提示词。

## 提示词注入：始终 / 概率

| 模式 | 行为 |
| --- | --- |
| `always`（默认） | 每轮都注入提示词，由 LLM 自己决定要不要发表情包 |
| `probability` | 仅当 roll 命中「注入概率」时才注入提示词，未命中的那轮模型看不到表情包能力，因此不会发 |

## 对话历史中的标签处理

无论如何配置，**发送给用户的消息与写入对话数据的文本都不会出现 `<情绪>` 原始标签**。

| `history.record` | 行为 |
| --- | --- |
| `true`（默认） | 标签替换成可自定义的摘要文字写入历史，让 AI 记得自己发过哪张表情包 |
| `false` | 直接从历史中剥离标签，不留痕迹 |

摘要统一**追加在 bot 正文之后并换行**，例如：

```
好呀，今天天气不错
[发送了表情包：happy-xxx.jpg]
```

摘要文字模板 `history.template` 默认 `[发送了表情包：{emotion}-{filename}]`：

- `{emotion}`：情绪名（即子文件夹名）
- `{filename}`：实际随机选中的图片文件名（含扩展名）

## 配置项

在 AstrBot 管理面板中配置：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| 表情包根目录 | （空，用插件目录 `emojis/`） | 情绪子文件夹的父目录，支持绝对/相对路径 |
| 提示词注入 → 启用 | `true` | 总开关 |
| 提示词注入 → 注入模式 | `always` | `always` / `probability` |
| 提示词注入 → 注入概率 | `0.3` | 仅 `probability` 模式生效 |
| 提示词注入 → 提示词模板 | 内置 | 含 `{emotions}` 占位符 |
| 对话历史 → 转写为文字 | `true` | 关闭则剥离标签 |
| 对话历史 → 摘要模板 | `[发送了表情包：{emotion}-{filename}]` | 含 `{emotion}` / `{filename}` 占位符 |

## 指令

- `/emojis_tag_status`：查看当前目录、注入模式、已扫描到的情绪与表情包数量。
- `/reload_emojis_tag`：重新加载配置并重新扫描表情包目录（新增情绪文件夹后无需重启）。

## 与 active_function 等插件共存

本插件的 `on_decorating_result` 运行在较高优先级（priority=20），早于
[active_function](https://github.com/L1ke40oz/astrbot_plugin_active_function)
的引用回复 / 戳一戳 / 撤回处理。这样：

- 表情包图片在它们「接管发送」之前就已作为非文本组件进入消息链，会被一并保留
  发送——**修复了引用回复时表情包发不出去的问题**；
- 插件会清理 active_function 缓存的文本快照，避免 `<情绪>` 标签或摘要被当成
  正文发给用户。

> 注意：当 active_function 接管发送（如引用回复）时，它会把图片等非文本组件放到
> 文本之后发送，此时表情包不再穿插在句子中间，而是出现在整段文字之后。普通回复
> （无引用/戳一戳/撤回）下仍是随机穿插。

## 已知限制

- 标签的过滤与图片插入发生在 `on_decorating_result` 阶段；在**流式输出**模式下该钩子可能不触发，标签可能短暂泄漏，建议配合非流式输出使用。
- 插件只处理与已扫描情绪同名的标签，不会干扰其它插件的标签（如 `[poke]`、`<tts>`）；含 `<tts>` 等尖括号标签的文本片段不会被切分，以免破坏配对。

## 参考

- [astrbot_plugin_sendemojis](https://github.com/L1ke40oz/astrbot_plugin_sendemojis)：按概率由模型判断情绪后发送表情包。
- [astrbot_plugin_active_function](https://github.com/L1ke40oz/astrbot_plugin_active_function)：标签过滤并转写进对话历史的实现参考。
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
