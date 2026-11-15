# Rust Relay 迁移与设计方案

Relay 对外提供 Anthropic Messages 和 OpenAI Responses。Messages 提供 `POST /v1/messages`，Responses 在 `/v1/responses` 同时提供 HTTP POST 和 GET WebSocket Upgrade。部署可使用原生 Messages、原生 Responses 或 Amazon Bedrock Converse；Responses WebSocket 使用具备原生 WSS 能力的部署。

**首期设计已于 2026-11-14 定稿。** Rust 节点已实现两种原生 HTTP/SSE 链路、Messages 与 Responses 双向 HTTP/SSE 转换及 Responses 双端 WebSocket。各传输共用节点准入和结算契约。 在 `src/relay/` 先跑通两条原生链路，各自从企业调用 Key、资金预留走到可核对的 USD 消费流水，再分别验证两个转换方向。每条链路单独声明可用能力。[开发计划与验收用例](relay-development.md)

## 代码复用范围与维护归属

Nexus 自主设计和维护 Relay。CC Switch 仅作为初次开发时可复用的代码和测试来源，用于减少协议转换、SSE 分帧等工作量。Relay 的对外行为以 Anthropic Messages、OpenAI Responses 的官方协议及 Nexus 的控制平面契约为准。功能取舍、缺陷修复和版本发布均按 Nexus 自身需求推进，独立于 CC Switch 的发布周期。

初次选材参考 `farion1231/cc-switch` 的提交 `8c76bfe1e2628fe385b2c009020036d85510b01d`。下表路径相对该提交的 `src-tauri/src/proxy/`。提交号用于追溯复用片段；实际引入时记录原路径和本地改动，随代码与发行包保留相应版权及许可声明。[参考源码](https://github.com/farion1231/cc-switch/tree/8c76bfe1e2628fe385b2c009020036d85510b01d/src-tauri/src/proxy) · [原项目许可证](https://github.com/farion1231/cc-switch/blob/8c76bfe1e2628fe385b2c009020036d85510b01d/LICENSE)

| 能力 | 源码锚点 | 迁移处理 |
| --- | --- | --- |
| 两个协议间的请求与响应 | `providers/transform_responses.rs`、`providers/transform_codex_anthropic.rs` | 前者服务 Messages 客户端到 Responses 上游，后者服务 Responses 客户端到 Messages 上游。按方向抽取工具 ID、参数与输出项映射的代码和样本；共享 helper 按用途提取，保持转换路径直接连接这两个协议。 |
| SSE 与流转换 | `sse.rs`、`providers/streaming_responses.rs`、`providers/streaming_codex_anthropic.rs` | 原生链路保留各自事件。转换时分别验证 Responses SSE 到 Messages SSE、Messages SSE 到 Responses SSE；覆盖跨块 UTF-8、工具参数增量、终止、错误、慢客户端和半帧上限。 |
| HTTP 传输 | `http_client.rs`、`hyper_client.rs`、`content_encoding.rs`、`response_processor.rs` | 保留连接复用、响应流、有限解压与 hop-by-hop 头处理的行为测试；用服务端客户端池管理 TLS、超时和取消。上游地址与鉴权材料仅来自已授权部署。 |
| API 入口与编排 | `server.rs`、`handlers.rs`、`handler_context.rs`、`forwarder.rs`、`error_mapper.rs` | 参考端点行为及错误映射，重建请求生命周期。每次发送上游前都必须有已提交的尝试记录，包括重试。 |
| 路由与故障转移 | `provider_router.rs`、`circuit_breaker.rs`、`failover_switch.rs`、`model_mapper.rs` | 候选、优先级、权重及公开模型到上游模型名的映射取自 Nexus 快照。熔断如引入，只影响节点在授权候选内的选择；运营配置仍由 catalog/publication 修改。 |
| 用量与成本 | `usage/parser.rs`、`usage/calculator.rs`、`usage/logger.rs` | 按端点复用原始计数字段识别和测试样本。价格、折扣和预算归 Nexus 的 billing 模块，消费由 Worker 结算。上游部分解析和流转换会为缺失用量填零，移植时必须保留“字段缺失”的信息。[计数解析](https://github.com/farion1231/cc-switch/blob/8c76bfe1e2628fe385b2c009020036d85510b01d/src-tauri/src/proxy/usage/parser.rs) |
| 桌面与供应商特例 | `session/`、`providers/*_oauth_auth*`、`copilot_*`、各类优化器与 rectifier | 桌面配置接管、SQLite、托盘、订阅账号登录、客户端身份模拟均在迁移范围之外。内容改写、自动删减媒体、私有字段清理逐项评审；能力不匹配时返回可识别的错误。 |

## new-api 的 Relay 与分词参考

对照 Herta-Network/new-api 的提交 `476886f40defd27432e41e79a8c0327c27bab03f`。CC Switch 提供候选 Rust 片段，new-api 提供 Go 实现的行为对照。RelayKit 和 TokenKit 自身文档均声明沿用项目的 AGPLv3；本方案参考其行为与测试场景，Rust 实现和样本由 Nexus 独立维护。具体引入第三方源码、测试数据或词表时，分别记录来源与许可。[RelayKit 说明](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/relaykit/README.md) · [TokenKit 说明](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/tokenkit/README.md) · [项目声明](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/NOTICE)

| 对照位置 | Nexus 的设计取舍 |
| --- | --- |
| Messages、Responses 的请求准备与渠道调用 | 对照各入口如何准备请求、选择适配器及处理上游响应。Nexus 在最终出站请求上核对字段和上界，并在每次上游发送前取得已提交的尝试记录。[Messages 入口](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/relay/claude_handler.go) · [Responses 入口](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/relay/responses_handler.go) |
| RelayKit 的请求转换、响应转换、逐块处理和终止 | 分方向测试工具 ID、参数增量、交错输出项和最终状态。注册表中部分响应转换通过 Chat 组合；Nexus 的两条转换方向均直接连接 Messages 和 Responses，测试同时检查实际转换路径和输出语义。[转换注册表](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/relaykit/relayconvert/text_converter_registry.go) · [响应测试](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/relaykit/relayconvert/response_registry_test.go) |
| 流式 usage 的来源 | new-api 的 Responses 累加器在结束时可能根据输出文本或预估输入补计 Token。Nexus 单独保存上游给出的计数及字段存在性，测试要能区分“缺失”和“明确为零”。计数缺失时沿用已有的等待或财务核对流程。[Responses 累加器](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/service/responses_usage.go) · [Messages 流计量用例](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/relaykit/relayconvert/internal/claude_messages/stream_billing_usage_test.go) |

## 服务边界

首期使用一个 Cargo 包，名称为 `nexus-relay`。以下是实现职责，按交付阶段建立文件。

| 位置 | 责任 |
| --- | --- |
| `src/relay/src/main.rs`、`server/` | 读取部署配置，建立数据库与 HTTP 连接池，挂载已启用路由，处理就绪、排空和关闭。 |
| `protocol/` | 请求/响应模型、协议转换、SSE 分帧、终止语义和厂商用量字段提取。核心函数用固定样本即可测试。 |
| `upstream/` | 根据部署建立请求，应用解密凭据、连接超时、首字节及流空闲超时；请求路径按端点构造，上游鉴权头由 Relay 注入，跨主机重定向不能携带原凭据。 |
| `control/` | 八个 `gateway_v1` 调用、集群快照、密文包、Redis 多层限额、租约与心跳。以 Python 模拟器作为契约测试的对照端。 |
| `request/` | 单次调用的模型与部署选择、上界确认、预留、尝试、交付、证据上报和清理。仅此处决定是否发起下一次尝试。 |

入口凭据按各自协议读取企业调用 Key：Messages 使用 `x-api-key`，Responses 使用 `Authorization: Bearer`。发往上游的凭据来自授权部署。

部署配置提供节点专属 PostgreSQL 登录、Redis 地址与命名空间、集群私钥、HTTP 监听和资源上限。运行时按集群接收模型、应用 Key 摘要、策略、部署和凭据引用。人员会话、支付凭据、上游存储根密钥只属于控制平面。客户端提供的模型名必须解析为授权模型 ID，客户端传入的 URL、组织、价格或上游密钥均不能改变路由。[快照来源](../src/nexus/modules/catalog/persistence/snapshots.py) · [数据平面契约](plane-protocol.md)

## 节点内部接口与资源

采用单个 Rust crate，库承载可测逻辑，二进制负责配置与生命周期。基于 Tokio、Axum 和 reqwest/rustls 实现 HTTP/SSE，使用 Axum WebSocket 接收升级、Tokio WebSocket 客户端配合 rustls 拨上游 WSS；PostgreSQL 使用异步驱动和有界池，Redis 使用异步客户端。集群凭据解密以 PyNaCl 生成的 libsodium sealed box 测试向量验证互操作。库版本在实现时锁入 Cargo.lock。

| 边界 | 输入 → 输出 | 所有者及并发约束 |
| --- | --- | --- |
| 节点配置 | 数据库快照原文、摘要、集群身份 → 已校验的不可变快照 | `control/` 按修订构建模型、Key 摘要和部署索引。每个请求持有同一快照的共享引用；后台刷新按节点合并并加随机错峰。 |
| 协议准入 | 入口请求、模型、部署能力 → 可执行候选、最终上游参数、输入和输出上界 | `protocol/` 识别并验证字段，`request/` 从可用候选中按优先级、权重选择。重试候选须能在相同快照和已冻结上界内执行：排除本请求已失败的部署后，先按同组剩余权重抽取，组内用完再降组。 |
| 上游执行 | 已确认的尝试、部署地址、解密凭据 → HTTP 状态、响应体或事件流 | `upstream/` 复用连接池，关闭自动重定向、HTTP 客户端的隐式重试及负载均衡层对生成请求的透明重发。从入口头部白名单构造请求，单独注入部署凭据；DB 连接在网络调用前归还池。 |
| 流的两路消费 | 同一上游字节流 → 客户端输出、当前尝试的用量证据 | 一个任务读取并分帧。先提取原始证据，再制作客户端事件。积压超过有界下游缓冲时暂停读取；独立任务在租约安全截止前续租。 |
| 请求完成 | 交付结果、已登记尝试、用量 → v1 事件及提交回执 | `request/` 持有 request_id、尝试序号、revision 和租约。事件首次生成时固定 ID、时间与字节。完成任务受进程监督，脱离客户端连接后仍可有界重放。 |

HTTP POST 路径延迟写出响应头，直到拿到可交付的上游响应。WebSocket 的 HTTP 101 只完成连接升级，之后的生成错误通过会话事件返回。写出前可返回对应入口协议的错误体；鉴权失败用 401，权限拒绝用 403，速率受限用 429，必要依赖不可用用 503。写出 SSE 后只能通过该协议可表达的流内错误或断流结束。跟踪上游终态、下游交付状态和计量完备性三个值；已开始交付的流始终由原尝试收尾。

节点就绪要求存在有效且已确认的集群快照、可用的准入依赖及足够的本地执行容量。部署配置分别限制请求体、解压后响应、单帧、工具参数缓冲、并发流和待确认事件数。超限在能判断的最早阶段处理。终止时先退出负载均衡，再限时排空活跃流及待确认事件，最后关闭池。进程被强制终止后仍可能缺失最后一条证据，沿用控制平面的保留预留与人工核对流程。

## 协议覆盖与交付条件

下表的箭头表示请求从客户端到上游的方向，响应沿原路返回。双向转换以对应原生链路为验收基准，两个方向分别按部署启用。

| 客户端入口 | 上游端点 | 首期处理与验收 |
| --- | --- | --- |
| `POST /v1/messages` | Anthropic Messages | 原生文本与 SSE。验证内容块顺序、工具调用 ID 与输入增量、结束原因及缓存读写用量。 |
| `POST /v1/responses` | OpenAI Responses | 原生文本与 SSE。验证输出项、函数调用与结果关联、完成/失败/未完成状态及最终 usage。 |
| `GET /v1/responses`（WebSocket Upgrade） | OpenAI Responses WSS | 下游和上游各完成原生握手，逐轮转发 create 与有类型的 JSON 事件，验收续接、取消和独立结算。 |
| `POST /v1/messages` | OpenAI Responses | 将 Messages 请求转换为 Responses，返回 Messages 响应或 SSE。逐项核对工具往返、停止语义和客户端可见内容。 |
| `POST /v1/responses` | Anthropic Messages | 将 Responses 请求转换为 Messages，返回 Responses 输出项或 SSE。核对调用 ID、参数增量、终止状态及不可映射字段的错误行为。 |

入口路由使用明确的 HTTP 方法和路径。Chat Completions 客户端通过 Responses 表示接入现有路由。Gemini 暂无客户端入口。每个启用组合分别记录文本、SSE、工具能力、真实用量格式和输入输出上界。WS 续接按下一节校验同一连接内的前序响应。需要跨连接持久会话或额外计价的托管工具，依各自的归属、恢复和计量能力另行启用。[目录当前字段](../src/nexus/modules/catalog/application/schemas.py)

## Responses WebSocket 会话

首期交付的原生路径为 **客户端 → Relay → 支持 WebSocket 的 Responses 部署**。客户端向 `/v1/responses` 发起带 Bearer 企业 Key 的 GET Upgrade，成功后通过 JSON 文本消息发送扁平的 `response.create`。Relay 使用部署凭据连接已发布的 WSS 端点，将经过准入的生成请求作为 WebSocket 消息写入上游，并将带类型的 Responses 事件作为 WebSocket 消息发回客户端。HTTP POST/SSE 继续使用各自的传输路径。两段握手、两轮复用及逐轮收费都在同一个端到端用例中验证。[OpenAI Docs：WebSocket Mode](https://developers.openai.com/api/docs/guides/websocket-mode)

new-api 的控制器在 Upgrade 后为每轮创建独立请求上下文，Relay 在首次选定渠道后拨号并复用 target；同一连接有活跃生成时，后续 create 会收到关联错误。Nexus 采用这个串行会话范围：一条客户端连接绑定一条专属上游连接，每次最多一个在途生成；同一 socket 上重叠的 create 返回与该事件关联的冲突错误。客户端可用多条连接并发，共用组织、项目与 Key 限额。首期覆盖普通逐轮生成、工具结果续接和逐轮取消；并行命名流、跨流分支、steering 和 `generate:false` 预热属于单独的能力。遇到未启用的事件或参数时，在发送上游前返回可关联错误。[new-api 握手与逐轮中间件](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/controller/responses_websocket.go) · [上游拨号、会话绑定与读写](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/relay/responses_websocket.go)

| 时点 | 会话与上游行为 | 资金与事件 |
| --- | --- | --- |
| 下游握手 | `server/` 校验企业 Key、连接容量与握手头，使用 Authorization 携带企业 Key；按 HTTP 返回拒绝或 101。保存凭据归属和节点连接标识；首个有效 create 才选择模型、部署并拨上游 WSS。使用授权地址和部署凭据构建上游握手。 | 握手和 ping/pong 只占连接资源。空闲连接不占生成租约，不产生资金预留、尝试或扣费。 |
| 每轮 create | 使用最新已确认快照重查 Key、模型、组织策略及固定部署的 WS 能力，验证本轮完整上下文上界。首轮绑定组织、项目、Key、模型、部署及凭据版本。后续轮继续核验绑定；撤权、轮换导致原凭据失效、部署失效或模型切换时拒绝本轮并终止失效会话。 | 每轮独立生成 Nexus request_id，取得 Redis 租约，调用 `reserve_request`。首次拨号失败且无已开始尝试时取消预留并按零用量结束该轮租约；上游握手成功或已有可用连接后，提交 `start_attempt` 才写出该轮 create。SQL 与 Redis 提交结果未知时沿用原业务 ID 查证。 |
| 事件与终态 | 上下游各有单一读取循环和有界写队列。按当前轮及上游 response ID 关联增量、工具和终态；可用时回显客户端 event_id/stream_id 用于错误归属。接受单路的 stream_id 时保持事件标识一致。迟到事件和控制指令错误不能结束另一轮。 | 按实际交付及原始上游 usage 提交 `attempt.finished` 和 `request.closed`。同一终态重放只结清一次。生成租约于本轮结束时核销；保留连接供下一轮使用。 |
| 取消、掉线与排空 | `response.cancel` 仅作用于匹配的活跃轮，按上游能力转发或关闭专属上游连接。取消与 create 竞态按发送顺序处理，控制错误单独应答。节点排空时停止新握手和新轮次，限时完成在途交付与证据上报，再关闭双端。 | 无尝试时取消预留；有尝试后按实际可证实的取消、交付、用量或未知结果上报。只重放 Nexus 的事实写入；已可能发送到上游的 create 不自动重发。 |

**续接与重连。** 默认使用 `store=false`。部署未在 `provider_features` 中启用 `stored_responses` 时，`store:true`、`background` 和 Nexus 响应句柄都在预留前以 `feature_disabled` 拒绝，见[上游供应商功能开关](#上游供应商功能开关)。同一活连接上保存已完成响应 ID、租户/Key、模型、部署、上游连接代次和上界依据等有界元数据；仅接受该会话可确认的 `previous_response_id`，所有权或父上下文不可确认时在准入前返回 `previous_response_not_found`。工具结果可作为下一轮增量输入。预留和 TPM 覆盖本轮**含前序上下文的完整输入上界**；无法从可信元数据推导时采用经验证的部署硬上界，仍无法覆盖则在准入前拒绝。每轮使用自己的实际 usage。客户端 event_id、上游 response ID 和 Nexus 资金幂等键各自独立。

依赖连接内历史的上游给出 `previous_response_not_found` 时，客户端须重发历史；显式存储的 Nexus 响应句柄可跨连接重新校验。返回协议错误，由客户端清空引用并显式提交完整历史；新请求重新选路和预留。已知上游错误按普通尝试事实收尾；写出结果未知的原请求保留核对。整个过程中避免缓存客户完整历史以重建上游会话。[OpenAI Docs：续接与连接本地状态](https://developers.openai.com/api/docs/guides/websocket-mode#continue-with-incremental-inputs)

**部署与运维。** catalog 在 Responses 部署上单独声明允许的传输（HTTP、WebSocket）和已验证的续接/取消能力，publication 发布给具备相应能力的节点。只有 HTTP 能力的部署不进入原生双端 WS 候选。跨协议转换的首期验收沿用 HTTP/SSE；Messages 入口继续使用 HTTP。负载均衡支持 Upgrade、长连接和排空。分别限制每租户/节点的空闲连接与活跃轮数、每条消息的重组大小、写队列、握手/首事件/空闲时限和连接寿命。长轮独立续租；背压超限时取消并核对。记录两端握手、连接复用、首事件、逐轮错误、取消、断线和积压，日志保留关联 ID，清除两端认证头和消息正文。

## 上游供应商功能开关

以下功能会把客户内容保存在上游供应商账户中，或经由该账户发往第三方。部署在 `relay_profile.provider_features` 中逐项列出才启用，存量部署和新部署默认都不启用。

| 功能 | 端点 | 涉及的请求 |
| --- | --- | --- |
| `stored_responses` | Responses | `store:true`、`/v1/responses/{id}` 的读取和删除、以 `resp_nx_` 句柄续接或 compact |
| `background` | Responses | `background:true`，须同时启用 `stored_responses` |
| `conversations` | Responses | `conversation` 参数和 `/v1/conversations` |
| `files` | Responses、Messages | `/v1/files` 上传，生成和资源请求中的 `file_nx_` 引用 |
| `vector_stores` | Responses | `/v1/vector_stores` 和 `file_search` 工具 |
| `batches` | Messages | `/v1/messages/batches` |
| `code_execution` | Responses、Messages | `code_interpreter`、`/v1/containers`、Claude 代码执行与容器复用 |
| `remote_mcp` | Responses、Messages | Responses 的 `mcp` 工具，Messages 的 `mcp_servers` 和 `mcp_toolset` |
| `web_fetch` | Messages | `web_fetch` 工具 |
| `web_search` | Responses、Messages | 搜索工具；仍须配置 `web_search_ceiling` 和计价维度 |

catalog 按端点校验这些声明，publication 随快照发布。只要快照含 relay_profile，就要求节点确认 `relay.provider-features.v1`；每启用一项功能，再要求对应能力（`relay.stored-responses.v1`、`relay.background-responses.v1`、`relay.conversations.v1`、`relay.files.v1`、`relay.vector-stores.v1`、`relay.message-batches.v1`、`relay.code-execution.v1`、`relay.remote-mcp.v1`、`relay.web-fetch.v1`、`relay.web-search.v1`）。节点在选择部署时逐个候选检查，所有候选都因未启用而落选时，在预留前返回 HTTP 400 和 `feature_disabled`。资源接口的创建、写入和搜索同样需要该功能。对已有资源的读取、列举、删除和取消不受开关限制，便于企业在功能关闭后清理。

启用后，客户内容按供应商的保留策略存放在该部署的上游账户中；使用同一部署的企业共用这个账户。Nexus 只在 PostgreSQL 保存定位用的句柄、上游 ID 和归属，不保存内容。企业可随时通过对应的删除接口删除。Key 撤销、企业停用或归档后，Relay 在宽限期后删除这些资源的上游副本，见[上游资源清理](#上游资源清理)。远程 MCP 和 Web Fetch 只接受公网 https 主机，拒绝回环、私网、链路本地、CGNAT 等 IP 字面量及 `localhost`、`.internal`、`.local` 等主机名。

### 供应商 ID 的归属

共享上游账户会按 ID 解析任何租户的已存储项，也会解密任何租户的 reasoning 或 compaction 密文。Relay 因此在出口统一给 Responses 输出中的每个输出项 `id`、`item_id`、`approval_request_id` 和 `encrypted_content` 加上 Key 绑定的标签：ID 写成 `nxo_<标签>_<供应商 ID>`，密文写成 `nxo1.<标签>.<密文>`。标签是集群私钥派生密钥对“Key ID + 原值”计算的 HMAC，同一值在 SSE 各事件中保持一致，各节点都能校验。HTTP 与 SSE 生成、WebSocket 事件、已存储 response 的读取与续读、`input_items` 和会话条目都经过同一出口处理；`response.id` 和 Nexus 资源句柄不受影响。

入口在预留前校验并剥离标签，再把供应商原值发往上游。标签不属于当前 Key、未加标签的 `encrypted_content`、`item_reference`、只有 `id` 的项、带供应商 ID 的 reasoning 或 compaction 项，以及未加标签的 `approval_request_id`，都以 HTTP 400 `item_not_owned` 拒绝。其他完整内容项上未加标签的 `id` 只起命名作用，转发前删除。Nexus 转换路径自带的 `nxctx1.` 私有历史仍由原有密封校验。选择出口改写而不是逐项向上游查证，是因为无状态续接（`store=false` 回传带密文的 reasoning 项）没有可查证的父响应。升级前客户端保存的带供应商 ID 的 reasoning 项会被拒绝，需要重新开始会话。

### 租户隔离与限额

资源接口（文件、会话、向量库、容器、批处理、已存储 response）与生成一样占用节点并发，并额外受每企业节点并发上限约束。资源接口调用按企业现有的 Redis RPM 与并发策略准入，不占用 Token 额度。批处理在解析任何条目的资源前先完成这一准入，解析后再按逐项上界占用 TPM。请求体上传最长 60 秒，超时返回 408 `upload_timeout`。续读已存储 response 的流另占每企业流配额，并在流寿命到期时结束。创建文件、会话、向量库、容器和批处理前，节点核对该 Key 与所属企业在本集群的存量，超限时返回 429 `resource_limit_exceeded`。上限都由节点配置，见[开发计划](relay-development.md)。

## 上游资源清理

删除上游副本有三条路径：

1. **企业主动删除。** 通过各资源的 DELETE 接口删除，功能关闭后仍可调用。
2. **Key 失效。** Key 撤销或过期，或其企业、项目、服务账户不再处于 active，节点首次观察到时记录起点（有撤销或过期时间的以该时间为准）。超过 `NEXUS_RELAY_PURGE_GRACE_SECONDS` 宽限期后，该 Key 在本集群的已存储 response、文件、会话、向量库、容器和批处理进入 `publication.upstream_purges` 队列。宽限期内恢复可用的 Key 不会被清理。
3. **本地保存失败。** 上游已创建文件或会话、但本地句柄保存失败时，节点立即尽力删除上游副本；删除失败则把定位信息写入同一队列，原因为 `orphan`。

集群内任一节点每分钟领取到期任务（租期 10 分钟），用部署当前凭据调用供应商的删除接口。2xx 和 404 都视为已删除，所以重试幂等；随后把本地句柄标记为删除。向量库通过原有的存储观测记录删除，计费按现有路径结束。批处理仍在运行时先请求取消，下次再删除。Claude 代码执行容器没有删除接口，由供应商到期回收。失败按指数退避重试，最长每天一次，30 次后标记为 `abandoned`。每次结果都写入 `publication.audit_events`（`upstream_resource.purged`、`upstream_resource.purge_failed`、`upstream_resource.purge_abandoned`），队列行本身保留尝试次数和最后的错误代码。部署已删除或换了凭据时，任务以 `deployment_unavailable` 等待运维处理。

## 首期协议与计量细则

**文本与工具。** 两种入口都从公开模型名解析模型 ID，由部署决定出站模型名。原生转发保留已启用字段，转换只覆盖可证明等价的显式文本上下文和自定义函数工具。Messages 的 `tool_use.id` 与 `tool_result.tool_use_id`、Responses 的 `function_call.call_id` 与 `function_call_output.call_id` 在同一对话中保持对应关系；Responses 的输出项 ID 单独处理。参数增量按工具索引或项 ID 归集，完整后解析 JSON。重复或失配 ID、无等价停止条件、依赖厂商私有思考签名的跨协议历史，在预留前返回参数错误。Responses 默认使用显式输入历史及 `store=false`。只有部署启用 `stored_responses` 后才接受 `store=true`；这类原生请求以企业 Key 绑定 Nexus 句柄；读取、删除和继续生成时重新核对授权及原上游绑定。后台运行和托管工具有各自的生命周期与计量要求。[OpenAI Docs：创建 Responses](https://developers.openai.com/api/reference/resources/responses/methods/create) · [工具调用与输出项](https://developers.openai.com/api/docs/guides/migrate-to-responses)

**SSE 与交付。** Messages 跟踪消息和各内容块的开始、增量、结束，并识别流内错误；`message_delta` 的累计计数覆盖此前值。Responses 按事件类型、输出项和内容索引处理文本与函数参数，保留 completed、failed、incomplete 的区别。合法达到输出上限、已完成协议交付且有完整用量时，可按实际用量结算；未知原因的中断保留实际证据供核对。对上游新字段，原生路径可保留明确无关的可选字段；会影响计量、终态或工具语义的未知字段须进入显式处理。转换不得把 EOF 合成为成功结束。[Anthropic 流事件](https://platform.claude.com/docs/en/build-with-claude/streaming) · [OpenAI Docs：流式响应](https://developers.openai.com/api/docs/guides/streaming-responses)

**上界。** Messages 强制执行 `max_tokens`，Responses 强制执行 `max_output_tokens`；对可省略参数由部署能力给出显式上限。预留前按最终模型、工具定义、序列化规则与实际转换内容核算输入。可使用部署已验证的硬上界（包括模型接受的输入上限）保守预留；其代价是更高的资金和 TPM 占用。预估 token 数本身不足以证明上界。不能覆盖的模型版本或参数组合继续留在联调范围。

**多次模型调用。** 服务端工具循环和上下文压缩会在一个请求内多次调用模型，每次都重新读取上下文；供应商的 usage 是各次调用之和。部署的 `input_ceiling` 只约束单次调用，因此输入预留按“单次上界 × 可能的调用次数”计算，调用次数由最终上游请求决定：

| 上游请求 | 可能的调用次数 |
| --- | --- |
| 无服务端工具，或 Chat Completions、Bedrock Converse | 1；Responses `compact` 和 count tokens 也为 1 |
| Messages 服务端工具 | 只有 `web_search_20250305` 的 `max_uses` 能约束采样轮数：1 + 各工具 `max_uses` 之和。代码执行、MCP、带动态过滤的搜索和抓取没有次数参数。交互请求最多 10 轮，即 Anthropic 文档规定的服务端循环默认上限；Message Batches 的上限未公开，无法确定上界的批任务在预留前拒绝 |
| Responses 内置工具 | 1 + `max_tool_calls`，它限制整个响应的内置工具调用总数。请求未给出时，搜索沿用部署上限；其余内置工具由 Relay 写入 10 |
| 启用压缩（Messages `compact_*` 编辑或 Responses `context_management`） | 上述次数 × 2，每轮采样前最多一次压缩 |

调用次数超过 64 或输入上界超过 10 亿时，请求在预留前以 400 拒绝。`catalog.v1_eligible` 接受不超过模型输入上限 64 倍的输入上界，单次调用仍受部署 `input_ceiling` 约束。输出上界不变：`max_tokens`、`max_output_tokens` 按响应生效。[服务端循环上限](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons) · [Web Search `max_uses`](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool) · [Responses `max_tool_calls`](https://developers.openai.com/api/reference/resources/responses/methods/create) · [Claude 压缩](https://platform.claude.com/docs/en/build-with-claude/compaction)

### 分词器与输入上界

首期由 Nexus 实现用量采集与统计，文本编码复用与目标部署匹配的成熟分词器。本地计数用于估算，资金预留使用已验证的上界，最终扣费依据上游实际 usage。

1. **按实际部署计数。** TokenKit 对识别出的 OpenAI 模型选择 `o200k_base` 或 `cl100k_base`，对 Claude 则按模型族和文本特征估算。Nexus 由部署配置声明上游模型及版本、编码或估算规则版本，以及可用的上界依据。跨协议转换完成后，计数对象是最终上游请求，覆盖系统指令、显式历史、工具定义、参数与结果。公开模型别名或入口协议不足以确定编码。[文本计数](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/tokenkit/tokenizer.go) · [模型族估算](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/tokenkit/tokenkit.go) · [请求计数样本](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/relaykit/dto/token_count_meta_test.go)
2. **分别输出估计和已验证上界。** 给定编码下的文本 token 数只说明这段文本的编码结果。请求包装、工具及供应商处理仍可能改变整单用量。new-api 为限制 BPE 耗时会切分长片段，切分可能改变结果；Claude 家族估算也可能低估。预留和 TPM 使用部署已验证的硬上界；只有估计时，走经验证的部署上限，无法覆盖就拒绝该组合。界面估计、限额上界和最终 usage 分别传递；只有上游证据进入消费结算。[现有预留与计量规则](billing.md)
3. **限制计数成本。** 编码表按版本共享，计数任务使用有界并发，短片段缓存设置容量和对象大小限制。对长 CJK、组合字符、emoji、长代码和工具 JSON，分别测试普通输入与极端长片段。比较 Rust 与锁定版本基准编码器的结果，单独记录快捷分段的偏差、冷/热缓存耗时和峰值内存。超时或资源超限时沿用明确的准入策略，禁止以零计数继续。[预切分实现](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/tokenkit/pretokenize.go) · [编码一致性与输入边界测试](https://github.com/Herta-Network/new-api/blob/476886f40defd27432e41e79a8c0327c27bab03f/tokenkit/tokenkit_test.go)

### 结算证据

下表定义了 **Relay 到 Worker 的证据格式**。保留现有 `attempt.finished` 和 `request.closed` 信封。Relay 仅提取白名单整数，Worker 拥有归一化与舍入规则。

| usage_format | 采集与归一化 | 共享样本的预期 |
| --- | --- | --- |
| `anthropic.messages.v1` | `input_tokens` 为普通输入，缓存读取和创建分别采集；创建总量与 5 分钟、1 小时分项核对。输出取可信终态的累计值。 | 普通输入 80、缓存读 20、创建 5 分钟缓存 10、输出从 2 累计到 7，最终四个收费量为 80、20、10、7。 |
| `openai.responses.v1` | 从原始 usage 提取总输入、缓存命中和总输出；推理是输出子集。仅启用已映射的计价维度。另有缓存写入计数或其他收费项目的部署，须验证其语义和价格后才能开放。 | 在仅有普通输入、缓存读和输出的定价下，输入 100（缓存 25）、输出 20（推理 6），收费量为 75、25、20。 |

字段缺失、有冲突、计数越界或未知收费维度时，保留可用事实并进入核对。不得为了通过现有 Worker 校验而发送 `fixture.*` 或补零。若厂商契约对特定可选字段规定了零值语义，在该格式版本的样本中单独证明。两种协议互转时，费用取决于实际调用的上游。用量包继续满足 v1 的 64 KiB 限制、扁平 `*_tokens` 键和最多 16 个计数字段，上游原始业务响应留在转发路径。[Anthropic 缓存用量](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) · [OpenAI Docs：Responses usage](https://developers.openai.com/api/reference/resources/responses/methods/create) · [入库约束](../migrations/sql/0008_gateway_protocol/events.sql)

## 一次请求如何执行

1. **加载配置。** 节点以自己的数据库登录获取本集群完整快照，校验原始字节的 SHA-256、schema、必需能力及引用。构建只读索引后原子切换，调用 `ack_config`。每个在途请求保留自己的 revision。按需调用 `get_credential_envelope`，核对集群、凭据和版本后解密。
2. **授权并预留。** 从客户凭据计算摘要，在快照中找到 Key、组织、项目和模型策略。选定支持的协议转换，基于最终上游请求核对输入上界，并强制执行输出上限。按组织、项目、Key 获取 Redis 限额租约，再调用 `reserve_request`。只有事务成功才继续；明确拒绝时按限额契约释放，提交结果未知时使用同一请求 ID 与原参数查证。
3. **登记尝试。** 在发布的候选中按优先级和权重选部署，固定上游模型、协议和凭据。续租成功后，用同一请求 ID、新尝试 ID 和递增序号调用 `start_attempt`；收到提交确认后才允许发送上游。数据库事务只覆盖授权或事实写入，HTTP 期间归还数据库连接。
4. **转发与提取证据。** HTTP 转发复用连接池，原生 Responses WebSocket 复用本会话的专属上游连接。响应原样或按协议转换。流式响应按下游读取速度写出，同时在转换之前从上游原始数据提取用量。长流由独立计时器续租，避免暂停读取或下游背压使续租超期。设定请求大小、响应解压上限、单帧及未完成工具参数上限、首字节与空闲时限。禁止透明重发有副作用的 POST；每次允许的故障转移都走新的尝试，规则见[HTTP 与 SSE 故障转移](#http-与-sse-故障转移)。
5. **结束与恢复。** 每个已开始尝试提交一个 `attempt.finished`，每个请求提交一个 `request.closed`，列出实际开始的全部尝试及最终交付尝试。使用同一个业务 ID 和原始序列化事件重放提交结果未知的写入，保留 `occurred_at`。上报返回冲突时保留关联信息并进入核对。流的交付按下游 HTTP 正文或 WebSocket 事件的实际写出结果判定；结束任务需由节点监督至提交或排空截止，客户端断连不会直接丢弃上报任务。调用 `get_request_status` 核实状态，Redis 租约按确认用量结清；客户扣费和每次尝试成本由 Worker 分别处理。[事件与幂等规则](plane-protocol.md#用量事件)

| 故障窗口 | Relay 行为 |
| --- | --- |
| 预留成功，尚未开始尝试 | 客户取消或本地失败时调用 `cancel_unstarted_request`；提交结果未知先查证。 |
| 尝试已提交，上游可能收到请求 | 保留尝试 ID 和预留；无法证明上游未执行时记录未知结果，不重发。只有连接未建立或上游以拒绝状态答复、且尚未向客户写出任何字节时，才另建尝试换部署发送。 |
| 已交付流内容，随后断流或客户断开 | 停止交付并取消上游，有界收集可取得的最终用量；已输出内容的流不能切换部署继续拼接。返回或记录真实结束原因。 |
| 缺失用量、超时、租约失效 | 停止扩大执行；缺失用量传 `null`，保留证据交给 Worker 等待或核对。兼容层合成的 usage、Messages 的 `message_stop` 或 Responses 的终止事件本身均不足以证明实际用量。 |
| 节点退出或进程丢失 | 正常关闭先摘除就绪、停止接收新请求，再限时完成上报。在途请求由原节点身份处理。崩溃发生在收到用量与持久化之间时可能丢失证据，通过已有尝试和预留核对；验收需明确这个窗口。 |

**请求与尝试的结果映射。** 上报时使用 v1 既有枚举。下面的成功交付指协议结果由 Relay 正常写出；对端在网络中断前实际读到的最后一个字节可能无法确认。

| 情况 | 尝试完成事实 | 请求关闭与资金处理 |
| --- | --- | --- |
| 特定尝试已正常交付，usage 完整 | `succeeded` 和原始计数 | `succeeded`，指定真实交付尝试 ID。两类事件可乱序到达。 |
| 客户取消，交付尝试和实际用量可证实 | `cancelled` 或已成功的尝试及计数 | `client_cancelled`，引用对应尝试，按现有规则核算。无任何开始记录时直接走取消预留命令。 |
| 上游明确失败且未完成服务 | `failed`，保留能确认的成本用量 | 所有允许的尝试均失败时填 `upstream_failed` 和空交付 ID；客户零扣费，尝试成本独立核对。 |
| 可能已执行且结果未知，或缺少最终用量 | 结果未知用 `unknown`；原生结果已知则保留其状态，缺少用量时 `raw_usage=null` | 交付不明用 `unknown`；交付可证实但用量不全时保留交付事实。由 Worker 区分等待和核对。 |

### HTTP 与 SSE 故障转移

HTTP POST（含 SSE）在同一请求内最多执行 `NEXUS_RELAY_MAX_ATTEMPTS` 次尝试。是否重发只看两件事：上游能否被证明没有执行，以及客户端是否已经收到字节。收到任何 2xx 响应后都不再换部署。

| 情形 | 判定 | 处理 |
| --- | --- | --- |
| DNS、TCP 连接、TLS 握手失败，或连接建立前超时 | 请求未离开节点 | 可重试 |
| Bedrock 请求签名失败 | 请求未发出 | 可重试 |
| 厂商文档列明的准入拒绝 | 见下方“可证明未执行的拒绝类型”表 | 同时核对错误体或 AWS 服务错误头、确认无正用量后可切换。尝试记 `failed` |
| 厂商错误信封中的其他 429 | HTTP 429 表示请求被拒绝，且未向客户交付任何内容，但错误类型不在厂商文档内 | 不重发；尝试记 `failed`，客户零扣费并释放预留；没有用量的尝试单独进入成本核对 |
| 502，或缺少厂商拒绝证据的 503/529 | 可能来自中间层，上游可能已执行。Responses 的 503 `server_error` 也在此列 | 不重试，结果记 `unknown`，预留保留并进入核对 |
| 请求写出后超时或连接中断 | 可能已执行 | 不重试，记未知结果并进入核对 |
| 其他状态，包括 500、504 和 4xx | 可能已执行，或请求本身不被接受 | 不重试，沿用单次尝试的交付与上报 |
| 已收到 2xx，包括 SSE 尚未写出事件就空闲超时或断流 | 已开始执行 | 不重试 |
| 客户端已断开 | 没有交付对象 | 不再发起新尝试 |

可重试时，`request/` 使用同一快照修订和同一冻结上界重新选择：排除本请求已失败的部署，先在同一优先级组按剩余权重抽取，组内用完再降到下一组。候选须满足当前请求的协议、HTTP 传输、流式和工具条件，需要转换的跨协议候选必须转换成功。输入上界不得超过已预留值，出站输出上限固定为已预留的输出上界，与 `start_attempt` 中 `v1_eligible` 的检查一致。无候选、凭据解密或续租失败、达到次数上限，或首次尝试开始后已超过 `NEXUS_RELAY_FAILOVER_BUDGET_MS` 时停止重试，把最后一次失败交给客户端：拒绝状态的错误体原样或按入口协议转换后返回，连接类失败返回 502。新尝试登记及上一尝试上报完成后，节点在发送前再次校验剩余时限、客户端和租约。已登记但未发送的尝试也进入结束事实。

每次重发前先续租，再以递增序号调用 `start_attempt`，绑定新部署及其凭据版本；收到提交确认后才写出上游。被拒的尝试随后单独提交 `attempt.finished`，结果为 `failed`；错误体带计数时附原始用量，否则 `raw_usage=null`。最后一次尝试结束时提交唯一的 `request.closed`，列出全部已开始的尝试，交付尝试只能是最后一次。Worker 只按交付尝试向客户收费，各尝试按自己部署的冻结成本价独立处理；没有用量的失败尝试不补零，而是在该尝试上开 `missing_usage` 核对项，不影响客户结算和预留释放。新尝试的提交结果未知时先查请求状态：已存在就按未发送的失败尝试上报并列入关闭事件；无法确认时不提交关闭事件，留待预留到期后核对。Redis TPM 按全部尝试的已确认用量之和核销，任一尝试缺少可信用量时保留授权上界。

是否可证明未执行，与还能否重试无关。`NEXUS_RELAY_MAX_ATTEMPTS=1` 只关闭换部署重发，判定规则不变：连接未建立、Bedrock 签名失败、厂商文档列明的拒绝和上表中其他 429，都把这次尝试记为 `failed`，请求以 `upstream_failed` 关闭并释放预留；拒绝的错误体原样或按入口协议转换后返回，连接类失败返回 502。后台生成和 Code Interpreter 请求同样只有一次尝试，规则相同。只有 502、无厂商证据的 503/529、请求写出后的超时或断流记 `unknown`，预留保留待核对。WebSocket 轮次已绑定部署，不参与故障转移；熔断不在本期范围。

**可证明未执行的拒绝类型。** 只有下表的组合会触发换部署。表外的 429 按上表第二类处理。

| 上游 | 状态与错误类型 | 依据 |
| --- | --- | --- |
| Anthropic Messages | 429 `rate_limit_error`（含速率、档位月度消费上限、工作区消费上限）；529 `overloaded_error`；503 `overloaded_error`（与 SSE `error` 事件同型） | [Anthropic API 错误](https://platform.claude.com/docs/en/api/errors) · [流式错误事件](https://platform.claude.com/docs/en/build-with-claude/streaming) |
| OpenAI Responses 与 Chat Completions | 429 的 `error.code` 或 `error.type` 为 `rate_limit_exceeded`、`rate_limit_error`、`slow_down`、`insufficient_quota`、`credit_balance_exhausted`、`organization_spend_limit_exceeded`、`project_spend_limit_exceeded`、`organization_usage_limit_exceeded`；503 的 `service_unavailable_error`/`server_is_overloaded` | [OpenAI 错误码](https://developers.openai.com/api/docs/guides/error-codes) · [速率限制](https://developers.openai.com/api/docs/guides/rate-limits)。`rate_limit_exceeded` 是“Rate limit reached for requests”实际返回的码，文档该行未写码值 |
| Amazon Bedrock（Converse、ConverseStream、InvokeModel） | `x-amzn-ErrorType` 为 429 `ThrottlingException`、429 `ModelNotReadyException`、503 `ServiceUnavailableException` | [Converse 错误](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html) · [InvokeModel 错误](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_InvokeModel.html) |

Responses 的 503 `server_error` 与 502 一样有意保持不重试：它可能出现在上游已开始处理之后。其他 4xx 和 500 的错误信封沿用单次尝试的交付路径，同样记 `failed` 且不重试。

## 控制平面改动与上线顺序

以下是 P1 的控制平面与节点契约。现有 `gateway_v1` 八个函数保持调用签名；真实厂商规则随控制平面先行发布。

| 属主 | 当前能力 | P1 交付内容 |
| --- | --- | --- |
| catalog | 上游有 `protocol_family`、地址和加密凭据；公开模型有流式/工具标志和 Token 上限。 | 在部署上声明经过验证的上游端点、传输、支持的功能及上界规则的版本；Responses 单独标记原生 WebSocket 能力。端点取值限定 Messages 或 Responses。既有部署升级后保留原配置；端点未经声明的部署不进入真实转发候选。 |
| publication | 按集群发布不可变快照、凭据引用和 required_capabilities。 | 发布上述部署能力，并按集群检查节点是否理解新字段。现有模拟集群仍能使用 v1 资源，将双端 WS 流量仅发给确认相应能力的节点。旧节点不得确认带未知必需能力的发布。回滚使用更高修订号，重新校验实时权限与凭据。 |
| billing | v1 收件箱接受两个事件类型；Worker 兼容两种历史 fixture 格式与已绑定的原生格式。 | 新增上表两种版本化格式及样本。依据尝试的 deployment_id、原请求的集群和修订核验允许的格式；在开始尝试的 SQL 事务中，从已授权修订的部署描述取得期望格式，由 billing 持久化到尝试。Worker 仅查询本模块事实。旧尝试沿用其原始规则。 |
| Rust Relay | Python 模拟器已验证 SQL 与 Redis 契约。 | 在 Rust 实现受限 SQL 调用、sealed box 和 Redis 原子租约。跨语言测试覆盖键布局、策略排序、指纹字节编码、租约 owner、Redis TIME 与恢复窗口；过期租约不能释放接替者的占用。 |

**发布次序。** 先部署兼容旧记录的迁移、管理 API 和支持新格式的 Worker；再登记具备相应能力的 Relay，在单一测试集群启用新快照；最后按部署逐项启用收费组合。回滚时先停止新准入，限时排空在途请求和待提交事件，再发布更高修订号切回已验收配置。未核清的预留继续由原尝试的证据和冻结价格处理。[配置发布](../src/nexus/modules/publication/application/service.py) · [结算消费者](../src/nexus/modules/billing/application/usage.py)

**配置规模。** 节点启动或重连获取权威快照；日常监听 Redis 变更并错峰检查版本。定期从 PostgreSQL 复核授权版本，低成本的“当前修订和摘要”读操作如有需要单独版本化，当前 v1 返回完整快照。记录每节点连接数、配置读取次数与字节数；同步授权和异步用量沿用现有 SQL 与持久队列。[网关协议](plane-protocol.md) · [容量基线](control-plane-acceptance.md)

## 实施顺序与完成标准

按 P0–P5 推进。兼容性、收费启用条件与发布证据按部署和转换方向验收。任务分解、依赖和交付检查见[Relay 开发计划](relay-development.md)。
