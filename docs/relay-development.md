# Rust Relay 开发计划

目标是交付可独立部署的 `nexus-relay`，为企业提供 Anthropic Messages 和 OpenAI Responses 请求转发。首期含 Responses 原生双端 WebSocket，即客户端升级到 Relay，由 Relay 使用部署凭据拨上游 WSS。本计划于 2026-11-14 随首期设计定稿，各阶段按下表验收。协议覆盖、模块职责、计数口径、CC Switch 代码复用及 new-api 行为对照见[Relay 设计](relay-design.md)。以下任务按依赖推进，可独立验收的产物作为合并单位。

## 运行现有实现

从仓库根目录执行 `cargo build --release --locked --manifest-path src/relay/Cargo.toml`，生成 `src/relay/target/release/nexus-relay`。节点通过 HTTP/1.1 提供 `POST /v1/messages`、`POST /v1/responses` 和 `GET /v1/responses` Upgrade。POST 支持普通 JSON 和 `stream=true` 的 SSE。Upgrade 建立串行多轮会话，首轮准入后才拨部署的 WSS。对外 TLS 由入口负载均衡终止，`GET /live` 和 `GET /ready` 分别提供存活和就绪状态。Prometheus 指标在独立的指标监听上提供，日志为 stderr 上逐行 JSON，字段、指标清单和探针用法见[Relay 可观测性](operations.md#relay-可观测性)。

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

在 catalog 中为候选部署配置 `relay_profile`，声明原生端点、`http` 或 `websocket` 传输、工具支持、计数规则、输入输出上界及验证依据。节点使用已验证的部署输入上界保守预留。Responses 默认强制 `store=false`，省略输出限制时使用部署上限。每个候选的路径在其基础地址后追加 `/v1/messages` 或 `/v1/responses`，已有 `/v1` 的地址只追加端点名。旧部署保留原配置，声明端点后才参与原生转发。

流量按快照能力启用。基础节点登记 `reservation.v1`、`usage-events.v1`、`relay.native.v1`。HTTP 部署开启 `streaming` 时，新发布的快照另要求 `relay.sse.v1`，公开模型也需要开启流式能力。Responses 部署开启 `websocket` 时，还需 `relay.responses-ws.v1`。只有通过验证的部署才声明 `continuation` 和 `cancellation`。前者允许用本连接已完成的上一轮 ID 续接；后者允许转发原生取消事件。其余部署通过断开专属上游连接中止在途生成，保留未知结果供核对。

WS 握手使用企业 Bearer Key，空闲和 ping/pong 不触发资金预留。每次 create 重查 Key、部署、凭据和模型，按覆盖完整上下文的部署上界预留，并使用独立尝试。每个连接最多一个在途轮次，同一会话复用固定上游连接。SSE 与 WS 通过有界队列传递事件并独立续租。节点以终态事件的真实写出结果判定交付。客户端已断开、结果未知或缺失用量时，Worker 依据已有事实核对。

运行 `cargo test --locked --manifest-path src/relay/Cargo.toml` 验证协议、路由及写出跟踪。运行 `uv run python scripts/run_integration.py -q tests/integration/test_plane.py tests/integration/test_relay.py tests/integration/test_relay_streams.py` 验证真实客户端、本地 TLS 上游、隔离 PostgreSQL/Redis 和 Worker 的完整链路。

获授权真实部署可运行 `tests/integration/test_live_relay_ws.py`。设置 `RELAY_ACCEPTANCE_WS_RUN=1`、`RELAY_ACCEPTANCE_WS_URL`、`RELAY_ACCEPTANCE_WS_MODEL` 和 `RELAY_ACCEPTANCE_WS_KEY`，使用上述隔离集成命令并指定该测试文件。测试只在一个连接串行发送两次生成，`RELAY_ACCEPTANCE_WS_OUTPUT` 默认每轮 64。商户 Key 写入本次隔离 catalog。给出 `wss://` 时 Relay 直接拨 TLS，给出回环 `ws://` 时测试搭建临时 TLS 入口，保留原始握手、帧和端点的升级状态。测试余额和真实上游计费分别核对。[Responses 续接语义](https://developers.openai.com/api/docs/guides/websocket-mode)

## HTTP 和 SSE 故障转移

一次尝试被证明没有执行，且客户端尚未收到任何字节时，节点在同一请求内换部署重发。可证明未执行指连接未建立（DNS、TCP、TLS 或连接超时），或状态与厂商的明确限流/过载错误匹配且无正用量。普通 502 或无法验证的拒绝进入核对。请求写出后的超时、2xx 之后的断流和其他状态都不重发。完整判定表和上报规则见[故障转移设计](relay-design.md#http-与-sse-故障转移)。

在路由策略中为同一模型配置多个候选并设置 priority 和 weight。故障转移只在请求授权时的快照内挑选，先用完同组剩余权重再降组；候选的输入上界须不超过首次预留，输出上限须能容纳已预留的输出。每个候选都需要已发布的成本价，并在网关集群的部署范围内。每次重发都提交新的尝试，Worker 只对最终交付的尝试向客户收费；没有用量的失败尝试单独进入成本核对。

运行 `cargo test --locked --manifest-path src/relay/Cargo.toml --test failover` 验证候选选择与错误分类。运行 `uv run python scripts/run_integration.py -q tests/integration/test_relay_failover.py`，用两个本地 TLS 上游验证 503 与拒绝连接后的切换、SSE 切换、发送后读超时、SSE 输出后断流、全部候选失败及 `NEXUS_RELAY_MAX_ATTEMPTS=1` 的原有行为，并核对尝试记录、单笔扣费、独立成本和预留释放。用例包含一次 30 秒的上游响应头等待。

## Bedrock Converse 上游

Messages 和 Responses 的 HTTP 请求通过 Bedrock `Converse` 转发，`stream=true` 通过 `ConverseStream` 转成客户端原有 SSE 格式。适配范围包括显式文本对话、系统指令、函数工具与结果、采样参数和输出上限。Responses 的原生 WebSocket 轮次仍要求 Responses WSS 部署。媒体、模型专有推理参数与不能等价表达的工具选项在预留前拒绝。[配置步骤](catalog-api.md#amazon-bedrock-部署) · [Converse](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html) · [ConverseStream](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ConverseStream.html)

节点按 AWS EventStream 的帧长度、校验和、事件类型和内容块索引解码。`messageStop` 记录停止原因，收到后续 `metadata` 才提交终态。缺失总量、已计量维度相互矛盾、流被截断或缓存写入无法对应冻结价格时，Worker 保留证据并进入核对。客户端可见的 usage 与结算证据分别生成，尝试固定使用 `aws.bedrock.converse.v1`。部署使用的计数规则要求普通输入、缓存读取、缓存写入及输出之和等于 `totalTokens`；缓存写入按 `cacheDetails` 的 5m、1h 分项核对。生产部署应使用实际模型的计量样本确认这些维度与价格。[AWS TokenUsage](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_TokenUsage.html)

运行 `uv run python scripts/run_integration.py -q tests/integration/test_bedrock_relay.py` 可验证两种入口、两种鉴权、普通及流式响应、异常流和 Worker 账本。该用例使用隔离 PostgreSQL、Redis 及本地 HTTPS 上游。

## 提高上游提示词缓存复用率

同一个多轮工作流可在 HTTP POST 或 Responses WebSocket 握手中携带 `X-Nexus-Cache-Key`，例如 `support-session-73`。使用稳定的不透明值，最多 256 字节。重复或无效的头返回 400。Relay 对通过鉴权和能力筛选的同优先级候选使用带权 rendezvous 选路，并沿用原有故障转移和预留上限。该头仅用于 Relay 选路。Responses 请求也可使用 `prompt_cache_key`。已有部署在没有提示时继续按权重随机分流。

要在部署上自动优化，先为公开模型配置缓存读和目标上游的写入维度，并分别发布匹配的销售价和部署成本价。向节点注册能力加入 `relay.prompt-cache.v1`，在现有部署的 `relay_profile` 中加上 `"auto_prompt_cache": true` 后发布快照。开关默认关闭。按实际部署支持的模型、断点位置和 TTL 开启。对于新请求，路由若含已启用的候选，优先使用显式工作流键；无键时，对至少 1024 个序列化字节的系统指令及工具模板计算摘要，否则使用第一条用户消息。这里的字节门槛仅用于选择路由依据，模型自行判定可缓存 token 数。显式工作流键适合为高流量公共模板分摊负载。路由摘要按组织和模型隔离，进程不保存对话或缓存键，缓存读取以供应商回执为准。

| 启用后的实际部署 | 发送给上游的缓存提示 |
| --- | --- |
| Anthropic Messages | 原生请求保留客户断点；无断点时使用顶层 `cache_control`。来自 Responses 的隐式模式使用同一机制，显式文本断点映射到对应内容块，工具结果尾部的断点映射到对应的 tool_result。显式模式没有断点时不添加自动标记。 |
| OpenAI Responses | 生成租户与模型范围内的 `prompt_cache_key`。既有原生键在该范围内稳定映射，原生 `prompt_cache_options` 保留。来自 Messages 的自动缓存映射到隐式模式，系统和消息文本块断点映射到相同位置；系统指令通过首条 developer 消息表达显式断点。 |
| Bedrock Converse | 用 `cachePoint` 标记消息尾部，或在客户端指定的系统、工具和消息内容块后放置检查点。仅在已确认该模型支持相应位置时启用。 |

跨协议时，未指定 TTL 的缓存标记使用目标上游默认时长，Messages 为 5 分钟，支持新版缓存模式的 Responses 为 30 分钟。客户显式指定的 TTL 在两种协议间没有相同档位，转换会在预留前拒绝；工具定义断点等不能映射的位置同样拒绝。Bedrock 支持相同 TTL 的标记时原样保留。消息顺序、工具列表顺序和工具 ID 保持原有转换语义。[OpenAI 缓存](https://developers.openai.com/api/docs/guides/prompt-caching) · [Claude 缓存](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) · [Bedrock 检查点](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html)

平台财务可用 `GET /api/v1/organizations/{org}/cache-usage` 按组织、时间、模型、项目和 API Key 筛选，结果按模型与部署分组。输入 token 命中率用所有选定行的 `sum(cache_read_tokens) / sum(input_tokens)`，请求尝试命中率用 `sum(cache_hit_attempts) / sum(metered_attempts)`。分母为零表示没有可计算的样本。对比 `metered_attempts / attempts` 可判断计量覆盖度；等待 Worker 处理或需要核对的尝试不会被误计为缓存未命中。按不同方案回放相同的多轮流量，同时比较写缓存量、每次完成任务的实际费用和首 token 延迟。

## 启用 HTTP 和 SSE 协议转换

`relay_profile.endpoint` 始终是部署的真实上游协议。`convert_from` 可省略，省略时只接受原生请求。要让同一部署额外服务另一种客户端，在部署上设置下表对应的值，登记集群节点能力，然后发布新修订。移除 `convert_from` 并发布新修订即可关闭该方向。

| 客户端 → 上游 | `endpoint` | `convert_from` | 额外节点能力 |
| --- | --- | --- | --- |
| Messages → Responses | `openai_responses` | `anthropic_messages` | `relay.messages-to-responses.v1` |
| Responses → Messages | `anthropic_messages` | `openai_responses` | `relay.responses-to-messages.v1` |

两种转换均要求 `transports` 包含 `http`。流式和工具调用还需模型、部署分别开启对应能力。WebSocket 继续走原生 Responses 路径。转换可处理显式文本历史、系统指令、自定义函数声明、调用及文本结果。工具 ID 在历史中必须唯一，结果须匹配此前的调用。Responses 工具的 `strict` 建议显式填写；省略时，可将属性均为必填的受支持对象 schema 规范化为严格模式。含可选属性或复杂约束的 schema 需要显式指定严格性。私有推理历史、媒体、托管工具、不可映射的停止参数会在预留前返回 400。

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
