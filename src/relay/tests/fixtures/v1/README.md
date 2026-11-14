# v1 协议样本

Messages 和 Responses 样本由 Nexus 依据协议独立编写。请求、文本、计数和标识均为合成数据。每份 JSON 中的 `events` 按上游发送顺序排列，`evidence` 是 Rust 导出的事实，`normalized` 是 Python Worker 对同一事实、相应价格维度的预期数量。

WebSocket 样本包含 GET Upgrade 的请求头、工具调用、同一响应 ID 续接的第二轮及取消文本事件。测试逐轮读取各自的终态与用量。双端网络连接、重连与取消竞态由 P3b 验收。

Messages 样本使用普通输入 80、读取缓存 20、创建五分钟缓存 10、最终输出 7。Responses 样本使用输入 100、缓存命中 25、输出 20、其中推理 6。费用依赖冻结价格。隔离数据库用例另以普通输入 2、输出 3、每 token 1 USD 的合成售价核对单次 5 USD 扣款。

[Messages 流事件](https://platform.claude.com/docs/en/build-with-claude/streaming) · [Responses 流事件](https://developers.openai.com/api/docs/guides/streaming-responses) · [Responses WebSocket](https://developers.openai.com/api/docs/guides/websocket-mode)
