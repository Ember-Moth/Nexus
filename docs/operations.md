# 查询与运营

API 提供查询和核对命令，Worker 维护报表投影、导出任务和周期性对账。账户余额与流水直接从 billing 读取。

## 凭请求 ID 排查

- 管理 API 的 `X-Request-ID` 对应 `nexus.api` 的 `http.response` JSON 日志。日志记录方法、路由模板、状态、错误码与生成响应头所用的毫秒数。可以用 `GET /api/v1/audit-events?trace_id=<UUID>` 查该请求产生的审计事件，组织用户仍须提供组织 ID。迁移前的记录与独立后台操作允许 `trace_id` 为空。
- 注册、邮箱绑定和密码找回请求实际入队后会写 `mail.queued`。按原 `request_id` 或 `job_id` 查 Worker 的 `mail.attempt`、`mail.failed`、`mail.accepted` 和待发送任务的 `mail.expired`。失败事件给出尝试次数、渠道、可用的供应商状态码和重试安排。供应商受理后仍可能退信。本地 SMTP 成功以 SMTP 事务完成为准，日志不保存地址、邮件令牌、正文或供应商返回的自由文本。
- Relay 的两个生成接口在普通 HTTP、SSE 和早期拒绝响应中返回服务器生成的 `X-Request-ID`。请求成功预留时，该 UUID 就是计费请求 ID。WebSocket 握手的 ID 是会话 ID；会话内各轮拥有独立的计费请求 ID。HTTP 402 的 `insufficient_funds` 对应账户充值，`budget_exceeded` 对应调整预算。原先两个场景均返回 `insufficient_budget`。
- 管理 API 与 Worker 的 Nexus 应用日志通过 stderr 输出 JSON，可由部署环境采集。日志级别受 `NEXUS_LOG_LEVEL` 控制。请求 ID 由服务端生成，原始请求路径、查询参数、认证头和请求体不写入请求事件。

## 查询

管理接口前缀为 `/api/v1`。组织请求、消费、用量和流水列表支持 project_id、api_key_id、model_id、start、end 和 time_basis。时间参数带时区，包含起点，排除终点。默认按 occurred_at 查询，posted_at 选择记账期间。使用 limit 控制每页数量，将返回的 next_cursor 作为下一次请求的 cursor。充值属于组织，按项目筛选时只返回归属于该项目的流水。

| 接口 | 返回内容 |
| --- | --- |
| `GET /organizations/{org}/requests` 与 `/requests/{id}` | 请求归属、冻结的价格与折扣版本、预留和结算状态 |
| `GET /organizations/{org}/charges` | 客户费用及原始计算明细，消费退费按独立负数记录列示 |
| `GET /organizations/{org}/usage` | 按 organization、project、key、model 或 UTC day 分组的用量、原始费用、退费、净消费及在途占用 |
| `GET /organizations/{org}/upstream-costs` | 平台财务查询尝试成本、成本来源、待确认金额和单独的纠正记录 |
| `GET /organizations/{org}/account` 与 `/ledger` | 权威余额、可用余额及稳定流水序列 |

汇总返回 filters 和 watermark，包含源变化时间、投影时间及待同步请求数。已确认用量按独立计量维度求和。平台财务可指定 include_cost=true 查看成本和毛利。provisional 表示仍有在途请求或待确认成本。客户响应只包含客户侧数据。开发者限于实时获授权项目。组织 owner 和财务可查组织账务并导出。

## 导出和审计

向 `POST /organizations/{org}/exports` 提交 kind 和 filters，取得 202 与任务标识。kind 可选 requests、charges、ledger 和平台财务专用的 attempt_costs。通过同一路径下的 `/{id}` 查看状态，ready 后访问 `/{id}/download` 获取 UTF-8 CSV。下载重新核验管理会话、组织角色、请求者和筛选范围。过期副本由 Worker 清除。CSV 文本处理表格公式前缀。任务使用短事务和租约代次，过期执行者的结果无法替换新的文件。

默认每组织每分钟可创建 10 个任务，每份最多 10000 行，文件保留 86400 秒，可分别通过 NEXUS_EXPORT_PER_ORG_MINUTE、NEXUS_EXPORT_MAX_ROWS 和 NEXUS_EXPORT_TTL_SECONDS 设置。文件上限为 16 MiB，超限任务报告失败，可缩短查询期间后重新导出。

`GET /audit-events` 支持 organization_id、action、trace_id、limit 和 cursor。平台角色可查平台范围，企业 owner 和财务须指定所属组织。审计合并各模块只读记录，返回操作人、时间、动作、对象、受控原因代码和决定引用。自由文本依据在平台财务核对详情内读取。

## 财务核对

平台财务通过 `GET /reconciliation-cases` 查询队列，可过滤组织和状态。详情 `/{id}` 提供证据及历史决定。向 `/{id}/resolve` 提交 action、reason 和 evidence，携带 Idempotency-Key 及包含当前 version 的 If-Match。重复提交使用原请求体和版本。

| action | 依据及效果 |
| --- | --- |
| confirm_no_charge | 对仍占用的请求登记不收费依据，释放账户及原预算期间的预留 |
| supplement_usage | 提供 attempt_id、quantities、normalization_version。请求按冻结售价和折扣计费，成本事项按尝试开始时的成本价计算 |
| waive | 提供相同用量依据及正数 waiver，记录原价和减免额，扣除差额。额外消费仍受账户可用余额和原预算期间约束 |
| retry | 重新排队消费事实、触发支付查单，或用权威明细复查账户差异 |
| confirm_cost | 提供 statement_id，以已登记的同一尝试账单纠正成本，保留原成本和差额记账 |
| verify_payment | 提供 observation_id。核对当前有效的服务端观察、实际收款归属和已过账的反向调整。未追回的款项继续保留在队列中 |

`POST /reconciliation-cases/upstream-statements` 接受组织、尝试、外部账单参考号、金额、币种、原因与依据，并要求 Idempotency-Key。与当前成本有差异时进入核对，后续纠正独立于客户扣费。日常账本核对由 Worker 按账户轮巡，每小时检查一次，发现差异时登记事项。手工决定、资金变更和审计同事务提交。

受限账户可在完成核对后调用 `POST /organizations/{org}/account/unfreeze`，提交 reason、evidence、If-Match 与 Idempotency-Key。服务重新检查全部冻结来源和账本差异。新发生的退款、争议或超上界请求可再次冻结账户。

平台管理员可从 `GET /internal/metrics` 查询当前 API 进程请求耗时与错误、连接取得耗时、等待锁数量、配置发布和节点滞后、用量积压与结算延迟、支付延迟、超期预留、重复与冲突事件、核对差异数。耗时样本取最近 24 小时的数据库事实，API 进程计数自启动累计。

`GET /internal/metrics/prometheus` 以 Prometheus 文本格式（0.0.4）返回同一份数据，原 JSON 接口不变。两者使用同一鉴权：单个 `Authorization: Bearer` 管理会话，且必须是平台管理员；缺少凭据返回 401，其他角色返回 403。导出的名称固定，唯一的标签是取值有界的 HTTP 方法，不含组织、项目或 Key 标识。

| 指标 | 类型 | 来源 |
| --- | --- | --- |
| `nexus_api_http_requests_total`、`nexus_api_http_errors_total`、`nexus_api_http_request_seconds_total` | counter，标签 `method` | 当前 API 进程自启动累计；错误指状态码不低于 400 |
| `nexus_api_db_connection_acquisitions_total`、`nexus_api_db_connection_acquire_seconds_total` | counter | 当前 API 进程取得数据库连接的次数和耗时 |
| `nexus_api_db_pool_checked_out`、`nexus_db_waiting_locks` | gauge | 进程连接池占用、全库等待中的锁 |
| `nexus_billing_usage_backlog`、`_usage_waiting_for_facts`、`_usage_review`、`_overdue_holds`、`_consumption_cases_open`、`_payment_cases_open`、`_reconciliation_differences`、`_conflicting_events`、`_duplicate_events` | gauge | 数据库当前计数 |
| `nexus_billing_payment_query_seconds`、`_notice_receive_seconds`、`_event_receive_seconds`、`_settlement_seconds`、`_payment_confirmation_to_credit_seconds`、`_payment_posting_seconds` | gauge | 最近 24 小时平均值，单位秒；没有样本时省略该行 |
| `nexus_publication_backlog`、`nexus_publication_lag_seconds`、`nexus_publication_lagging_nodes` | gauge | 未分发的配置版本、最旧一项的等待时长、落后于期望修订的活跃节点 |

Prometheus 用平台管理员的会话令牌抓取，令牌写入只有 Prometheus 可读的文件：

```yaml
- job_name: nexus-api
  metrics_path: /internal/metrics/prometheus
  authorization:
    type: Bearer
    credentials_file: /etc/prometheus/nexus-admin-session
  static_configs:
    - targets: ["nexus-api.internal:8000"]
```

会话按 `NEXUS_SESSION_TTL_SECONDS` 过期（最长 24 小时），部署方须在到期前用专用的平台管理员账号重新登录并替换该文件。每次抓取会执行与 JSON 接口相同的三组数据库查询，抓取间隔建议不短于 30 秒。`/internal/` 前缀应继续只在内网入口开放。

网关新增预留时检查全库 ready 结算任务数。达到 `billing.settings.settlement_backlog_limit` 时返回 `settlement_backlog`。默认阈值为 1024，部署方可在该单行配置中按实测容量设置正整数。已有请求可继续上报、重放或结算，积压下降后自动恢复新预留。该阈值按准入事务可见的已提交事实判断，已授权请求继续产生事件时，队列可以超过阈值。Worker 的 Redis 心跳包含进程累计的数据库连接取得次数与耗时。

### 预留覆盖与核对要点

迁移 `0031_model_call_bounds`、`0032_container_coverage`、`0033_storage_prepayment` 须先于新版 Worker 与 Relay 部署。Worker 读取 `NEXUS_VECTOR_STORAGE_PREPAID_DAYS`（默认 7，范围 1–31）和 `NEXUS_VECTOR_STORAGE_LAPSE_POLICY`（`delete` 或 `suspend`，默认 `delete`），新增消费者 `storage_prepayment` 负责续期与失效。

- **冻结范围收窄。** 只有费用超过已持有预留时账户冻结。工具或计量单位超出授权值但费用在预留内时，请求以 `authorized_bound_exceeded` 进入核对、不冻结；token 数超出预留依据时照常结算并写入 `request.token_bound_exceeded`，该事件持续出现说明某个部署的上界规则过窄。
- **工具循环的预留更高。** 带服务端工具或压缩的请求按可能的模型调用次数预留输入，资金与 TPM 占用随之上升，规则见[多次模型调用](relay-design.md#首期协议与计量细则)。批任务中含代码执行等无次数参数工具的项目被拒绝。
- **容器扩展预留。** 长时间使用的容器会出现多条 `openai_container_v1` 请求：首个预留和 `billing.container_extensions` 中的扩展预留。会话复核时各自生成核对事项，按时间顺序分配供应商分钟数，每段使用独立的证据引用。`container.coverage_unreserved` 审计事件表示执行后无法补足预留，账户已冻结；`container.coverage_deferred` 表示因结算积压暂缓补足，该会话的下一次操作会先补足预留。
- **向量存储预付。** 每个存储有一条随续期替换的预付预留（被取消的旧预留原因为 `storage_hold_replaced` 或 `storage_hold_applied`）。`vector_storage.renewal_failed` 后存储停止写入，`vector_storage.lapsed` 后按策略删除或停用。
- **故障转移判定。** 厂商错误信封中的 429 一律记 `failed` 并释放客户预留；只有文档列明的类型会换部署。连接未建立的尝试在 `NEXUS_RELAY_MAX_ATTEMPTS=1` 时也记 `failed`。没有用量的失败尝试只开成本核对项。

## Relay 可观测性

### 存活与就绪

| 路径 | 监听 | 含义与用法 |
| --- | --- | --- |
| `GET /live` | 主监听和指标监听 | 进程与事件循环能应答即返回 200，不访问 PostgreSQL 或 Redis。用作存活探针，连续失败才重启进程 |
| `GET /ready` | 主监听 | 最近一次快照刷新和心跳成功且仍有并发余量时返回 200，否则 503。用作负载均衡摘流，不应据此重启 |
| `GET /metrics` | 指标监听 | Prometheus 文本格式指标 |

收到 SIGTERM 后节点开始排空：主监听停止接受连接，`/ready` 和主监听上的 `/live` 不再应答；指标监听继续提供 `/live` 和 `/metrics`，直到在途请求和完成上报结束、进程退出。存活探针指向指标监听时，排空期间不会被误判为失效。

### 指标

指标监听由 `NEXUS_RELAY_METRICS_LISTEN` 设置，默认只绑定 `127.0.0.1:9464`，因此企业客户端经入口负载均衡无法访问。该端口不鉴权，生产部署绑定内网地址并限制来源，例如容器内绑定 Pod 地址，用网络策略只放行 Prometheus 和 kubelet。不放在主监听上：入口会把主监听的所有路径转给企业客户端，而指标不应被客户端访问；单独监听也不受请求连接的超时和关闭策略影响，排空时仍可抓取。

标签取值都是固定集合，不使用请求、组织、项目、部署或 Key 标识，序列数量在构建时确定：

- `endpoint`：客户端调用的端点，`anthropic_messages` 或 `openai_responses`
- `transport`：`http`、`sse` 或 `websocket`（Responses 的 WebSocket 轮次）
- `outcome`：前四个值即 `request.closed` 上报的交付结果 `succeeded`、`upstream_failed`、`client_cancelled`、`unknown`；`rejected` 表示在发起上游尝试之前被拒绝，原因包括 Key、请求参数、限额、预算、节点容量和依赖不可用。尝试开始前客户端离开计为 `client_cancelled`，WebSocket 上游握手失败计为 `upstream_failed`
- `dependency` 与 `operation`：`database` 的操作名是 `gateway_v1` 函数名；`redis` 为 `acquire`、`renew`、`finish`、`heartbeat`、`subscribe`
- `kind`：`rejected` 表示网关契约返回了业务错误码（SQLSTATE P0001，例如余额不足），`unavailable` 表示连接、超时或协议失败
- 故障转移的 `reason`：被证明未执行的原因，`connect`（DNS、TCP、TLS 或连接超时）、`signing`（Bedrock 签名失败），或上游拒绝的状态码 `status_429`、`status_503`、`status_529`
- 故障转移的 `result`：之后的处理。`switched` 换到另一部署并提交了新尝试；`no_candidate` 快照内没有符合冻结上界的其他候选；`credential_unavailable` 候选的凭据包无法打开；`start_failed` 续租或 `start_attempt` 失败，新尝试未能提交；`attempt_limit` 已达 `NEXUS_RELAY_MAX_ATTEMPTS`；`budget_exhausted` 已超过 `NEXUS_RELAY_FAILOVER_BUDGET_MS`；`client_gone` 客户端已离开。除 `switched` 外，客户端都收到这次失败的结果

| 指标 | 类型 | 标签 | 含义 |
| --- | --- | --- | --- |
| `nexus_relay_requests_total` | counter | endpoint、transport、outcome | 生成请求和 WebSocket 轮次，每个计一次 |
| `nexus_relay_upstream_first_byte_seconds` | histogram | endpoint、transport | 从发出上游请求到收到响应头，WebSocket 为该轮第一个上游帧 |
| `nexus_relay_upstream_duration_seconds` | histogram | endpoint、transport | 从发出上游请求到完成、失败或放弃，包含未收到响应的请求 |
| `nexus_relay_sse_streams_active` | gauge | | 正在向客户端写出的 SSE 响应 |
| `nexus_relay_websocket_sessions_active` | gauge | | 占用节点会话槽位的客户端 WebSocket 会话 |
| `nexus_relay_websocket_turns_active` | gauge | | 占用租户轮次槽位的 WebSocket 活跃轮次 |
| `nexus_relay_control_errors_total` | counter | dependency、operation、kind | 失败的 `gateway_v1` 调用和 Redis 命令 |
| `nexus_relay_snapshot_revision` | gauge | | 节点已安装的配置修订，首次加载前为 0 |
| `nexus_relay_control_plane_healthy` | gauge | | 最近一次快照刷新和心跳成功时为 1 |
| `nexus_relay_draining` | gauge | | 开始排空后为 1 |
| `nexus_relay_failover_decisions_total` | counter | reason、result | HTTP 和 SSE 尝试被证明未执行的次数，按原因和后续处理划分；`result="switched"` 之和即重试次数 |
| `nexus_relay_request_attempts` | histogram | endpoint、transport | 每个请求开始的上游尝试数（桶 1 至 4），在开始过尝试的请求关闭时记录；WebSocket 轮次恒为 1 |
| `nexus_relay_completion_events_pending` | gauge | | 尚未被网关确认的完成上报：失败尝试的 `attempt.finished`，或请求最后的尝试结束与关闭事件 |
| `nexus_relay_completion_event_replays_total` | counter | | 提交失败或结果未知后重放完成事件的次数 |
| `nexus_relay_completion_events_abandoned_total` | counter | reason | 因冲突（`conflict`）或 60 秒重放期限（`deadline`）放弃的完成事件，需要核对 |
| `nexus_relay_capacity_in_use`、`nexus_relay_capacity_limit` | gauge | | 节点并发信号量的占用数与上限（`NEXUS_RELAY_CONCURRENCY`），占用包括在途请求、轮次和待上报任务 |

建议告警：`nexus_relay_control_plane_healthy == 0` 持续一分钟；`completion_events_abandoned_total` 增加；`completion_events_pending` 持续上升；`capacity_in_use / capacity_limit` 接近 1；`requests_total` 中 `unknown` 或 `upstream_failed` 的比例上升；`failover_decisions_total` 中 `switched` 的速率突增，说明某个部署持续拒绝请求；`result` 为 `no_candidate`、`credential_unavailable` 或 `start_failed` 的计数增加。

### 日志

节点把日志逐行写到 stderr，每行一个 JSON 对象，固定包含 `timestamp`（UTC，毫秒）、`level`、`target`、`message`。级别由 `NEXUS_RELAY_LOG` 控制，默认 `info`。其余字段只允许下表名称保留取值；任何其他字段，包括依赖库自带的字段，都写成 `"[redacted]"`。消息是固定文本，不内插变量。`src/relay/tests/logging.rs` 检查源码中每个日志事件只用允许的字段名、消息不插值、取值不来自持有密钥或正文的变量，并禁止 `println!` 一类绕过日志的输出；单元测试另外验证未列入的字段被替换。

| 字段 | 内容 |
| --- | --- |
| `request_id`、`attempt_id` | 与 billing 中请求和尝试一致的 UUID |
| `attempt`、`attempts` | 当前尝试的序号（与 `start_attempt` 的序号一致）、请求开始的尝试总数 |
| `deployment_id`、`next_deployment_id`、`cluster_id`、`node_id`、`session_id` | 部署、故障转移选中的下一部署、集群、节点 UUID 和 WebSocket 会话的随机 UUID |
| `endpoint`、`transport`、`outcome`、`delivery`、`event_type` | 与指标相同的枚举、尝试结果、交付结果、完成事件类型 |
| `reason`、`error` | 固定原因码、网关契约错误码或错误类型的固定描述，不含驱动或上游返回的正文 |
| `revision`、`elapsed_ms`、`replays`、`turns`、`in_flight`、`reused`、`reusable` | 修订号、耗时、计数和布尔值 |
| `listen`、`version` | 监听地址与版本 |

日志中不会出现企业调用 Key（包括摘要）、上游凭据、集群私钥、数据库或 Redis 口令、请求或响应正文和工具参数；排查时用 `request_id` 关联 billing 中的请求和尝试。

| 事件 `message` | 级别 | 时机 |
| --- | --- | --- |
| `relay starting`、`relay listening`、`metrics listening` | info | 启动，含 `cluster_id`、`version`、`listen` |
| `configuration snapshot applied` / `configuration snapshot rejected` | info / warn | 确认新快照，或以 `unsupported_capability`、`invalid_resources`、`credential_unavailable` 拒绝；被拒的快照在每次刷新重试时再记一次 |
| `control-plane refresh failed` / `control-plane refresh recovered` | warn / info | 每次刷新失败，`reason=heartbeat` 表示 Redis 心跳失败；恢复后的首次成功 |
| `request finished` | info | 每个开始了上游尝试的请求或轮次结束，含最后一次尝试的部署、交付结果、尝试数和耗时 |
| `upstream attempt declined` | warn | 故障转移决策点：一次尝试被证明未执行。含 `request_id`、`attempt`、原部署 `deployment_id`、`reason` 和 `outcome`（取值同指标 `result`）；选出候选时另含 `next_deployment_id` |
| `attempt set unresolved; request close withheld for review` | warn | 新尝试的提交结果和是否存在都无法确认，请求不提交关闭事件，留待预留到期后核对 |
| `upstream request failed`、`upstream response rejected`、`upstream stream interrupted`、`upstream request interrupted`、`upstream websocket handshake failed`、`upstream websocket turn interrupted` | warn | 上游失败，含 `request_id`、`deployment_id` 和原因 |
| `completion event replay scheduled` / `completion event accepted after replay` | warn / info | 完成事件第一次提交失败开始重放，以及重放后被接受 |
| `completion event conflicts with recorded facts`、`completion event abandoned after replay deadline` | error | 放弃上报，请求需进入核对 |
| `reservation cancellation unconfirmed`、`quota lease finalization unavailable` | warn | 取消预留或释放 Redis 租约未得到确认 |
| `websocket session opened`、`websocket turn ended`、`websocket session closed` | info | WebSocket 会话建立、每轮结束和会话关闭；`websocket turn started` 为 debug |
| `shutdown signal received`、`drain started`、`drain finished` | info | 排空开始与结束，`drain deadline reached; ...` 为 warn |
| `relay stopped with an error` | error | 启动或运行失败，进程以非零状态退出 |

生产环境保持依赖库在 `info` 或更高级别。临时开启 `debug` 排查时，依赖库的字段仍被替换，但事件数量会显著增加。
