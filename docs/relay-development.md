# Rust Relay 开发计划

目标是交付可独立部署的 `nexus-relay`，为企业提供 Anthropic Messages 和 OpenAI Responses 请求转发。首期含 Responses 原生双端 WebSocket，即客户端升级到 Relay，由 Relay 使用部署凭据拨上游 WSS。本计划于 2026-11-14 随首期设计定稿，各阶段按下表验收。协议覆盖、模块职责、计数口径、CC Switch 代码复用及 new-api 行为对照见[Relay 设计](relay-design.md)。以下任务按依赖推进，可独立验收的产物作为合并单位。

## 运行现有实现

从仓库根目录执行 `cargo build --release --locked --manifest-path src/relay/Cargo.toml`，生成 `src/relay/target/release/nexus-relay`。节点通过 HTTP/1.1 提供生成端点 `POST /v1/messages`、`POST /v1/responses` 和 `POST /v1/chat/completions`，压缩端点 `POST /v1/responses/compact`，以及 `GET /v1/responses` Upgrade。生成端点支持普通 JSON 和 `stream=true` 的 SSE。Upgrade 建立串行多轮会话，首轮准入后才拨部署的 WSS。对外 TLS 由入口负载均衡终止，`GET /live` 和 `GET /ready` 分别提供存活和就绪状态。Prometheus 指标在独立的指标监听上提供，日志为 stderr 上逐行 JSON，字段、指标清单和探针用法见[Relay 可观测性](operations.md#relay-可观测性)。

| 环境变量 | 节点配置 |
| --- | --- |
| `NEXUS_RELAY_DATABASE_URL` | 节点专属 PostgreSQL 登录的连接 URL，使用已登记的 gateway principal |
| `NEXUS_RELAY_REDIS_URL`、`NEXUS_RELAY_NAMESPACE` | 与控制平面相同的 Redis 和命名空间，命名空间默认 `nexus:v1` |
| `NEXUS_RELAY_CLUSTER_ID`、`NEXUS_RELAY_PRIVATE_KEY` | 已登记的集群 UUID 和对应的 base64 编码 32 字节 Curve25519 私钥 |
| `NEXUS_RELAY_LISTEN`、`NEXUS_RELAY_POOL_SIZE` | 默认监听 `127.0.0.1:8081`，每个节点默认最多 8 个数据库连接 |
| `NEXUS_RELAY_METRICS_LISTEN` | 指标监听，默认 `127.0.0.1:9464`，只提供 `GET /metrics` 和 `GET /live`，不承载企业流量也不鉴权。容器部署时绑定 Pod 内网地址，用网络策略只放行 Prometheus 和 kubelet，不要经由入口负载均衡暴露 |
| `NEXUS_RELAY_LOG` | 日志级别过滤，默认 `info`。以逗号分隔，单独的级别作为默认值，`target=level` 单独设置模块，例如 `info,nexus_relay::server=debug,sqlx=warn`。取值无效时节点拒绝启动 |
| `NEXUS_RELAY_REQUEST_BYTES`、`NEXUS_RELAY_RESPONSE_BYTES` | 默认请求上限 2 MiB，解压后非流式响应上限 8 MiB |
| `NEXUS_RELAY_CONCURRENCY`、`NEXUS_RELAY_LEASE_MS` | 默认最多 128 个并发任务，Redis 租期默认 15000 毫秒，范围 100 至 60000 毫秒 |
| `NEXUS_RELAY_EVENT_BYTES`、`NEXUS_RELAY_STREAM_IDLE_MS` | 每个 SSE 事件和 WS 消息默认上限 1 MiB，上游事件和下游写出空闲时限默认 30000 毫秒 |
| `NEXUS_RELAY_WS_SESSIONS`、`NEXUS_RELAY_WS_SESSIONS_PER_TENANT`、`NEXUS_RELAY_WS_TURNS_PER_TENANT` | 单节点默认最多 128 个会话，同一企业在该节点默认最多 16 个会话和 16 个活跃轮次。跨节点生成额度由 Redis 控制 |
| `NEXUS_RELAY_MAX_ATTEMPTS` | HTTP/SSE 生成每个请求最多尝试的次数，默认 2，范围 1 至 4；1 表示关闭故障转移 |
| `NEXUS_RELAY_FAILOVER_BUDGET_MS` | 首次尝试开始后仍可发起新尝试的时限，默认 10000 毫秒，范围 100 至 60000 毫秒；不截断已在执行的尝试 |
| `NEXUS_RELAY_UPSTREAM_CA` | 可选的 PEM 根证书路径，用于拥有专用 CA 的 HTTPS 部署 |

在 catalog 中为候选部署配置 `relay_profile`，声明原生端点、`http` 或 `websocket` 传输、工具支持、计数规则、输入输出上界及验证依据。节点使用已验证的部署输入上界保守预留。Responses 默认使用 `store=false`，省略输出限制时使用部署上限。每个候选的路径在其基础地址后追加 `/v1/messages` 或 `/v1/responses`，已有 `/v1` 的地址只追加端点名。旧部署保留原配置，声明端点后才参与原生转发。

流量按快照能力启用。基础节点登记 `reservation.v1`、`usage-events.v1`、`relay.native.v1`。HTTP 部署开启 `streaming` 时，新发布的快照另要求 `relay.sse.v1`，公开模型也需要开启流式能力。Responses 部署开启 `websocket` 时，还需 `relay.responses-ws.v1`。只有通过验证的部署才声明 `continuation` 和 `cancellation`。前者允许用本连接已完成的上一轮 ID 续接；后者允许转发原生取消事件。其余部署通过断开专属上游连接中止在途生成，保留未知结果供核对。

WS 握手使用企业 Bearer Key，空闲和 ping/pong 不触发资金预留。每次 create 重查 Key、部署、凭据和模型，按覆盖完整上下文的部署上界预留，并使用独立尝试。每个连接最多一个在途轮次，同一会话复用固定上游连接。SSE 与 WS 通过有界队列传递事件并独立续租。节点以终态事件的真实写出结果判定交付。客户端已断开、结果未知或缺失用量时，Worker 依据已有事实核对。

运行 `cargo test --locked --manifest-path src/relay/Cargo.toml` 验证协议、路由及写出跟踪。运行 `uv run python scripts/run_integration.py -q tests/integration/test_plane.py tests/integration/test_relay.py tests/integration/test_relay_streams.py` 验证真实客户端、本地 TLS 上游、隔离 PostgreSQL/Redis 和 Worker 的完整链路。

获授权真实部署可运行 `tests/integration/test_live_relay_ws.py`。设置 `RELAY_ACCEPTANCE_WS_RUN=1`、`RELAY_ACCEPTANCE_WS_URL`、`RELAY_ACCEPTANCE_WS_MODEL` 和 `RELAY_ACCEPTANCE_WS_KEY`，使用上述隔离集成命令并指定该测试文件。测试只在一个连接串行发送两次生成，`RELAY_ACCEPTANCE_WS_OUTPUT` 默认每轮 64。商户 Key 写入本次隔离 catalog。给出 `wss://` 时 Relay 直接拨 TLS，给出回环 `ws://` 时测试搭建临时 TLS 入口，保留原始握手、帧和端点的升级状态。测试余额和真实上游计费分别核对。[Responses 续接语义](https://developers.openai.com/api/docs/guides/websocket-mode)

真实 AWS 验收使用 `tests/integration/test_live_bedrock.py`。设置 `RELAY_ACCEPTANCE_BEDROCK_RUN=1`、`RELAY_ACCEPTANCE_BEDROCK_REGION`、`RELAY_ACCEPTANCE_BEDROCK_MODEL` 和 `RELAY_ACCEPTANCE_BEDROCK_KEY`。Key 使用 Bedrock API Key 或目录支持的 SigV4 凭据 JSON。该测试向对应区域的 Runtime 发送一次 Messages JSON 和一次 Responses SSE，每次最多 64 个输出 token，并检查隔离账本中的两笔扣费和采购成本。默认使用 Converse。验证 Claude Invoke 时，设置 `RELAY_ACCEPTANCE_BEDROCK_MODE=claude_invoke`。 同时设置 `RELAY_ACCEPTANCE_BEDROCK_CITATIONS=1` 会追加两次 Messages 生成，分别校验文档引用、带原引用历史的 SSE 续接、来源字符区间和计费。

## 模型发现、计数和 Chat 客户端

- `GET /v1/models` 使用企业 Bearer Key，返回该 Key 在当前集群实际有权使用的模型别名与创建时间。节点同时检查已发布范围、当前 Key、项目、策略和部署状态。
- `POST /v1/messages/count_tokens` 使用企业 `x-api-key`，提交模型和 Messages 输入，返回上游计算的 `input_tokens`。原生 Messages 使用供应商计数端点，Bedrock 使用 AWS CountTokens。计数占用请求频率与并发额度，不产生生成请求的资金预留或扣费。上游可能按模型拒绝此操作。
- 原生 Messages 请求接受 `anthropic-version` 和 `anthropic-beta`，Relay 仍使用部署凭据鉴权。带有 beta 或非默认版本的请求只选原生 Anthropic 部署。未指定版本时使用 `2023-06-01`。
- `POST /v1/chat/completions` 支持单个输出、自定义函数、显式多轮历史、普通 JSON 与 Chat SSE。流以 `data: [DONE]` 结束，`stream_options.include_usage` 控制最后的用量块。按真实上游的 Responses、Messages 或 Bedrock usage 结算。`stop` 接受单个字符串或最多四个停止串，选择支持停止序列的 Messages 或 Bedrock 上游，并将匹配终态返回为 `finish_reason: "stop"`。仅有原生 Responses 部署时，默认会在预留前拒绝显式停止序列；客户端也可逐请求授权本地匹配。
- Chat 客户端可通过 `web_search_options` 在已定价的标准 Responses Web Search 部署上搜索。支持 `search_context_size` 和 `user_location.approximate`。搜索次数受部署的 `web_search_ceiling` 限制，按完成的搜索动作结算。查询选项适用于单独的搜索请求，与自定义函数分开发送。
- URL 引用位于 `choices[0].message.annotations`。流式 Chat 在结束前通过 Nexus 扩展 `choices[0].delta.annotations` 交付完整引用数组。引用位置以拼接后的回答正文为准；客户端可将带 `annotations` 的助手消息放回下一轮历史。[OpenAI 搜索输出](https://developers.openai.com/api/docs/guides/tools-web-search)
- 在原生 Responses HTTP、SSE 或 WebSocket 生成中提交 `store:true`，返回的响应 ID 为 Nexus 的 `resp_nx_…` 句柄。相同企业 Key 可读取或删除 `/v1/responses/{handle}`，或在后续生成、compact 及重新建立的 WebSocket 中传 `previous_response_id`。节点将句柄绑定原部署、凭据版本和上游账户，并再次检查当前权限。储存内容由上游持有，本地只保存定位元数据。普通无状态请求继续返回原先的 ID；这些 ID 不能用于 HTTP 状态续接。

### Responses 客户端使用 Chat Completions 上游

对于只提供 `POST /v1/chat/completions` 的供应商，创建协议族为 `openai_compatible` 的上游，并在部署的 `relay_profile` 中设置 `endpoint: "openai_chat_completions"`、`transports: ["http"]`。沿用该部署已验证的输入、输出上界和计数规则。`convert_from` 可省略。流式和函数工具分别要求模型和部署均开启 `streaming`、`tools`。应用迁移 `0030_chat_upstream`，升级 Worker 和 Relay，为集群节点登记 `relay.responses-to-chat.v1`，然后发布修订。

客户端继续调用 `POST /v1/responses`。普通请求返回 Responses 对象，`stream: true` 返回带序号的 Responses SSE 事件。支持字符串输入、显式消息历史、instructions、函数调用和结果，以及文本或 JSON 结构化输出。工具参数增量按调用索引重组。仅支持 Chat 能表达的请求；存储与后台生成、托管工具、WebSocket 和独立压缩仍需要支持它们的原生部署。

Chat 流会请求 `stream_options.include_usage`，并在收到结束原因和 `[DONE]` 后发出 Responses 终态。`length` 和 `content_filter` 返回 `response.incomplete`。计费依据绑定到该尝试的 `openai.chat.v1` 回执，包括上游报告的输入、输出、缓存命中和推理 token。缺失回执或计数冲突进入核对。Responses 客户端的用量对象中，Chat 省略的明细显示为零；账本保留原始回执中明细缺失的事实。

### OpenAI Conversations

通过 `POST /v1/conversations` 创建会话，可用 `X-Nexus-Model` 选择原生 Responses 部署。同一企业 Key 可调用 `GET`、`POST`、`DELETE /v1/conversations/{id}` 读取、更新元数据或删除会话。使用 `/items` 追加和分页读取显式项目，使用 `/items/{item_id}` 读取或删除单项。消息内容由供应商保存，Nexus 保存 Key、模型、部署、凭据版本及会话句柄的对应关系。

在 Responses 的 `conversation` 字段传 `conv_nx_…` 或 `{"id":"conv_nx_…"}` 可继续生成，生成按次预留和结算。含会话的响应和 SSE 事件返回同一个 Nexus 会话句柄。会话和 `previous_response_id` 各自指定历史来源，一次生成选其一。[OpenAI Conversations](https://developers.openai.com/api/reference/resources/conversations/methods/create)

### 远程 MCP 和 Web Fetch

原生 Responses 的 `tools` 可包含 `type: "mcp"`、`server_label` 和 HTTPS `server_url`。审批策略与调用方提供的授权参数随请求交给上游。需要审批时，保存返回的响应，再用 Nexus 的 `previous_response_id` 和 `mcp_approval_response` 继续。Relay 核验父响应确实包含该审批请求。相同机制支持引用父响应输出中的 `item_reference`。[OpenAI 远程 MCP](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)

原生 Messages 支持由调用方提供的 HTTPS `mcp_servers` 及其 `mcp_toolset`，也支持使用 `web_fetch_20250910` 工具。按供应商要求传递 `anthropic-beta`。工具入参和授权值留在生成请求中；工具调用由供应商执行，按回执里的模型 token 用量结算。Web Search 使用独立的次数计价维度。[Anthropic MCP](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector) · [Web Fetch 计费](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool)

### Web Search

在公开模型的 `tool_dimensions` 添加 `web_search_requests`，为模型和部署分别发布该维度的销售价、采购价。两者均支持按一次或每千次报价。部署须启用 `tools` 并设置 `relay_profile.web_search_ceiling`；节点须登记 `relay.web-search.v1`。原生 Responses 默认使用标准搜索。供应商对 preview 有独立计价时，使用定价对应的部署并将 `web_search_variant` 设为 `preview`。

Messages 使用工具定义中的 `max_uses`，Responses 使用请求级 `max_tool_calls`。省略时 Relay 写入部署上限；显式上限超过部署能力时会在预留前拒绝。上游返回后，Messages 按 `usage.server_tool_use.web_search_requests` 结算，Responses 按终态输出中已完成的 `web_search_call` 的 `search` 动作结算。同一动作可以包含多个查询。搜索内容的 token 按供应商回执和模型定价处理；涉及固定检索 token 费用的模型，配置采购价时须核对供应商账单与回执覆盖范围。[Anthropic Web Search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool) · [OpenAI Web Search](https://developers.openai.com/api/docs/guides/tools-web-search) · [OpenAI 工具定价](https://developers.openai.com/api/docs/pricing)

搜索费与 token 费分别预留和结算。支持的动态过滤组合按供应商免执行附加费规则处理，未知费用维度会进入核对。Anthropic 批处理的 token 享受批量折扣，搜索次数按常规单价，客户组织折扣同时适用。每次搜索后模型都会重新读取上下文，输入预留按 1 + 搜索上限次调用计算，规则见[多次模型调用](relay-design.md#首期协议与计量细则)。用量缺失或含无法核实的搜索动作时进入核对。

### File Search 和向量存储

在模型的 `tool_dimensions` 添加 `file_search_calls` 与 `vector_storage_byte_seconds`，为模型和部署发布包含这两个维度的销售价和采购价。开启原生 Responses HTTP 部署的 `tools`，填写 `file_search_ceiling` 并登记节点能力 `relay.file-search.v1`。检索调用可按次或每千次报价。存储的 `unit_quantity` 为 `92771293593600`，对应 1 GiB 使用 1 日的字节秒数，`unit_price` 填每 GiB·日的价格。[供应商工具定价](https://developers.openai.com/api/docs/pricing)

1. 用企业 Bearer Key 上传文件到 `POST /v1/files`。在 `POST /v1/vector_stores` 的 `file_ids` 使用返回的文件句柄，保留返回的 `vs_nx_…`。创建空存储时可以通过 `X-Nexus-Model` 选择部署。
2. 同一 Key 可以列举、读取、更新、删除自己的存储，也可在 `/v1/vector_stores/{id}/files` 管理文件、读取解析内容。在 `/file_batches` 批量添加文件后，用返回的批句柄查询、取消或列举该批文件。直接调用 `/v1/vector_stores/{id}/search` 可查询已索引内容。[OpenAI Retrieval](https://developers.openai.com/api/docs/guides/retrieval)
3. 在 `POST /v1/responses` 或 Responses WebSocket 中发送 `tools: [{"type":"file_search","vector_store_ids":["vs_nx_…"]}]`。使用 `include: ["file_search_call.results"]` 可以查看匹配项。结果和引用中的文件 ID 可继续用于当前 Key 的 Files API。一次已完成的 `file_search_call` 对应一次调用，即使它包含多个查询。混用 Web Search 和 File Search 时，两者共享实际下发的 `max_tool_calls`，预留按价格较高的组合计算。[OpenAI File Search](https://developers.openai.com/api/docs/guides/tools-file-search)

节点约每分钟读取供应商报告的 `usage_bytes`，文件变更后会安排读取。企业存储费按上一次成功观测的字节数乘以持续秒数计算，在 UTC 日期交界或存储终止时分单。相邻、字节数相同且观测连续的时段合并保留，可按时段重算每日总量。已知到期时间用于截断计量。用量按创建时冻结的销售价、组织折扣和部署采购价计算。供应商账户共享的免费额度留给采购对账。连续观测相隔超过五分钟、轮询失败或资源意外消失，涉及时段转入核对。余额或预算不足时保留待结算记录，后续补足后可继续处理。

管理端可用 `GET /api/v1/organizations/{organization_id}/vector-storage-usage` 查询存储句柄、UTC 用量日期、观测字节秒数、核对原因和关联扣费请求。该接口支持项目、Key、模型和时间过滤；`posted_at` 视图只列已有入账时间的记录。

### 代码执行和生成文件

**Anthropic Messages。** 在原生 Messages 部署启用 `tools` 和 `code_execution`。请求可声明 `code_execution_20250522`、`code_execution_20250825`、`code_execution_20260120` 或 `code_execution_20260521`。使用旧版 beta 时按供应商要求发送 `anthropic-beta`。下一次 Messages 请求可传回响应中的 `cntr_nx_…`。上传到 Files API 的文件可放入 `container_upload.file_id`。工具结果中的生成文件也返回本 Key 的 `file_nx_…`，可用 Files API 查询、下载或删除。批任务的 JSONL 结果采用相同的归属校验。[Claude Code Execution](https://platform.claude.com/docs/en/agents-and-tools/tool-use/code-execution-tool)

独立执行需要在模型的 `tool_dimensions` 配置 `code_execution_seconds`，并发布销售价和采购价，可按秒或按小时报价。`execution_reserve_seconds` 指定每个请求的资金预留，至少覆盖 300 秒。发生执行或预加载文件而回执缺少计费时长时，账本保留预留。财务人员用供应商的唯一明细引用和实际计费秒数补账，采购和销售使用同一份证据，已确认的 token 计数须匹配。供应商账户的免费小时数通过采购对账处理。与 `web_search_20260209`、`web_fetch_20260209` 或已支持的更新版本组合时，执行附加费为零，token 和实际搜索次数继续按各自单价结算。[Claude 执行计费](https://platform.claude.com/docs/en/agents-and-tools/tool-use/code-execution-tool#usage-and-pricing)

**OpenAI Code Interpreter。** 在原生 Responses 部署启用 `tools`，按供应商费率选择 `container_tariff` 和 `container_reserve_minutes`。固定二十分钟档使用 `session_20m`，按分钟且五分钟起计的部署使用 `minute_min_5`。为允许的内存档配置 `container_1g_minutes`、`container_4g_minutes`、`container_16g_minutes` 或 `container_64g_minutes`，可按 1 或 20 分钟报价。预留至少覆盖所选费率的起计窗口。[OpenAI 容器定价](https://developers.openai.com/api/docs/pricing)

用企业 Bearer Key 调用 `POST /v1/containers` 可创建禁网容器，使用 `file_ids` 装入本 Key 的 OpenAI 文件。Responses 的 `code_interpreter` 工具可引用返回的句柄，也可使用 `{"type":"auto","memory_limit":"1g"}` 让供应商分配或复用容器。容器支持列表、读取和删除。`/v1/containers/{id}/files` 支持二进制上传或 JSON 文件引用。生成内容的 `container_file_citation` 包含本 Key 的容器和文件句柄，可据此下载。容器内文件随容器到期失效。网络策略目前支持 `disabled`。[OpenAI Code Interpreter](https://developers.openai.com/api/docs/guides/tools-code-interpreter)

容器在独立的 `openai_container_v1` 账目预留资金，生成请求单独按 token 结算。供应商从创建起计费，直到容器被删除或空闲到期（默认 20 分钟）。每次操作（创建、读取、文件操作或 Code Interpreter 调用）都可能把容器寿命延长到“该操作的截止时间 + 空闲到期”：生成请求的截止时间是其预留到期时间，直接调用容器接口按 15 分钟计。

- **先预留再执行。** 会话的资金是一串不可变的预留：创建时的首个预留覆盖 `container_reserve_minutes`，且不少于新容器在上述截止时间内可能计费的分钟数；之后每次操作前，若已预留分钟数不足以覆盖新的可能结束时间，就追加一个扩展预留，至少 `container_reserve_minutes`，`session_20m` 按 20 分钟取整。追加因余额、预算或账户状态失败时拒绝这次复用（返回 402 或 403），客户可新建容器。
- **回执后补足。** 回执给出的空闲到期比假设更长，或自动模式下供应商复用了覆盖不足的旧容器时，操作已经执行，无法拒绝：Relay 把本次请求的临时预留转为旧会话的扩展预留，仍不足时再追加；追加失败则冻结钱包并写入 `container.coverage_unreserved` 审计事件，直到财务处理该会话。旧会话覆盖充足时，临时预留照常取消。
- **复核后不再接受操作。** 会话进入复核（空闲到期或确认删除）后，其事件已关闭，再引用该句柄返回 404，需新建容器。
- **节点崩溃或请求中断。** 预留在调用供应商之前提交，崩溃不会留下未预留的用量；已登记但未执行的操作仍按截止时间计入可能结束时间，多出的扩展预留在复核时由财务确认零收费。

供应商确认新建容器和内存档、执行成功，且在首个起计窗口内确认删除时，可以结算一个起计窗口，扩展预留全部释放。其余会话在关闭或到达复核时间后核对供应商计费分钟数：首个预留和每个扩展预留各是一个核对请求，起点晚于确认删除时间或最晚可能结束时间的扩展预留自动释放。财务按时间顺序把供应商分钟数分配到各段：每段不超过其预留分钟数，`session_20m` 的每段为 20 的倍数，`minute_min_5` 的五分钟起计只适用于首段；每段使用各自的证据引用（例如在供应商明细引用后加段序号）。读取容器本身会刷新空闲时间，Relay 仅因客户操作读取容器。财务人员可用 `container_origin`、`container_handle`、`container_unit` 和 `container_tariff` 对应账单明细，扩展预留另见 `billing.container_extensions`。

### 后台生成

原生 Responses 支持 `background:true` 的 JSON 和 SSE 提交。后台请求使用上游持久化并返回 Nexus 响应句柄。以 `stream:true` 创建时保存每个事件的 `sequence_number`，断线后使用同一 Key 调用 `GET /v1/responses/{handle}?stream=true&starting_after={sequence_number}` 续读。续读沿用原生成的计费记录。[OpenAI 后台响应](https://developers.openai.com/api/docs/guides/background)可用 `GET /v1/responses/{handle}` 轮询，或 `POST /v1/responses/{handle}/cancel` 取消。处理中的请求暂时禁止删除。接单节点将任务元数据持久化，以同一节点身份重启会继续轮询，并根据上游的最终用量结算或进入核对。收到 queued/in_progress 应答不产生最终扣费。上游账单和能力随部署而异。

### OpenAI 文件

用企业 Bearer Key 向 `POST /v1/files` 提交 multipart 文件和 `purpose`。可用 `X-Nexus-Model` 选择原生 Responses 部署。列表支持 `purpose` 过滤以及 `order`、`after`、`limit` 分页；可按返回的 `file_nx_…` 句柄读取元数据、下载内容或删除。文件到期后句柄失效。上传和下载沿用节点配置的大小限制。

在 Responses 中可将句柄用于 `input_file.file_id`、`input_image.file_id` 或函数工具结果中的对应内容块；Chat 的 `file.file_id` 也可使用同一个句柄。Relay 先校验文件与企业 Key、模型、部署及供应商凭据的绑定，再向该上游传入实际文件 ID。Conversations 支持创建和追加含文件的消息；读取会话项目或 `GET /v1/responses/{handle}/input_items` 时会返回所属 Key 的文件句柄。上传和读取本身不触发生成结算。[OpenAI Files](https://developers.openai.com/api/reference/resources/files/methods/create)

### Anthropic 文件

使用企业 `x-api-key` 向 `POST /v1/files` 提交 multipart 文件。可以通过 `X-Nexus-Model` 选择有权限的原生 Anthropic 部署；随后用同一 Key 调用 `GET /v1/files`、`GET /v1/files/{id}`、`GET /v1/files/{id}/content` 或 `DELETE /v1/files/{id}`。上传返回的 Nexus 文件句柄可以放入 Messages 输入的 `source.file_id`，生成和计数会核验归属并绑定原部署。数据库保存允许返回的文件元数据和上游定位，不保存文件字节。供应商决定文件是否允许下载。上传和下载沿用 Relay 配置的请求、响应大小上限。

### Anthropic Message Batches

向 `POST /v1/messages/batches` 提交 `requests`，每项带唯一的 `custom_id` 和非流式 Messages 参数。同一批可使用多个模型，须通过同一个原生 Anthropic 上游账户提交，各项分别使用选中部署的采购价和模型售价。请求体沿用节点大小上限，各项合计占用 RPM、TPM，一次批提交占用一个并发名额。所有项目在同一数据库事务中获得资金预留后，Relay 才向上游提交。批任务的服务端循环上限未公开，含代码执行、MCP、带动态过滤的搜索或抓取等无次数参数工具的项目无法确定输入上界，整批在预留前以 400 拒绝；`web_search_20250305` 按 `max_uses` 预留。

用创建批任务的企业 Key 调用 `GET /v1/messages/batches` 分页，或用返回的句柄查询、取消及读取 JSONL 结果。批状态的 `results_url` 指向当前 Relay 的相对路径。Worker 按 `custom_id` 对应结果，成功项的 token 采购成本和客户售价按冻结单价的 50% 计算，客户折扣继续生效。终态为 canceled、expired 或 errored 的项目按零结算。数据库只保存批元数据、每项标识与计费事实。接单节点重启后会继续轮询。提交结果未知时转入核对；完成结果核对后可删除批资源。[Anthropic Message Batches](https://platform.claude.com/docs/en/build-with-claude/batch-processing)

## OpenAI 上下文压缩

调用 `POST /v1/responses/compact`，使用企业 Bearer Key，提交公开模型名和显式历史。例如：

```json
{"model":"enterprise-model","input":[{"role":"user","content":"保留本次设计决策"}]}
```

Relay 只选择该组织路由中支持原生 Responses HTTP 的部署，向其 `/v1/responses/compact` 发起非流式 POST。成功响应是 `response.compaction`。下一轮调用 `POST /v1/responses` 时，从返回的完整 `output` 数组继续追加新消息，保留加密压缩项和其他保留项的顺序。也可以使用显式 `input` 搭配同一 Key 所有的 Nexus 响应句柄；供应商原始 ID 和跨 Key 的历史引用不予转发。[OpenAI 接口](https://developers.openai.com/api/reference/resources/responses/methods/compact) · [上下文压缩指南](https://developers.openai.com/api/docs/guides/compaction)

**计费。** 每次压缩独立预留、记录上游尝试并结算，使用该模型的销售价和实际部署的成本价，计量格式为 `openai.responses.v1`。部署的 `input_ceiling`、`output_ceiling` 应覆盖上游压缩操作本身的硬上界。压缩请求无法在上游强制指定输出上限，继而重试的部署也必须完全落在首次预留的输入、输出上界内。回执缺少可核对用量或结果不明时进入现有核对流程。可凭 `X-Request-ID` 查询本次压缩的请求与费用。

### Claude Messages 压缩

支持该功能的原生 Claude 部署可在 `POST /v1/messages` 中接受 `"compaction":{"type":"summarize"}` 和 `anthropic-beta: compact-2026-09-04`。收到 `stop_reason: "compaction"` 后，把返回的助手 `compaction` 块（包括签名）放在后续历史开头，并继续发送同一 beta 头。阈值压缩使用供应商规定的 `context_management` 请求和相应 beta 头。[Claude 按需压缩](https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand) · [Claude 阈值压缩](https://platform.claude.com/docs/en/build-with-claude/compaction-threshold)

Relay 分别核对 `usage.iterations` 中的 `message` 与 `compaction`。顶层计数对应普通生成；各轮的输入、输出、缓存读及分档写入合计进入配额与账本。回执中的 `thinking_tokens` 包含在输出总量内。上一轮的压缩块随历史重放时，只有本次供应商新报告的迭代会计费。JSON 与 SSE 使用相同规则。压缩结果缺少完整用量、模型或计数相互冲突时进入核对；累计用量超过冻结的请求上界时沿用超额核对流程。

真实 Claude 验收入口为 `tests/integration/test_live_claude_compaction.py`。为支持该 beta 的 HTTPS 测试部署设置 `RELAY_ACCEPTANCE_CLAUDE_COMPACTION_RUN=1` 和同前缀的 `URL`、`MODEL`、`KEY`。用例执行两次生成，输出上界默认每次 4096，可用 `OUTPUT` 调整。第一笔必须收到签名压缩块和压缩迭代用量，第二笔只携带该块和新问题，核验 SSE 回答、两笔销售扣费和采购成本。

## HTTP 和 SSE 故障转移

一次尝试被证明没有执行，且客户端尚未收到任何字节时，节点在同一请求内换部署重发。可证明未执行指连接未建立（DNS、TCP、TLS 或连接超时），或状态与厂商文档列明的限流、配额或过载错误匹配且无正用量。厂商错误信封中的其他 429 不重发，但尝试记 `failed` 并释放客户预留。普通 502、无厂商证据的 503/529 进入核对。请求写出后的超时、2xx 之后的断流和其他状态都不重发。完整判定表和上报规则见[故障转移设计](relay-design.md#http-与-sse-故障转移)。

在路由策略中为同一模型配置多个候选并设置 priority 和 weight。故障转移只在请求授权时的快照内挑选，先用完同组剩余权重再降组；候选的输入上界须不超过首次预留，输出上限须能容纳已预留的输出。每个候选都需要已发布的成本价，并在网关集群的部署范围内。每次重发都提交新的尝试，Worker 只对最终交付的尝试向客户收费；没有用量的失败尝试单独进入成本核对。

运行 `cargo test --locked --manifest-path src/relay/Cargo.toml --test failover` 验证候选选择与错误分类。运行 `uv run python scripts/run_integration.py -q tests/integration/test_relay_failover.py`，用两个本地 TLS 上游验证 503 与拒绝连接后的切换、SSE 切换、发送后读超时、SSE 输出后断流、全部候选失败及 `NEXUS_RELAY_MAX_ATTEMPTS=1` 的原有行为，并核对尝试记录、单笔扣费、独立成本和预留释放。用例包含一次 30 秒的上游响应头等待。

## Bedrock Converse 上游

Messages 和 Responses 的 HTTP 请求通过 Bedrock `Converse` 转发，`stream=true` 通过 `ConverseStream` 转成客户端原有 SSE 格式。适配范围包括显式文本对话、系统指令、函数工具与结果、采样参数和输出上限。Responses 的原生 WebSocket 轮次仍要求 Responses WSS 部署。Converse 支持内联图片、文档和视频，也支持 Claude 的 thinking 配置及其签名内容。其他不能映射的模型参数仍在预留前拒绝。[配置步骤](catalog-api.md#amazon-bedrock-部署) · [Converse](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html) · [ConverseStream](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ConverseStream.html)

节点按 AWS EventStream 的帧长度、校验和、事件类型和内容块索引解码。`messageStop` 记录停止原因，收到后续 `metadata` 才提交终态。缺失总量、已计量维度相互矛盾、流被截断或缓存写入无法对应冻结价格时，Worker 保留证据并进入核对。客户端可见的 usage 与结算证据分别生成，尝试固定使用 `aws.bedrock.converse.v1`。部署使用的计数规则要求普通输入、缓存读取、缓存写入及输出之和等于 `totalTokens`；缓存写入按 `cacheDetails` 的 5m、1h 分项核对。生产部署应使用实际模型的计量样本确认这些维度与价格。[AWS TokenUsage](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_TokenUsage.html)

运行 `uv run python scripts/run_integration.py -q tests/integration/test_bedrock_relay.py` 可验证两种入口、两种鉴权、普通及流式响应、异常流和 Worker 账本。该用例使用隔离 PostgreSQL、Redis 及本地 HTTPS 上游。

配置 `relay_profile.bedrock_invoke=true` 并登记 `relay.bedrock-claude-invoke.v1` 可将 AWS Claude 部署切换到模型专用 `InvokeModel` / `InvokeModelWithResponseStream`，请求包含 AWS 的 `anthropic_version`，响应使用 `anthropic.messages.v1` 计量。Converse 和 Invoke 均保留现有区域 SigV4 或 Bedrock API Key 认证。通过 Converse 发送视频时，Responses 使用 `input_file.file_data`（例如 `data:video/mp4;base64,…`），Chat 使用 `file.file_data`，Messages 使用 Nexus 扩展块 `{"type":"video","source":{"type":"base64","media_type":"video/mp4","data":"…"}}`。节点的请求大小上限同样适用。PDF、CSV、Office、HTML、Markdown 和纯文本文件按 MIME 映射为 AWS 文档格式，文件名中的句点等字符转为空格。请按部署模型的能力选择媒体类型。[AWS VideoBlock](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_VideoBlock.html) · [AWS DocumentBlock](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_DocumentBlock.html)

在支持助手预填的 Claude 模型上，Messages 的末尾助手文本可原样用于 Converse 续写。Messages 指定 `stop_sequences` 时，Relay 请求 AWS 返回匹配的停止串。普通响应和 SSE 终态保留供应商提供的 `stop_sequence`；模型省略该字段时返回 `null`。Converse 内容过滤、Guardrail 拦截和 Claude 的拒答可转换为 Responses 的 `incomplete_details.reason=content_filter`。Responses 的拒答文本及过滤终态可转换回 Messages 的文本块与 `stop_reason=refusal`。

Messages 向 Converse 提交文档或 `search_result` 时可启用 `citations.enabled`。支持文本、内联文档、自定义文本块，以及用户消息或函数工具结果中的检索资料。普通响应在相应文本块的 `citations` 返回来源索引、引文和范围；SSE 在文本块结束前生成 `citations_delta`。将助手内容原样放回后续 Messages 请求可保留这些引用。来源字符、页码、内容块和检索结果使用各自的位置字段。Claude 托管 Web Search 的 `encrypted_index` 属于其原生服务。ConverseStream 会把供应商分片发送的签名合为一次 `signature_delta`。脱敏思考块按二进制拼接密文，客户端可将得到的 `redacted_thinking.data` 用于下一轮。[AWS 引用结构](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_CitationsContentBlock.html) · [Claude 引用坐标](https://platform.claude.com/docs/en/build-with-claude/citations)

### Guardrail 用量与费用

在模型的 `tool_dimensions` 登记该 Guardrail 会收费的策略维度，为模型和部署分别发布销售、采购单价。每个单价支持按 1 单位或 1,000 单位报价。Converse 部署的 `bedrock_guardrail_unit_ceiling` 指定一次生成中每个收费维度可自动结算的最大数量。应用迁移 `0029_guardrail_units`，升级 Worker 和 Relay，为节点登记 `relay.bedrock-guardrail-usage.v1` 后发布快照。例如，模型登记 `guardrail_content_units` 和 `guardrail_image_units`，上界为 4，钱包及各层预算会在生成前同时预留最多 4 个文本过滤单位和 4 个图片过滤单位，再加 token 的原有预留。

| 计价维度 | AWS 回执来源 |
| --- | --- |
| `guardrail_content_units`、`guardrail_image_units` | 内容策略处理的文本单位、图片单位 |
| `guardrail_topic_units`、`guardrail_sensitive_units` | 拒绝主题和敏感信息策略的收费单位 |
| `guardrail_grounding_units` | 上下文依据检查处理的文本单位 |
| `guardrail_reasoning_units` | 单个自动推理策略的文本单位，按各次评估累加 |

要让供应商返回计量依据，在部署默认或已发布的请求选项中将 Guardrail 的 `trace` 设置为 `enabled`。Relay 从响应或流终态的 `trace.guardrail` 提取各次输入、输出评估的 `invocationMetrics.usage`。结算证据只保存计数。词表和正则敏感信息的免费单位单独留作核对。上下文费用与 token 独立计价，销售应用已冻结的组织折扣，采购应用该尝试的部署价格。[AWS Guardrail 指标](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_GuardrailUsage.html) · [AWS Guardrails 定价](https://aws.amazon.com/bedrock/pricing/#Amazon_Bedrock_Guardrails)

AWS 没有返回完整评估、出现未知计数、实际使用未定价的策略或一个评估涉及多个无法分别核实文本量的推理策略时，请求进入财务核对。已确认的 token 数量仍可查询。超过冻结上界时保留预留并进入超额核对。使用供应商账单补充用量时，销售和采购须使用同一尝试的同一条证据与数量；同一供应商账单行只能绑定一次。[核对流程](operations.md)

真实 AWS 验收可在已有的 Bedrock 测试部署配置中增加 `RELAY_ACCEPTANCE_BEDROCK_GUARDRAIL_ID` 和 `RELAY_ACCEPTANCE_BEDROCK_GUARDRAIL_VERSION`。选择会计量收费策略的 Guardrail。JSON 和 SSE 两笔请求都必须获得正的策略用量，并在隔离账本完成扣费与采购入账。


## 提高上游提示词缓存复用率

同一个多轮工作流可在 HTTP POST 或 Responses WebSocket 握手中携带 `X-Nexus-Cache-Key`，例如 `support-session-73`。使用稳定的不透明值，最多 256 字节。重复或无效的头返回 400。Relay 对通过鉴权和能力筛选的同优先级候选使用带权 rendezvous 选路，并沿用原有故障转移和预留上限。该头仅用于 Relay 选路。Responses 请求也可使用 `prompt_cache_key`。已有部署在没有提示时继续按权重随机分流。

要在部署上自动优化，先为公开模型配置缓存读和目标上游的写入维度，并分别发布匹配的销售价和部署成本价。向节点注册能力加入 `relay.prompt-cache.v1`，在现有部署的 `relay_profile` 中加上 `"auto_prompt_cache": true` 后发布快照。开关默认关闭。按实际部署支持的模型、断点位置和 TTL 开启。对于新请求，路由若含已启用的候选，优先使用显式工作流键；无键时，对至少 1024 个序列化字节的系统指令及工具模板计算摘要，否则使用第一条用户消息。这里的字节门槛仅用于选择路由依据，模型自行判定可缓存 token 数。显式工作流键适合为高流量公共模板分摊负载。路由摘要按组织和模型隔离，进程不保存对话或缓存键，缓存读取以供应商回执为准。

| 启用后的实际部署 | 发送给上游的缓存提示 |
| --- | --- |
| Anthropic Messages | 原生请求保留客户断点；无断点时使用顶层 `cache_control`。来自 Responses 的隐式模式使用同一机制，显式文本断点映射到对应内容块，工具结果尾部的断点映射到对应的 tool_result。显式模式没有断点时不添加自动标记。 |
| OpenAI Responses | 生成租户与模型范围内的 `prompt_cache_key`。既有原生键在该范围内稳定映射，原生 `prompt_cache_options` 保留。来自 Messages 的自动缓存映射到隐式模式，系统和消息文本块断点映射到相同位置；系统指令通过首条 developer 消息表达显式断点。 |
| Bedrock Converse | 用 `cachePoint` 标记消息尾部，或在客户端指定的系统、工具和消息内容块后放置检查点。仅在已确认该模型支持相应位置时启用。 |

跨协议时，未指定 TTL 的缓存标记使用目标上游默认时长，Messages 为 5 分钟，支持新版缓存模式的 Responses 为 30 分钟。客户显式指定的 TTL 在两种协议间没有相同档位，默认在预留前拒绝；调用方可以[逐项指定近似转换](#显式选择近似转换)；工具定义断点等不能映射的位置同样拒绝。Bedrock 支持相同 TTL 的标记时原样保留。消息顺序、工具列表顺序和工具 ID 保持原有转换语义。[OpenAI 缓存](https://developers.openai.com/api/docs/guides/prompt-caching) · [Claude 缓存](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) · [Bedrock 检查点](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html)

平台财务可用 `GET /api/v1/organizations/{org}/cache-usage` 按组织、时间、模型、项目和 API Key 筛选，结果按模型与部署分组。输入 token 命中率用所有选定行的 `sum(cache_read_tokens) / sum(input_tokens)`，请求尝试命中率用 `sum(cache_hit_attempts) / sum(metered_attempts)`。分母为零表示没有可计算的样本。对比 `metered_attempts / attempts` 可判断计量覆盖度；等待 Worker 处理或需要核对的尝试不会被误计为缓存未命中。按不同方案回放相同的多轮流量，同时比较写缓存量、每次完成任务的实际费用和首 token 延迟。

## 启用 HTTP 和 SSE 协议转换

`relay_profile.endpoint` 始终是部署的真实上游协议。`convert_from` 可省略，省略时只接受原生请求。要让同一部署额外服务另一种客户端，在部署上设置下表对应的值，登记集群节点能力，然后发布新修订。移除 `convert_from` 并发布新修订即可关闭该方向。

| 客户端 → 上游 | `endpoint` | `convert_from` | 额外节点能力 |
| --- | --- | --- | --- |
| Messages → Responses | `openai_responses` | `anthropic_messages` | `relay.messages-to-responses.v1` |
| Responses → Messages | `anthropic_messages` | `openai_responses` | `relay.responses-to-messages.v1` |

两种转换均要求 `transports` 包含 `http`。流式和工具调用还需模型、部署分别开启对应能力。WebSocket 继续走原生 Responses 路径。转换可处理显式文本历史、系统指令、自定义函数声明、调用及文本结果。工具 ID 在历史中必须唯一，结果须匹配此前的调用。Responses 工具的 `strict` 建议显式填写；省略时，可将属性均为必填的受支持对象 schema 规范化为严格模式。含可选属性或复杂约束而无法安全规范化时沿用原 schema，并使用非严格函数调用。外部供应商签发的私有项须送回原供应商。显式指定的数值思考预算、供应商专用工具和缺少等价上游参数的请求按已授权路由选择可处理它们的部署。

### 显式选择近似转换

HTTP 和 SSE 生成请求可用 `X-Nexus-Allow-Approximation` 逐项授权语义变化。默认精确匹配。即使授权了近似转换，Relay 也先从有权限的精确候选中选路；需要切换部署时，再按同一规则选择能覆盖冻结预留的候选。没有可用的精确路线或已授权近似路线时，准入返回 400。Bedrock 继续通过已登记的上游适配器参与选路。

例如，向 `POST /v1/messages` 发送以下请求头，表示可以把显式 5 分钟缓存换成 30 分钟缓存，在 Relay 本地匹配停止串，并用提示词续写助手前缀。调用方可以只发送其中一项。

```http
X-Nexus-Allow-Approximation: cache-5m=30m, stop=local, prefill=prompt
```

| 选项 | 实际行为 |
| --- | --- |
| `cache-来源时长=目标时长` | 可在 Messages 的 `5m`、`1h` 与 Responses 的 `30m`、`12h` 之间逐档指定映射，方向与请求一致。仅在支持缓存转换且目标写入档已定价的部署生效。保留可表达的断点位置和显式覆盖；不合法的长短时长顺序在预留前拒绝。目标协议的缓存有效期规则随之生效。 |
| `stop=local` | Messages 的 `stop_sequences` 或 Chat 的 `stop` 需要转到 Responses 时，Relay 在连续的可见回答文本中匹配。命中后隐藏停止串及后续输出，Messages 返回匹配值，Chat 返回 `finish_reason: "stop"`。工具参数、拒答和推理块不参与匹配。首个顺序到达的回答文本片段可增量交付，可能构成停止串的后缀会暂缓。跨块或交错的剩余内容在终态按原顺序补齐。上游拒答、内容过滤、流失败或缺少终态时沿用其原有终止与核对流程。 |
| `prefill=prompt` | 对转入 Responses 的 Messages 请求，保留末尾助手文本为历史，并追加一条固定用户指令，请求模型从最后一个字符继续。模型可能重复前缀或采用不同措辞。 |

**费用差异**

| 已采用的转换 | 结算依据 |
| --- | --- |
| 缓存时长 | 缓存写按目标时长的冻结单价及上游实际写入量计费；命中按实际缓存读取量计费。较短时长也可能导致下一轮重新写入。供应商的时长和价格规则见 [OpenAI 缓存](https://developers.openai.com/api/docs/guides/prompt-caching) 和 [Claude 缓存](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)。 |
| 本地停止串 | 上游会继续生成到其终态。全量输出（包括匹配点之后已经生成的 token）参与配额、采购成本和客户结算。输出上限仍有效；流式完成需要等待真实终态。 |
| 提示词续写 | 额外用户指令参与输入计量；模型生成的重复前缀也属于实际输出。 |

响应头 `X-Nexus-Applied-Approximation` 列出本次实际采用的选项。`X-Nexus-Approximation-Billing` 单独列出目标写入维度、`full_upstream_output` 或 `continuation_prompt_input`。精确路线省略这两个头。用 `X-Request-ID` 查询管理端 `GET /api/v1/organizations/{organization_id}/requests/{request_id}`，可从 `charge_details` 查看实际数量、单价、单位和折扣。未执行的精确路线没有实际回执，Relay 不计算假设差价。

例如，某合同的普通输入、输出单价均为 1，30 分钟写入单价为 1.25。回执有 4 个输入 token，其中 2 个为缓存写入，另有 6 个输出 token。本地停止只交付部分文字时，按该合同结算为 `2 × 1 + 2 × 1.25 + 6 × 1 = 10.50`。

### 跨协议历史续接

使用 Responses ↔ Messages 的 HTTP 或 SSE 生成时，客户端可以把上一轮输出随显式历史传回。Relay 把供应商签名、加密推理、压缩项和专有引用封装在客户端携带的 `nxctx1.` 凭据中。集群用独立派生的 XSalsa20-Poly1305 密钥和随机 nonce 认证加密；此机制不向数据库、Redis 或日志写入正文。相同集群私钥的其他节点或重启后的节点均可处理下一轮。

Relay 在预留前验证企业 Key、公开模型 ID、原部署、供应商模型及凭据版本，并核对客户端带回的可见内容。该轮只能使用匹配的原部署。密文、签名、引文或可见摘要发生改动时，请求返回 `400 invalid_private_history`。集群私钥轮换或上游账户绑定改变后，旧凭据失效。接单节点均须升级到支持此格式的版本。

| 客户端与实际来源 | 客户端如何续接 |
| --- | --- |
| Responses 使用 Claude 或 Bedrock 上游 | Claude 对外展示的思考放入 `reasoning.summary`，签名和脱敏内容保存在 `encrypted_content`。Claude 压缩块返回为 `compaction` 项。把整个 `response.output` 追加到下一轮 `input`。 |
| Messages 使用 Responses 上游 | 公开摘要放入 `thinking`，其 `signature` 携带 Nexus 凭据。没有可见摘要的项使用 `redacted_thinking.data`。OpenAI 的压缩项同样通过该字段携带，扩展字段 `nexus_item_type: "compaction"` 标识压缩边界。OpenAI 提供的内容是密文，没有可展示的压缩摘要。把整组 `content` 作为助手消息放回 `messages`。 |
| Claude 引用转换为 Responses | 可映射的网页来源返回 `url_citation`。输出消息的 `nexus_citations` 保留来源文档、页码、字符区间或检索结果块的位置与引文；`nexus_history` 保留重放所需的原始索引。须保留输出项上的扩展字段。Responses 输入的 `input_file` 可用 Nexus 扩展 `citations: {"enabled": true}` 请求 Claude 文档引用。 |
| Responses 引用转换为 Messages | 网页引用返回为文本块上的来源链接；`nexus_start_index`、`nexus_end_index` 保留其在原回答中的准确位置。上游没有提供来源摘录时，`cited_text` 为空字符串。文件或容器文件引用随附一个 `nexus_item_type: "annotations"` 的不透明上下文块，其 `nexus_annotations` 提供内容块序号及本 Key 的文件句柄。带回所有相关文本块与凭据，保持原顺序。 |

两端共同具备的定性思考强度（`low`、`medium`、`high`、`xhigh`、`max`）按同名值传递，具体模型决定是否支持和怎样执行。Messages 的自适应思考可请求 Responses 返回可重放的加密项。流式转换只展示供应商公开的摘要，使用完成的原始项签发续接凭据。供应商省略的私有状态无法恢复。[OpenAI 推理](https://developers.openai.com/api/docs/guides/reasoning) · [Claude 思考强度](https://platform.claude.com/docs/en/build-with-claude/effort)

双向自动压缩支持显式 token 阈值。Responses 的 `context_management[].compact_threshold` 对应 Messages 的 `compact_20260112` 编辑和 `input_tokens` 触发值。Messages 客户端按协议提供 `compact-2026-01-12` beta；Responses 转发到 Messages 时 Relay 会添加该 beta，重放对应压缩块时也会添加。需要明文签名摘要的 Claude 按需压缩，以及 `POST /v1/responses/compact`，分别使用各自的原生上游。

引用文件时，使用源部署对应 Files API 签发的 `file_nx_…` 句柄。Relay 在每次重放时重新确认文件、容器和容器文件仍属于当前企业 Key、模型和部署，资源删除或到期后拒绝引用。Claude 文档引用的输出字段 `file_id` 经核验后，后续 Claude 输入使用该协议规定的文档序号及位置字段。[Claude 引用](https://platform.claude.com/docs/en/build-with-claude/citations) · [OpenAI 引用结构](https://developers.openai.com/api/reference/resources/responses/methods/create)


客户端看到公开模型名、自己的内容块或输出项、工具调用 ID 和终态。Messages 工具流按 Responses 输出项和参数事件传送，Responses 的交错输出在有界缓冲内按 Messages 内容块顺序写出。转换器使用 `NEXUS_RELAY_RESPONSE_BYTES` 约束需要保存的输出，不完整 EOF 会中断流。输出达到上限分别映射为 `max_tokens` 或 `incomplete` 加 `max_output_tokens`。客户可见的 usage 按目标协议的计数语义换算。Responses 允许缺失时返回 null；Messages SDK 要求数值对象，流开始时的计数和缺失时的占位计数为零，终态有可信 usage 时发送实际计数。Worker 始终读取单独保存的上游原始证据；缺失时保留预留并进入核对。

从仓库根目录运行 `cargo test --locked --manifest-path src/relay/Cargo.toml --test conversion`，以及 `uv run python scripts/run_integration.py -q tests/integration/test_relay_conversion.py`。后者用相同的本地 HTTPS 部署对照原生与转换的 JSON、SSE、上游尝试、消费和预算。[Responses 函数工具](https://developers.openai.com/api/docs/guides/function-calling) · [Messages 流事件](https://platform.claude.com/docs/en/build-with-claude/streaming)

## 阶段、依赖与交付

| 阶段 | 依赖 | 实施任务及归属 | 验收与退出条件 |
| --- | --- | --- | --- |
| **P0 协议核心与样本** | 已确定两种协议范围 | 在 `src/relay/` 建独立 Cargo 包。定义版本化样本目录、HTTP 输入、WS 握手与文本事件、终止状态、用量证据类型。按需选取 SSE 分帧代码并复用成熟文本编码器，参照 new-api 的转换和分词测试独立编制样本，记录实际复用片段的来源与许可。 | Rust 在干净环境可独立编译；文本、分块 UTF-8、CRLF、半帧、错误终态和超限帧有离线测试；验证两个生成路径上的方法：Messages POST、Responses POST 及 Responses GET Upgrade。 |
| **P1 节点与控制契约** | P0 的字段和样本 | **catalog/publication** 实现部署端点、传输能力、计数规则版本和上界依据的发布、旧记录升级及能力协商。**billing** 实现期望格式绑定、两套归一化规则及重放测试。**Relay** 建节点池、快照索引、密文包解密、限额、租约、八个 SQL 调用和完成任务。 | 以隔离 PostgreSQL/Redis 和 Python 模拟器作对照，验证相同快照、旧节点拒绝未知能力、撤权、密钥轮换、提交丢响应、租约重放和失效。新 Worker 能同时处理历史 fixture 事件与新版协议样本；仅 HTTP 的部署、未确认新能力的节点不会获配 WS 轮次。 |
| **P2 原生非流式闭环** | P1；每条链路有明确能力与证据规则 | **两份独立交付**：Messages → Messages、Responses → Responses。每份接真实 Relay HTTP 入站、本地上游替身、数据库收件箱和 Worker。实现候选选择、凭据注入、输入/输出上界、成本证据及客户交付归属。 | 两种入口分别验证成功、拒绝、重复上报、缺失用量。替身统计实际收到的请求数，数据库核对尝试、账本、余额和预算。真实部署的收费启用另以授权的商户 API 和计量样本验收。 |
| **P3a 原生 SSE 与故障处理** | 对应方向 P2 通过 | 实现两种协议的 SSE 分帧、背压、超时、工具流、取消和排空。不同传输复用同一套按生成请求预留、按尝试上报和核对的逻辑。 | 验收主动断开、上游错误、未知提交、长流续租和资金一致性。 |
| **P3b Responses 双端 WebSocket（首期必交）** | P1 的传输声明、P2 Responses 链路；与 P3a 共用事件与计量组件 | `server/` 实现客户端握手、会话与逐轮准入；`upstream/` 实现原生 WSS 拨号与专属连接复用；`request/` 接入同连接续接、取消和逐轮完成。参考 new-api 的握手、路由固定和每轮结算用例，用 Rust 独立实现。 | 真实 WS 客户端和可观测的上游 WS 替身双端返回 101。连续两轮生成只建立一个上游连接，收到两条独立 create；两轮各自有请求、用量和正确流水。以声明支持 WS 的获授权真实部署复核端到端行为。 |
| **P4 双向转换** | 相关原生 HTTP/SSE 基准通过 | **分别合并并启用** Messages → Responses 与 Responses → Messages。以 RelayKit 中请求、响应、流终止的拆分为对照，断言两条转换路径都不经过第三种协议。覆盖显式文本历史、自定义工具声明/调用/结果、流内 ID、停止原因及错误体。不可等价表达的输入在准入前拒绝。 | 每个方向与对应原生上游请求比较交付内容、调用关联及资金结果。上游原始 usage 相同则归一化数量相同；客户端可见的翻译或合成字段不参与收费。 |
| **P5 部署、负载与发布** | 计划启用的组合通过对应阶段 | 提供独立二进制、配置说明、就绪/存活检查、节点和 Worker 指标、按集群启用与回滚步骤。运行生产形态节点的协议、故障和容量测试。 | 首期验收包含 P3b 双端 WSS 与 HTTP/SSE，归档可复现报告及已测的端点/传输矩阵。灰度、回滚后都核对在途请求和资金流水；一条组合的验收单独决定该组合能否开流量。 |

P1 的控制平面变更和 Rust 协议客户端可在同一份样本契约上并行；P2 的两个方向可以分别上线测试集群。真实厂商账户或授权样本缺失时，离线移植、受控上游测试和节点契约验证仍可继续，验收结果明确记录实测环境。

## 端到端验收矩阵

| 场景 | 可观察结果 |
| --- | --- |
| 权限或能力不匹配 | 失效 Key、跨组织模型、未声明端点、转换不支持的参数分别返回可识别响应；上游收到 0 次请求。分别检查 Redis 限额和资金预留，明确拒绝后无遗留资金占用。 |
| 正常交付与重放 | 先成功预留、再登记尝试、再发送一次上游请求。重复投递相同事实后仅一笔客户扣费；预算支出等于该请求客户费用，预留归零，各尝试成本独立。 |
| 失败、重试与取消 | 无尝试时取消释放；明确可重试且尚未交付时，第二次发送另建尝试；可能已执行时禁止盲重发。仅客户最终交付的尝试计入客户费用。客户已断开时以实际可证明的交付和用量决定收费，余额/成本均可追溯。 |
| 流与用量边界 | 首字节前失败、输出后断流、达到已预留输出上限的合法终态及实际用量越界、客户端主动取消和无终态 EOF 各有样本。SSE 每字节切分、多个工具片段交错、慢消费者分别验证终止事件、内存上限和租约续期。 |
| WS 握手与复用 | 错误 Key 在升级前被拒绝；仅握手或 ping/pong 时，观察到 0 笔生成预留/扣费。对原生 WS 部署录下两端握手、上游实际收帧次数和连接代次；HTTP/SSE 替身不得令双端 WS 用例通过。 |
| WS 逐轮隔离 | 同一套企业额度由 HTTP 与 WS 共用。两轮间撤销 Key、冻结账户或更换已绑定凭据后，下一轮不向上游发送。忙碌时发第二个 create 得到关联错误，第一轮继续；取消被拒或迟到的上一轮错误不能终结新轮。 |
| WS 续接与掉线 | 用本连接的上一轮 response ID 和工具结果成功续接，并核对含历史的预留上界。换租户、换 Key、换连接、换模型及无效前序 ID 分别拒绝或要求完整历史；验证写出结果未知时无自动补发。 |
| WS 背压与账本 | 下游慢读/断开、上游握手失败/半途断开、终态重复与滚动排空分别注入。比对每轮实际开始的尝试、原始 usage、预留释放或核对事项；两个非零成功轮次恰有两笔消费。关闭后无残留连接任务和运行租约。 |
| 分词与预留 | 用同一公开别名路由到不同部署，对转换后的系统指令、工具定义与多轮结果逐一核对计数。已知编码的普通文本与基准编码器比对；对 Claude 和超长分段分别记录估计误差，注入一次低估，断言预留仍按已验证上界或拒绝。缺失上游 usage 时不得把本地计数写为实际消费。 |
| 计量语义 | 验证累计输出、缓存命中、两类缓存写入、推理子集、负数/溢出、缺失与真实零。缺失或冲突入核对，保留原始尝试与预留；可信零量按既有规则释放。价格版本切换不改变在途请求。 |
| 依赖与崩溃 | 分别切断 PostgreSQL、Redis、Worker；在预留提交、尝试提交、收到用量和上报提交前后注入故障。按原 ID 查证或重放；有上游副作用但证据丢失的请求进入核对，不估造账单。 |
| 快照与多节点 | 启动、错过通知、旧修订、跨集群读、冻结、密钥轮换、旧租约释放、节点替换与排空。检查快照确认版本、上游凭据归属与节点身份。 |
| 版本兼容 | 现有部署升级后仍可查询，旧节点继续使用兼容快照，未知必需能力无法确认；先升级 Worker，再放行发送新格式的节点。回滚时旧记录和已确认流水可复核。 |

**负载口径。** 先重复本机 8/64 活跃节点、8 家企业均摊与单家占 90%、50 请求/秒持续 30 秒，以及 64 节点突发 512 请求。记录实际输入速率、预留与用量提交 p99、最后证据到结算 p95/p99、输入结束积压、清空时间和错误/未完成数。按同一环境比较两轮并反转顺序。另外对冷/热编码缓存、超长文本和高并发计数记录 p99、RSS 及输入大小。另设 Responses WS 空闲连接、连续多轮、小量长轮与慢读负载，记录两端连接数、复用率、活跃轮数、逐轮首事件、RSS、断线后清理及账本结果。再对长流、慢客户端、配置更新、上游高延迟和跨地域网络单独测试，记录首字节、事件间隔、进程 RSS 和数据库池等待。沿用结算目标 p95 ≤ 2 秒、p99 ≤ 5 秒；现有 Python 数据用于对照，Rust 性能待测。[负载与账本基线](control-plane-acceptance.md#准入与结算优化复测)

## 首批任务与部署输入

1. **先锁定两组样本。** 每个协议各选一个候选部署和模型，整理原生请求、成功/失败响应、SSE、工具及 usage 样本；Responses 另准备双端 WS 握手、两轮 create、取消与断线样本。先用固定样本开工，标记来源、协议版本、已脱敏的字段和预期结果；真实上游验证单独记录。
2. **冻结最小契约。** 为这两个部署填好端点、能力、可证明的输入上界、强制输出上限、计价维度及版本化 `usage_format`。给出普通、缓存、缺失用量样本的预期处理结果，并记录可收费样本的客户费用。分词样本同时标记选用的编码或估算器、预留上界及其依据。未经验证的模型/维度组合保持未启用。
3. **交付第一条纵向链路。** P0 移植分帧与样本测试，P1 打通一个测试节点的快照、授权和上报；P2 任选一个已具备样本的原生方向，跑通从客户端 HTTP 到一条消费流水，再按同一口径验收另一方向。
4. **灰度前确认环境参数。** 由部署方给出单节点并发流、请求大小、WS 空闲/活跃连接预算、消息与队列上限、允许的排空时限、数据库总连接预算及目标网络延迟，据实测设定默认值。对未知结束原因，沿用保留事实、进入核对的策略。

每次交付在同一报告区分协议样本测试、真实 PostgreSQL/Redis 加上游替身、获授权真实厂商验证、部署环境容量四类证据。只有完成该组合所需的验证后，才在快照中对目标集群声明能力。
