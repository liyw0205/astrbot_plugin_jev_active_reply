# Jev 主动回复插件

**作者：木有知 · 版本：0.2.1 · AstrBot >=4.28.2,<5 · aiocqhttp 群聊**

按当前人设判断什么时候接话、自然续聊，什么时候保持沉默。Jev 负责情境判断，实际回复仍走 AstrBot 原生生成链，保留人设、记忆、图片、语音和其他插件的处理能力。

## 首次配置

通过 AstrBot 插件管理安装仓库或 ZIP。填写对应渠道的 Keys，设置 `enabled_sessions`（群号、完整 UMO，或明确填写 `*`）。空列表不启用任何群。默认 `dry_run=true`，只观察、不主动触发；观察仍消耗 Jev 请求。管理员使用 `jev状态` / `jev诊断` 查看配置和近期原因，确认后关闭 dry_run。仓库不包含任何真实 Key、聊天记录或服务器配置。

| 渠道 | Endpoint | 默认模型 | Key 字段 |
|---|---|---|---|
| TypeSafe 官方 | `https://api.typesafe.ai/v1/systemone` | `jev-latest` | `typesafe.keys` |
| MindsHub | `https://api.mindshub.ai/v1/decisions` | `jev` | `mindshub.keys` |

两家分别适配端点、模型别名和错误处理。不要使用 Chat Completions 格式，不要把 MindsHub Key 发往官方站点。

## 连续补充：先生成，不死等

默认 `burst_merge_enabled=true`、`burst_window_seconds=5`、`debounce_seconds=0`。第一条先判断、先生成，不强制等监听窗口结束。

- 只合并同群、同一发送者、同一 conversation 的非点名候选。
- 窗口内继续补充且旧轮尚未发送、未处于投递中时，合并文字和图片，停止本插件旧轮，避免同一件事重复回答。保留接收顺序和原消息 ID。
- 已输出或正在发送时，不重放前文，按新的续聊判断。不能撤销已经到达平台的消息。
- 显式 @、命令、不同人、不同 conversation、窗口外消息不合并。最多 8 条（可调 2–12），最多 12 张图片，连续合并总跨度最多 30 秒。
- `debounce_seconds` 是首条判断前的额外等待，不是连续补充监听窗口。取消旧请求不代表退还已经消耗的额度。

## 人设、上下文与微调

默认按当前 UMO 和 conversation 自动读取实际 persona。`behavior_guidance` 同时用于 Jev 判断与主模型自主轮，例如“熟悉话题可以接梗，情绪表达不要自动变成长篇建议”。它补充行为，不另造身份。

可调整参与档位、间接点名/续聊门槛、打扰容忍度、主动加入偏移，以及二审适当性与重复阈值。`persona_override` 仍为判断侧覆盖，不更改主模型人设；通常留空，优先使用 behavior_guidance。

参考原 JevGate 的可配置问题，`question_overrides` 提供高级 JSON 覆盖，例如 `{"worthwhile":{"criteria":{"true":"自然接梗、共情或有兴趣的补充，不必是提问","false":"复述、已经说完、强行说教"}}}`。只修改既有判断维度的措辞与标准，不执行表达式；错误 JSON 回退默认。目标消息从历史窗口中排除，避免重复判断同一段话。

`context_mode` 提供 quality / balanced / economy，默认效果优先。所选窗口未超安全预算时保留原文和完整人设，不机械限制每条几百字；接近预算才移除完整旧消息，保护最近对话。保护内容本身过大时暂停并诊断，不暗中截掉当前消息或人设。默认质量窗口 40 条，可调 4–100。

计数使用明确标识的保守估算，不是供应商精确 tokenizer；同时检查 state+最长问题、整包 token 约束与 UTF-8 字节硬上限。主回复模型容量不同，仍使用 AstrBot 自己的上下文管理。

二审收到主模型本次实际使用的辅助场景与记忆依据。如果这些辅助证据确实超限，默认 `overflow_evidence_policy=summarize` 使用当前主模型整理一次有损摘要，并明确标注；人设、当前消息原文以及主模型自己的原始请求不变。预算内不摘要。不希望增加该次主模型调用时可选 reject；模型容量未知、摘要失败或仍超限时暂停本轮。

## 图片、记忆与二审

Jev 是纯文本模型。默认 `image_decision_mode=native_vision`，由原生主模型先取得图片和完整上下文，选择回应或 keep_silent，再进行文字审查；这会增加生成消耗。Jev 二审不会冒充看到了像素。text_gate 模式只凭已有转述与文字先判断。

边界候选可通过 uncertain_adjudication 交给拥有完整记忆的主模型复核。ContextAware 不可用时回退到原生对话历史与有界本地记录，不重复调用其他插件的记忆写入 hook。

`second_review=always` 为默认；selective 审主动插话、图片与边界候选；off 仍保留过期、会话变化、重复、沉默和发送保护。每次二审额外一次 Jev 请求。

## 兼容边界

- ContextAware：读取公共历史接口；仅在本次自主请求中协调 strict_mode 与续聊证据，不全局关闭严格模式。
- WakePro：manage_wakepro 开启时，仅在授权群的非点名轮接管旧 Debounce/Mention/Wake 步骤。普通点名、命令、黑名单与其他群不改；卸载时恢复原步骤。
- 旧 JevGate / Core 主动唤醒：保留竞争检测。迁移应关闭被替代的旧主动入口，避免重复触发，不必关闭其他拟人插件。
- 分段、TTS、表情、撤回：保留原链路，对本事件的 send / bot 操作以及 OutputPatch 分段入口加保护，不全局修改平台类。未知插件自行绕开这些入口发送，不在保证范围内。
- 沉默与取消：尊重 keep_silent、停止标记和 STT cache-only；不复活被其他插件停止的事件。
- 工具：自主轮默认只允许沉默、图片回看、记忆召回；普通点名轮的完整工具能力不受影响。

## 多 Key 与长期运行

两家各自独立轮询。同渠道 Key 默认属于同一账户/组织，共享 RPM 与退避。只有实际属于不同配额主体时，才填写与 Keys 一一对应的 quota_group_ids；同组名共享限额。多 Key 不放大同组织服务端配额。

队列有界、二审优先、等待有截止时间。结构化认证失败隔离 Key；非 JSON 的边缘 403 不当作 Key 失效；429/529 退避。超时、断连与模糊 5xx 不自动重放。拒绝后的一次重试及跨渠道回退均需显式开启，可能消耗额度。

默认渠道日请求预算：MindsHub 80、TypeSafe 500，包含二审；设为 0 不施加本地日预算，服务端配额仍生效。每群默认不额外设日上限，可自行限制。影子群计数与正式群计数分开，但真实渠道消耗不因影子模式而消失。

SQLite 在线程中串行访问，房间、历史、审计队列均有界。日计数与渠道冷却持久化；分钟限流窗口在重载后重新建立。已知 Jev 无容量/额度或冷却时不接管 WakePro，保留原有唤醒；补足额度后管理员可用 `jev恢复` 清除临时冷却，该命令不发上游请求、不补发旧消息。不要把单元测试当成长期稳定或真实 QQ 已读回执。

## 测试

开发依赖：`python -m pip install -r requirements-dev.txt`。独立测试：`python -m pytest tests/test_client.py tests/test_policy.py tests/test_http.py -q`。

全部集成测试需要 AstrBot 4.28.2 运行依赖，并将 AstrBot 源码根目录和插件父目录加入 PYTHONPATH，再运行 tests/。测试使用真实 Core 类型和受控 mock，不发送真实 QQ 消息或真实推理请求。

## 致谢

参考 JevGate 的门控思路与 WakePro 的唤醒使用体验，依赖 AstrBot 插件接口，并适配 ContextAware 等生态插件。第三方源码、凭据和运行数据不随本仓库分发。
