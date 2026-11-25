# 图片输入（第一阶段）

在提供商设置中选择支持视觉输入的 Chat Completions 模型，打开“支持图片输入”并保存。然后在聊天输入框上传、粘贴或拖入图片；可以只发图片，也可以附带文字。该开关用于声明模型能力，不会让纯文本模型获得视觉能力。

## 存储与请求边界

```text
浏览器上传图片
  → POST /api/sessions/{session_id}/assets
  → 校验真实格式、大小、尺寸
  → 会话目录/assets/{asset_id}/original.png
  → 会话 SQLite 的 json_objects：namespace=assets，object_id=asset_id
  → 返回 asset_id、文件名、格式、尺寸

发送用户消息
  → content: [text, image(asset_id), ...]
  → 消息队列与 Turn 只保存引用
  → Runtime 装配图片读取服务
  → Provider 构造请求时读取图片、校验摘要、编码 Base64
  → Chat Completions 的 image_url 内容项
```

没有增加数据库表或修改 schema 版本。资产元数据保存文件相对位置、SHA-256、大小、尺寸、创建时间和引用保留标记。图片原件不放在 workspace，普通工具不能将它作为工作区文件覆盖。Provider 只通过注入的图片读取服务获取内容，不依赖 storage。

为了沿用已有的草稿、附件和排队交互，浏览器内部附件引用使用 `source=upload, path=asset:<asset_id>`。发送 Turn 时转换成独立 `image` 内容项，普通文件引用仍附在文本项中。

## 生命周期

- 每条直接输入最多 4 张图片，单张最多 10 MiB、2000 万像素；支持静态 PNG、JPEG、WebP，按实际文件内容识别，不信任扩展名或上传 MIME。
- 资产属于 Session，可由同会话的主 Agent 和子 Agent 使用，不能跨会话引用。
- 未进入消息的上传可以删除；进入引用校验后会保守标记保留，直到整个会话被物理删除。排队取消、回退或分支变化不会误删历史图片。若后续请求被拒绝，已保留的图片也可能留到会话删除。
- 历史、回退版本、steering、子 Agent 输入保留图片引用。上下文压缩时保留历史图片，并将图片交给视觉模型参与摘要；当前阶段不会自动以文字摘要替换原图。
- 图片令牌使用基于尺寸的保守估算，不把 Base64 字符串长度当作图片 token 数。不同模型的真实计费规则不同，实际 usage 以 Provider 返回为准；若保留的图片使压缩后仍超预算，会明确报错。
- 图片 Base64 只用于当次网络请求；消息、请求日志和 Trace 不保留图片载荷。预览走本地会话资产接口。

## 范围

本阶段只支持用户图片输入与 Chat Completions。Responses、Messages 协议收到图片会明确拒绝，避免静默丢图；普通文件解析、音频、MCP 工具返回图片和按需读取旧图工具留待后续实现。

## 验证

`tests/test_image_assets.py` 使用真实本地 SQLite、磁盘文件和 FastAPI TestClient，覆盖上传/预览/删除、伪造图片、大小限制、跨会话拒绝、仅图片 Turn、消息队列、回退、完整性校验、请求编码与日志脱敏、重复压缩后的引用保留。模型网络调用使用测试 transport，不调用付费模型。

前端测试覆盖图片内容编码及从已保存 Turn 恢复附件。完整检查命令见仓库 `AGENTS.md`。

2026-11-10 本地验证记录：

| 检查 | 结果 |
| --- | --- |
| 图片专项后端测试 | 12 passed |
| 前端全量 Vitest | 66 个文件、541 项通过 |
| TypeScript / Vite build | 通过 |
| 本次 Python 修改的 Ruff / format | 通过（31 个文件） |
| 已有浏览器集成测试 | 安装缺失的 Playwright Headless Shell 后，7 项全部通过 |
| 实际图片网页链路 | 独立临时会话：选择图片 → 只发图片 → 本地 HTTP 测试模型收到原始字节 → SSE 显示回复 → 刷新恢复图片预览，全部通过 |

完整后端首次运行结果为 `1753 passed, 80 skipped, 17 failed`。其中 7 项为缺少浏览器，补齐依赖后复测通过；其余 10 项在隔离的原始代码副本（保留用户原有修改）中同样失败：benchmark 空 Trace 预期 1 项、协作模式事件顺序 4 项、旧 Provider 默认名迁移预期 2 项、Windows Sandbox 权限/进程测试 3 项。本次没有修改这些功能。

仓库全局 Ruff 另有 `tests/test_stream_attempt_rollback.py` 的原有导入排序错误，本次未改该文件。付费视觉模型的实际识图效果尚未验证；上述网页测试使用本地模型替身检查请求中的图片字节和交互链路。
