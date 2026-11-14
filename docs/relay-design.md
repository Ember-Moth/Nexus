# Rust Relay 迁移与设计方案

首期 Relay 只支持 **Anthropic Messages** 和 **OpenAI Responses**，范围同时适用于客户端入口、上游部署、路由候选和协议转换。对外生成接口为 `POST /v1/messages` 与 `POST /v1/responses`。

以下为待评审的实现方案。建议先在 `src/relay/` 跑通两条原生链路，各自从企业调用 Key、资金预留走到可核对的 USD 消费流水，再分别验证这两个协议间的转换。每条链路单独声明可用能力。

## 上游基线与迁移清单

选取 `farion1231/cc-switch` 的固定提交 `8c76bfe1e2628fe385b2c009020036d85510b01d`。Rust 项目位于 `src-tauri/`，代理是其中的 `src/proxy/` 模块。其服务器状态持有桌面应用句柄和数据库，转发器还依赖本地 provider 路由与多种账户认证。迁移单位应为可测试的协议能力。每批引入记录原路径、提交号、本地改动与移植的测试，随代码和发行包保留上游 MIT 声明，核对文件内的其他来源说明。[上游源码](https://github.com/farion1231/cc-switch/tree/8c76bfe1e2628fe385b2c009020036d85510b01d/src-tauri/src/proxy) · [上游许可证](https://github.com/farion1231/cc-switch/blob/8c76bfe1e2628fe385b2c009020036d85510b01d/LICENSE)

下表路径相对上游 `src-tauri/src/proxy/`。

| 能力 | 源码锚点 | 迁移处理 |
| --- | --- | --- |
| 两个协议间的请求与响应 | `providers/transform_responses.rs`、`providers/transform_codex_anthropic.rs` | 前者服务 Messages 客户端到 Responses 上游，后者服务 Responses 客户端到 Messages 上游。按方向抽取工具 ID、参数与输出项映射的代码和样本；共享 helper 按用途提取，保持转换路径直接连接这两个协议。 |
| SSE 与流转换 | `sse.rs`、`providers/streaming_responses.rs`、`providers/streaming_codex_anthropic.rs` | 原生链路保留各自事件。转换时分别验证 Responses SSE 到 Messages SSE、Messages SSE 到 Responses SSE；覆盖跨块 UTF-8、工具参数增量、终止、错误、慢客户端和半帧上限。 |
| HTTP 传输 | `http_client.rs`、`hyper_client.rs`、`content_encoding.rs`、`response_processor.rs` | 保留连接复用、响应流、有限解压与 hop-by-hop 头处理的行为测试；用服务端客户端池管理 TLS、超时和取消。上游地址与鉴权材料仅来自已授权部署。 |
| API 入口与编排 | `server.rs`、`handlers.rs`、`handler_context.rs`、`forwarder.rs`、`error_mapper.rs` | 参考端点行为及错误映射，重建请求生命周期。每次发送上游前都必须有已提交的尝试记录，包括重试。 |
| 路由与故障转移 | `provider_router.rs`、`circuit_breaker.rs`、`failover_switch.rs`、`model_mapper.rs` | 候选、优先级、权重及公开模型到上游模型名的映射取自 Nexus 快照。熔断如引入，只影响节点在授权候选内的选择；运营配置仍由 catalog/publication 修改。 |
| 用量与成本 | `usage/parser.rs`、`usage/calculator.rs`、`usage/logger.rs` | 按端点复用原始计数字段识别和测试样本。Nexus Worker 独占价格、折扣、预算与账务计算。上游部分解析和流转换会为缺失用量填零，移植时必须保留“字段缺失”的信息。[计数解析](https://github.com/farion1231/cc-switch/blob/8c76bfe1e2628fe385b2c009020036d85510b01d/src-tauri/src/proxy/usage/parser.rs) |
| 桌面与供应商特例 | `session/`、`providers/*_oauth_auth*`、`copilot_*`、各类优化器与 rectifier | 桌面配置接管、SQLite、托盘、订阅账号登录、客户端身份模拟均在迁移范围之外。内容改写、自动删减媒体、私有字段清理逐项评审；能力不匹配时返回可识别的错误。 |

## 服务边界

建议从一个 Cargo 包开始，名称为 `nexus-relay`。以下是实现职责，按交付阶段建立文件。

| 位置 | 责任 |
| --- | --- |
| `src/relay/src/main.rs`、`server/` | 读取部署配置，建立数据库与 HTTP 连接池，挂载已启用路由，处理就绪、排空和关闭。 |
| `protocol/` | 请求/响应模型、协议转换、SSE 分帧、终止语义和厂商用量字段提取。核心函数用固定样本即可测试。 |
| `upstream/` | 根据部署建立请求，应用解密凭据、连接超时、首字节及流空闲超时；请求路径按端点构造，上游鉴权头由 Relay 注入，跨主机重定向不能携带原凭据。 |
| `control/` | 八个 `gateway_v1` 调用、集群快照、密文包、Redis 多层限额、租约与心跳。以 Python 模拟器作为契约测试的对照端。 |
| `request/` | 单次调用的模型与部署选择、上界确认、预留、尝试、交付、证据上报和清理。仅此处决定是否发起下一次尝试。 |

入口凭据按各自协议读取企业调用 Key：Messages 使用 `x-api-key`，Responses 使用 `Authorization: Bearer`。发往上游的凭据来自授权部署。

部署配置提供节点专属 PostgreSQL 登录、Redis 地址与命名空间、集群私钥、HTTP 监听和资源上限。运行时按集群接收模型、应用 Key 摘要、策略、部署和凭据引用。人员会话、支付凭据、上游存储根密钥只属于控制平面。客户端提供的模型名必须解析为授权模型 ID，客户端传入的 URL、组织、价格或上游密钥均不能改变路由。[快照来源](../src/nexus/modules/catalog/persistence/snapshots.py) · [数据平面契约](plane-protocol.md)

## 协议覆盖与交付条件

下表的箭头表示请求从客户端到上游的方向，响应沿原路返回。先验收两条原生链路；双向转换属于后续设计步骤，各方向分别启用。

| 客户端入口 | 上游端点 | 拟支持的处理与验收 |
| --- | --- | --- |
| `POST /v1/messages` | Anthropic Messages | 原生文本与 SSE。验证内容块顺序、工具调用 ID 与输入增量、结束原因及缓存读写用量。 |
| `POST /v1/responses` | OpenAI Responses | 原生文本与 SSE。验证输出项、函数调用与结果关联、完成/失败/未完成状态及最终 usage。 |
| `POST /v1/messages` | OpenAI Responses | 将 Messages 请求转换为 Responses，返回 Messages 响应或 SSE。逐项核对工具往返、停止语义和客户端可见内容。 |
| `POST /v1/responses` | Anthropic Messages | 将 Responses 请求转换为 Messages，返回 Responses 输出项或 SSE。核对调用 ID、参数增量、终止状态及不可映射字段的错误行为。 |

入口路由使用明确的 HTTP 方法和路径。OpenAI Chat Completions、Gemini 和其他协议均在首期范围外，也不能作为中转协议或失败后的回退目标。每个启用组合分别记录文本、SSE、工具能力、真实用量格式和输入输出上界。涉及前序响应 ID、服务端会话或有额外收费的托管工具时，在具备对应的租户归属、恢复及计量能力前，准入阶段拒绝相应请求。[目录当前字段](../src/nexus/modules/catalog/application/schemas.py) · [上游路由表](https://github.com/farion1231/cc-switch/blob/8c76bfe1e2628fe385b2c009020036d85510b01d/src-tauri/src/proxy/server.rs)

## 一次请求如何执行

1. **加载配置。** 节点以自己的数据库登录获取本集群完整快照，校验原始字节的 SHA-256、schema、必需能力及引用。构建只读索引后原子切换，调用 `ack_config`。每个在途请求保留自己的 revision。按需调用 `get_credential_envelope`，核对集群、凭据和版本后解密。
2. **授权并预留。** 从客户凭据计算摘要，在快照中找到 Key、组织、项目和模型策略。选定支持的协议转换，基于最终上游请求核对输入上界，并强制执行输出上限。按组织、项目、Key 获取 Redis 限额租约，再调用 `reserve_request`。只有事务成功才继续；明确拒绝时按限额契约释放，提交结果未知时使用同一请求 ID 与原参数查证。
3. **登记尝试。** 在发布的候选中按优先级和权重选部署，固定上游模型、协议和凭据。续租成功后，用同一请求 ID、新尝试 ID 和递增序号调用 `start_attempt`；收到提交确认后才允许发送上游。数据库事务只覆盖授权或事实写入，HTTP 期间归还数据库连接。
4. **转发与提取证据。** 复用 HTTP 连接池并原样或按协议转换响应。流式响应按下游读取速度写出，同时在转换之前从上游原始数据提取用量。长流由独立计时器续租，避免暂停读取或下游背压使续租超期。设定请求大小、响应解压上限、单帧及未完成工具参数上限、首字节与空闲时限。禁止透明重发有副作用的 POST；每次允许的故障转移都走新的尝试。
5. **结束与恢复。** 每个已开始尝试提交一个 `attempt.finished`，每个请求提交一个 `request.closed`，列出实际开始的全部尝试及最终交付尝试。使用同一个业务 ID 和原始序列化事件重放提交结果未知的写入，保留 `occurred_at`。上报返回冲突时保留关联信息并进入核对。流的交付由 HTTP 写出结果判定；结束任务需由节点监督至提交或排空截止，客户端断连不会直接丢弃上报任务。调用 `get_request_status` 核实状态，Redis 租约按确认用量结清；客户扣费和每次尝试成本由 Worker 分别处理。[事件与幂等规则](plane-protocol.md#用量事件)

| 故障窗口 | Relay 行为 |
| --- | --- |
| 预留成功，尚未开始尝试 | 客户取消或本地失败时调用 `cancel_unstarted_request`；提交结果未知先查证。 |
| 尝试已提交，上游可能收到请求 | 保留尝试 ID 和预留；无法证明上游未执行时记录未知结果。首期只对可证明安全且尚未向客户交付的情况执行受控重试。 |
| 已交付流内容，随后断流或客户断开 | 停止交付并取消上游，有界收集可取得的最终用量；已输出内容的流不能切换部署继续拼接。返回或记录真实结束原因。 |
| 缺失用量、超时、租约失效 | 停止扩大执行；缺失用量传 `null`，保留证据交给 Worker 等待或核对。兼容层合成的 usage、Messages 的 `message_stop` 或 Responses 的终止事件本身均不足以证明实际用量。 |
| 节点退出或进程丢失 | 正常关闭先摘除就绪、停止接收新请求，再限时完成上报。在途请求由原节点身份处理。崩溃发生在收到用量与持久化之间时可能丢失证据，通过已有尝试和预留核对；验收需明确这个窗口。 |

## 开发前需要落实的控制平面契约

- **部署端点。** 当前 catalog 的协议族和模型的 `streaming`、`tools` 标志不足以证明部署支持特定端点，尤其是 `openai_compatible` 不能单独作为 Responses 能力。建议在控制平面显式声明部署支持的 Messages 或 Responses 端点，由 publication 随快照发布。Relay 只选择与本次入口匹配且已经验收的原生或转换组合；重试沿用同一约束。
- **真实计量。** 当前 Worker 只识别 `fixture.tokens.v1` 和 `fixture.dimensions.v1`。分别为 Messages 与 Responses 的真实上游定义有版本的 `usage_format`，在响应转换前采集原始计数。Messages 分辨普通输入、缓存读取、缓存写入和累计输出；Responses 将缓存输入视为总输入的子集，推理 Token 视为输出的子集。Rust 与 Python 用相同样本验证部分流事件、最终计数、缺字段与真实零。收费开放前，每个启用部署都需具备上界证明、Worker 规则和真实用量样本。[归一化实现](../src/nexus/modules/billing/domain/usage.py) · [事件入库约束](../migrations/sql/0008_gateway_protocol/events.sql)
- **上界。** 协议转换产生的系统提示、工具 schema、模型 tokenizer 和输出参数都可能改变实际计量。为每个组合声明经验证的计数或保守上界算法；一次请求可重试的部署都必须满足已预留上界。无法给出可靠上界时，限制或拒绝对应输入，并记录原因。
- **多节点配置与运行状态。** 启动、重连时校验完整快照；日常结合 Pub/Sub 和错峰版本检查，仅在版本变化或状态不确定时回源。现有 `get_config_snapshot` 返回完整包；若需要从 PostgreSQL 廉价复核最新授权版本与摘要，应单独设计受节点身份限制的只读操作。数据库继续在准入时校验当前权限和修订。Rust 与 Python 对同一组织的 Redis 键、原子脚本、租约 owner 和安全截止需可互操作。
- **运维。** 每个节点分配有界连接池；集群总连接预算包含 API、Worker 和所有节点。监控授权提交、用量持久化、上游首字节、流中断、待核对请求、积压和清空耗时。日志保留关联 ID 和原因码，避免收集客户正文、调用凭据和上游认证头。现有本机压测提供对照口径，真实网络、长流条件和跨地域性能另测。[负载与恢复基线](control-plane-acceptance.md)

## 实施顺序与完成标准

| 阶段 | 交付物 | 通过条件 |
| --- | --- | --- |
| P0 上游基线 | 固定来源、许可证、移植清单及可独立编译的协议核心 | 脱离桌面应用运行移植测试；随机分块、跨块 UTF-8、CRLF、工具参数、异常 EOF 和上游错误样本通过。 |
| P1 节点契约 | 节点登录、快照与密文包、限额、尝试与证据上报 | 用同一真实 PostgreSQL/Redis 测试环境对照 Python 模拟器；验证撤权、错集群、提交丢响应、重复事件和租约过期。 |
| P2 两条原生链路 | Messages → Messages、Responses → Responses 的非流式文本 | 各选一个明确支持该端点的部署。两条链路分别用 HTTP 替身验证预留到扣费、重复事件和未知用量核对，再用获授权的真实上游校验计量口径。 |
| P3 两种原生 SSE 与故障 | Messages、Responses 各自的流事件、背压、超时、取消、受控重试和优雅退出 | 对慢客户端、断流、跨块工具、重试后成功逐项核对交付内容、每次成本、客户扣费和剩余预留。 |
| P4 双向转换 | Messages → Responses、Responses → Messages | 每个方向分别验收非流式、SSE、工具和错误映射。转换后的客户端响应与原生基准对照，账务始终由上游原始用量驱动；无法无损表达的必需字段在预留前拒绝。 |
| P5 部署验收 | 独立二进制、运行配置与多节点报告 | 至少复现已有 8/64 节点、热点企业、512 请求突发，再补长流和上游故障。报告 p99、错误/超时/未完成数、进程内存、积压清空和账本一致性。 |

第一项实施工作建议是为两条原生链路各确定一个付费 API 部署、测试模型、计量样本和上界，再据此锁定 P0 移植文件。每次升级 CC Switch 时按固定提交比较导入文件，重跑协议样本和 Nexus 的资金契约测试。
