# 查询与运营

API 提供查询和核对命令，Worker 维护报表投影、导出任务和周期性对账。账户余额与流水直接从 billing 读取。

## 凭请求 ID 排查

- 管理 API 的 `X-Request-ID` 对应 `nexus.api` 的 `http.response` JSON 日志。日志记录方法、路由模板、状态、错误码与生成响应头所用的毫秒数。可以用 `GET /api/v1/audit-events?trace_id=<UUID>` 查该请求产生的审计事件，组织用户仍须提供组织 ID。迁移前的记录与独立后台操作允许 `trace_id` 为空。
- 注册、邮箱绑定和密码找回请求实际入队后会写 `mail.queued`，`purpose` 为 `register`、`bind`、`reset`，或表示“登录名已被使用”提示的 `exists`。按原 `request_id` 或 `job_id` 查 Worker 的 `mail.attempt`、`mail.failed`、`mail.accepted` 和 `mail.expired`。失败事件给出尝试次数、渠道、可用的供应商状态码和 `outcome`：`retry_scheduled` 表示服务商不可用、稍后重试；`rejected` 表示服务商同步拒收（例如永久退信或 4xx），任务已删除且不影响 Worker 就绪。`mail.expired` 表示任务在 `NEXUS_MAIL_QUEUE_TTL_SECONDS` 内没有发出。供应商受理后仍可能退信。本地 SMTP 成功以 SMTP 事务完成为准，日志不保存地址、邮件令牌、正文或供应商返回的自由文本。
- Turnstile 不可用时匿名调用方只收到 `turnstile_unavailable`。`nexus.auth` 日志的 `turnstile.unavailable` 事件以 `outcome` 给出原因：`not_configured`（策略开启但 API 缺少 Turnstile 配置）、`invalid_secret`（Siteverify 报告 secret 缺失或失效）、`transport` 或 `malformed_response`。
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

向 `POST /organizations/{org}/exports` 提交 kind 和 filters，取得 202 与任务标识。kind 可选 requests、charges、ledger 和平台财务专用的 attempt_costs。通过同一路径下的 `/{id}` 查看状态，ready 后访问 `/{id}/download` 获取 UTF-8 CSV。`GET /organizations/{org}/exports` 按提交时间从新到旧分页列出任务，可见范围与单个读取一致：组织 owner 和财务只见本人提交的任务，平台角色可见全部，attempt_costs 任务仅平台财务可见。下载重新核验管理会话、组织角色、请求者和筛选范围。过期副本由 Worker 清除。CSV 文本处理表格公式前缀。任务使用短事务和租约代次，过期执行者的结果无法替换新的文件。

默认每组织每分钟可创建 10 个任务，每份最多 10000 行，文件保留 86400 秒。平台管理员在“平台 → 设置 → 报表”或 `PUT /api/v1/settings/reporting` 修改（`exports.per_org_minute`、`exports.max_rows`、`exports.ttl_seconds`），下一次导出即使用新值。文件上限为 16 MiB，超限任务报告失败，可缩短查询期间后重新导出。

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

`POST /reconciliation-cases/upstream-statements` 接受组织、尝试、外部账单参考号、金额、币种、原因与依据，并要求 Idempotency-Key。与当前成本有差异时进入核对，后续纠正独立于客户扣费。手工决定、资金变更和审计同事务提交。

日常账本核对由 Worker 的 `account_reconciliation` 消费者按账户轮巡，每个账户每小时检查一次。发现差异时登记或更新该账户的 `reconciliation_difference` 事项，证据是差异列表；`differences` 计入指标 `nexus_billing_reconciliation_differences`。核对不锁钱包，同一组织的节点预留、开始尝试和结算在核对期间照常进行，分三个事务完成：

1. **领取。** 在 `billing.account_checks`（每个账户一行，创建账户时写入）中用 `SKIP LOCKED` 取最久未检查、且未被领取的账户，写入 10 分钟租约后立即提交。同一账户同一时刻只有一个 Worker 在检查；Worker 中途退出时租约到期，账户重新可领。
2. **检查。** 在 REPEATABLE READ 只读事务中比较余额和占用额投影、流水链、冲正、预算期间与预留、充值和调整记录、请求扣费与退费、支付到账。事务不加行锁。每笔资金事务的投影和事实在同一事务提交，快照对每笔事务要么全部可见要么全部不可见，因此并发预留和结算不会产生误报。
3. **记录。** 结果在一个短事务中写回 `account_checks`，前提是领取凭据仍有效；租约已被其他 Worker 接管时丢弃结果。如果账户事项在快照之后被处理或改写，本次结果不会覆盖它，旧快照不会重新打开已解决的事项。

流水不可修改，序号在账户内唯一，所以逐条检查是增量的：上次通过链、冲正来源与累计、来源记录检查的流水前缀记在 `account_checks.ledger_sequence`，下次只检查之后的新流水，余额用该条的 `balance_after` 加新流水合计比较。某条新流水有问题时，前缀停在它之前，问题每次都会再次报告。请求、预算、充值和调整记录、支付订单仍按整个账户比较，每项一条带索引的查询，只返回有差异的行。请求行会被结算、退费和复核修改，没有按提交顺序记录的变更标记，所以这部分不做增量，耗时随请求数线性增长。本机实测（PostgreSQL 无并发负载）10 万请求的账户检查约 60 毫秒，同一账户全量检查约 130 毫秒；原实现持有钱包行锁检查需 3.3 秒。按这个速度，单条语句约在单账户千万级请求时触及 5 秒的 `statement_timeout`。

手工 `POST /organizations/{org}/account/reconcile`、账户事项的 `retry` 和解冻仍在账户锁内全量比较，作为权威复查。查看核对进度：

```sql
SELECT organization_id, checked_at, differences, ledger_sequence, claim, claimed_until
FROM billing.account_checks ORDER BY checked_at;
```

`claim` 一直不为空、`checked_at` 早于一个多小时以前，说明该账户的检查反复出错，每次租约到期后重试；Worker 日志中有 `consumer=account_reconciliation` 的 `worker.consumer_failed`。

受限账户可在完成核对后调用 `POST /organizations/{org}/account/unfreeze`，提交 reason、evidence、If-Match 与 Idempotency-Key。服务重新检查全部冻结来源和账本差异。新发生的退款、争议或超上界请求可再次冻结账户。

平台管理员可从 `GET /internal/metrics` 查询当前 API 进程请求耗时与错误、连接取得耗时、等待锁数量、配置发布和节点滞后、用量积压与结算延迟、支付延迟、超期预留、重复与冲突事件、核对差异数。耗时样本取最近 24 小时的数据库事实，API 进程计数自启动累计。

`GET /internal/metrics/prometheus` 以 Prometheus 文本格式（0.0.4）返回同一份数据，原 JSON 接口不变。两者使用同一鉴权：单个 `Authorization: Bearer` 管理会话，且必须是平台管理员；缺少凭据返回 401，其他角色返回 403。导出的名称固定，唯一的标签是取值有界的 HTTP 方法，不含组织、项目或 Key 标识。

| 指标 | 类型 | 来源 |
| --- | --- | --- |
| `nexus_api_http_requests_total`、`nexus_api_http_errors_total`、`nexus_api_http_request_seconds_total` | counter，标签 `method` | 当前 API 进程自启动累计；错误指状态码不低于 400 |
| `nexus_api_db_connection_acquisitions_total`、`nexus_api_db_connection_acquire_seconds_total` | counter | 当前 API 进程取得数据库连接的次数和耗时 |
| `nexus_api_db_pool_checked_out`、`nexus_db_waiting_locks` | gauge | 进程连接池占用、全库等待中的锁 |
| `nexus_billing_usage_backlog`、`_usage_waiting_for_facts`、`_usage_review`、`_overdue_holds`、`_consumption_cases_open`、`_payment_cases_open`、`_reconciliation_differences`、`_conflicting_events`、`_duplicate_events` | gauge | 数据库当前计数 |
| `nexus_billing_settlement_backlog_limit` | gauge | 当前 `settlement_backlog_limit`，见[结算 Worker](#结算-worker) |
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

会话按身份策略的 `sessions.session_ttl_seconds` 过期（`/api/v1/settings/authentication`，最长 24 小时），部署方须在到期前用专用的平台管理员账号重新登录并替换该文件。每次抓取会执行与 JSON 接口相同的三组数据库查询，抓取间隔建议不短于 30 秒。`/internal/` 前缀应继续只在内网入口开放。

Worker 的 Redis 心跳包含进程累计的数据库连接取得次数与耗时。结算的扩展、监控和背压见[结算 Worker](#结算-worker)。

### 预留覆盖与核对要点

迁移 `0031_model_call_bounds`、`0032_container_coverage`、`0033_storage_prepayment` 须先于新版 Worker 与 Relay 部署。Worker 在每个账务事务里读取账务策略的 `vector_storage.prepaid_days`（默认 7，范围 1–31）和 `vector_storage.lapse_policy`（`delete` 或 `suspend`，默认 `delete`），由 `PUT /api/v1/settings/billing` 修改；消费者 `storage_prepayment` 负责续期与失效。

- **冻结范围收窄。** 只有费用超过已持有预留时账户冻结。工具或计量单位超出授权值但费用在预留内时，请求以 `authorized_bound_exceeded` 进入核对、不冻结；token 数超出预留依据时照常结算并写入 `request.token_bound_exceeded`，该事件持续出现说明某个部署的上界规则过窄。
- **工具循环的预留更高。** 带服务端工具或压缩的请求按可能的模型调用次数预留输入，资金与 TPM 占用随之上升，规则见[多次模型调用](relay-design.md#首期协议与计量细则)。批任务中含代码执行等无次数参数工具的项目被拒绝。
- **容器扩展预留。** 长时间使用的容器会出现多条 `openai_container_v1` 请求：首个预留和 `billing.container_extensions` 中的扩展预留。会话复核时各自生成核对事项，按时间顺序分配供应商分钟数，每段使用独立的证据引用。`container.coverage_unreserved` 审计事件表示执行后无法补足预留，账户已冻结；`container.coverage_deferred` 表示因结算积压暂缓补足，该会话的下一次操作会先补足预留。
- **向量存储预付。** 每个存储有一条随续期替换的预付预留（被取消的旧预留原因为 `storage_hold_replaced` 或 `storage_hold_applied`）。`vector_storage.renewal_failed` 后存储停止写入，`vector_storage.lapsed` 后按策略删除或停用。
- **故障转移判定。** 厂商错误信封中的 429 一律释放客户预留；只有文档列明的类型会换部署。Relay 只在上一次尝试被证明没有执行（厂商文档列明的拒绝，或请求没有发出）时才开始下一次尝试，并把这样的尝试报为 `declined`：被换掉的尝试，最后一次被拒绝的尝试（没有备用部署、达到集群策略 `relay.max_attempts`，或故障转移预算用尽；该值为 1 时连接未建立的尝试也在此列），以及已登记但没有发出的备用尝试。`declined` 尝试按零成本确认（归一化规则 `relay.declined/1`），不开核对项，请求仍以 `upstream_failed` 关闭；之后又有尝试的 `failed` 尝试也按同一规则确认。其余没有用量的失败尝试，例如未列明类型的 429（记 `failed`）、执行中出错，仍各开一个 `missing_usage` 成本核对项，见[结算 Worker](#结算-worker)。

## 结算 Worker

Worker 的 `settlement` 消费者把 Relay 上报的用量事实结算成扣费和尝试成本，规则见[幂等结算](billing.md#幂等结算)。

**批量结算。** 一个事务取最早到期任务所属的钱包，锁住它和它最多 32 个到期任务，按请求 ID 锁住它们的报表变更行（与报表投影的顺序一致，避免死锁），再用 6 条查询读出这些请求的尝试、成本价、用量事实、售价、折扣和预算占用，然后逐笔做与单笔结算相同的判定和过账，最后一次写入。每笔请求的扣费、流水、预算、审计和核对项都与逐笔结算相同。任何一笔出错时整批回滚，再逐笔重新结算，只有出错的那一笔按退避重试（`billing.usage_jobs.failures`），其余照常完成。热点钱包因此每批只加一次行锁，节点的预留最多等待一批的时长（实测见[Worker 优化后](relay-acceptance.md#worker-优化后)）。

**多副本。** chart 默认 2 个副本，分散到不同节点，并有 `maxUnavailable: 1` 的 PodDisruptionBudget。领取钱包和任务都用 `SKIP LOCKED`，副本之间、副本与节点的预留之间都不互相等待：不同钱包并行结算，同一个钱包同一时刻只由一个副本结算。领取就是事务里的行锁，没有单独的租约：进程退出或连接断开时事务回滚，其他副本立即接管；宿主消失而连接没有关闭时，PostgreSQL 在 `idle_in_transaction_session_timeout`（Worker 连接为 30 秒）后结束该会话并释放锁。乱序、重放、并发领取、同一钱包并发和中途被杀的情形由 `tests/integration/test_settlement_concurrency.py` 覆盖。

**监控 ready 任务数。** 以下指标来自 `/internal/metrics/prometheus`：

| 指标 | 含义 | 建议告警 |
| --- | --- | --- |
| `nexus_billing_usage_backlog` | 到期待结算的 ready 任务数 | 持续 1 分钟超过 `nexus_billing_settlement_backlog_limit` 的一半；达到上限时节点开始拒绝新预留 |
| `nexus_billing_settlement_backlog_limit` | 当前背压阈值 | 用于上面的比值 |
| `nexus_billing_settlement_seconds` | 最近 24 小时最后一条用量证据到扣费的平均秒数 | 超过 2 秒 |
| `nexus_billing_usage_waiting_for_facts` | 等待关闭事件或尝试事实的任务 | 持续增长说明节点上报有缺失 |
| `nexus_billing_usage_review` | 进入核对的任务 | 与流量同比增长时检查核对原因 |

没有 Prometheus 时可以直接查询：

```sql
SELECT count(*) FILTER (WHERE state='ready') AS ready,
       count(*) FILTER (WHERE state='ready' AND failures>0) AS retrying,
       min(next_attempt_at) FILTER (WHERE state='ready') AS oldest_due
FROM billing.usage_jobs;
SELECT ready, observed_at FROM billing.settlement_backlog;  -- 准入使用的计数
```

ready 持续偏高而 Worker 的 CPU 不高，多数是热点钱包：一个钱包同一时刻只能由一个副本结算，增加副本没有帮助。`retrying` 不为 0 说明有请求反复出错，Worker 日志中有 `worker.consumer_failed`。

**背压。** 节点新增预留时，`billing.request_runs` 的触发器读取 `billing.settlement_backlog` 中的 ready 计数，达到 `billing.settings.settlement_backlog_limit` 时返回 `settlement_backlog`（节点返回 503）。计数由预留自己刷新：发现它早于 100 毫秒的那个预留重新计数（最多数到阈值），其他预留用 `SKIP LOCKED` 跳过、沿用上次的值，所以准入不再在企业锁内扫描队列，Worker 全部停止时背压也照常生效。阈值因此最多滞后 100 毫秒，已授权的请求继续产生事件时队列也可以超过阈值。已有请求可继续上报、重放或结算，积压降到阈值以下后自动恢复新预留。默认阈值为 2048，部署方可在该单行配置中设置正整数，按“可接受的清空秒数 × 单个钱包的清空速度”估算：热点钱包只能由一个副本结算，所以不按副本数放大。取值依据见[Worker 优化后](relay-acceptance.md#worker-优化后)。

## 网关配置自动发布

集群的 auto_publish 为 true 时，Worker 会把源变化自动发布到该集群，协议细节见[自动发布](plane-protocol.md#自动发布)。迁移 0037 之前已存在的集群保持 false，仍需手动发布；此后新建的集群默认为 true。

### 生效时延

| 变更 | 生效方式 |
| --- | --- |
| 撤销 Key、冻结或停用企业、收缩访问策略 | 授权时实时检查数据库，提交后立即生效，与自动发布无关 |
| 新签发或轮换的 Key、新模型和部署、路由、限额、扩大的访问策略、价格资格 | 发布后生效。auto_publish 为 true 时，源静默 3 秒后发布，持续变化时最迟 30 秒。分发和节点重新加载另需约 0.02–1.7 秒（1 千至 10 万个 Key 的实测），新 Key 约 4 秒后可用 |
| 新开通企业（`all` 或 `personal` 范围） | 随下一次自动发布纳入。企业还需要平台访问策略、路由和可用余额才能调用 |

静默时间和最长延迟来自集群策略的 `auto_publish.quiet_seconds`（默认 3）和 `auto_publish.max_delay_seconds`（默认 30，不得小于前者）：平台默认值用 `PUT /api/v1/settings/cluster-defaults` 修改，单个集群可用 `PUT /api/v1/gateway-clusters/{cluster_id}/policy` 覆盖，Worker 在下一次检查时使用。兜底轮询间隔 `NEXUS_AUTO_PUBLISH_POLL_SECONDS`（默认 30）是 Worker 的环境变量。缩短静默窗口能减少等待，但持续变更时会产生更多修订，每个修订都要由所有节点重新加载。节点应用旧配置后，只有 5 秒窗口可以继续用旧修订准入。

### 开启、暂停和恢复

- 开启或关闭：`PATCH /api/v1/gateway-clusters/{cluster_id}`，携带 If-Match，请求体为 `{"auto_publish": true}` 或 `{"auto_publish": false}`。关闭后不再自动发布，手动发布接口照常可用。
- 回滚：带 rollback_revision 的手动发布会同时把 auto_publish 置为 false，集群状态显示为 `paused`，`paused` 字段记录回滚生成的修订和时间。这样可以避免 Worker 马上用当前配置覆盖回滚。
- 恢复：确认可以发布当前配置后，PATCH `{"auto_publish": true}`。Worker 会在静默窗口后用当前配置发布一个更高修订，暂停记录也随之清除。

### 状态与排查

`GET /api/v1/gateway-clusters` 的每个集群包含 auto_publish、organization_scope 和 auto_publish_status：

| 字段 | 含义 |
| --- | --- |
| `state` | `disabled` 已关闭；`paused` 因回滚暂停；`pending` 有未发布的变化，等待静默窗口；`failing` 最近一次发布校验失败且源未再变化；`current` 已与源一致；`unknown` 开启后尚未检查 |
| `pending_since` | 第一次未发布变化被发现的时间 |
| `checked_at` | Worker 最近一次检查该集群的时间 |
| `last_published` | 最近一次自动发布的修订、时间和触发它的源变化，例如 `{"identity": ["41", "42"]}` |
| `last_failure` | 最近一次失败的错误码、说明和时间。恢复后仍保留，用于事后追查 |
| `paused` | 回滚暂停的原因、修订和时间 |

`failing` 时按 `last_failure.code` 处理：`invalid_snapshot` 的说明会指出缺少售价或成本价、部署缺少路由，或企业不是 active；`unsupported_capability` 表示有节点缺少所需能力，需要升级节点后重新登记。修正源数据或重新登记节点后，Worker 会自动重试，无需手动发布。如果状态长时间停留在 `pending`，检查 Worker 心跳中 `auto_publish` 是否为 ok，以及数据库通知是否可达。Worker 断开后会退回到每 30 秒轮询一次。

Worker 日志事件：`publication.auto_published`，outcome 为 published 或 unchanged，后者表示内容未变而未生成修订；`publication.auto_publish_failed` 表示校验失败；`publication.auto_publish_deferred` 表示发布与人工操作冲突或源仍在变化，会稍后重试；`publication.auto_publish_error` 表示意外错误或存储不可用。事件都带 cluster_id 和 error_code。`/internal/metrics` 增加以下指标：

| 指标 | 含义 |
| --- | --- |
| `nexus_publication_auto_pending` | 有未发布变化的集群数 |
| `nexus_publication_auto_pending_seconds` | 最早一个未发布变化已等待的秒数 |
| `nexus_publication_auto_failing` | 处于 failing 的集群数 |
| `nexus_publication_auto_paused` | 因回滚暂停的集群数 |

建议告警条件：`auto_pending_seconds` 超过最大延迟加一分钟、`auto_failing` 大于 0，或 `auto_paused` 持续超过约定时间。审计中自动发布的 `config.released` 没有 actor_id，details.actor 为 `auto_publisher`。

## 上游资源清理

部署启用供应商功能后，Key 创建的文件、会话等内容保存在上游账户中，归属和删除规则见[上游资源](project-credentials.md#上游资源)和[上游资源清理](relay-design.md#上游资源清理)。三条清理路径共用 `publication.upstream_purges` 队列：Key 失效超过宽限期（`key_inactive`）、本地句柄保存失败（`orphan`）和组织主动删除（`owner_request`）。队列按上游定位（集群、类型、部署、供应商 ID）唯一，同一副本最多一条任务，谁先排队用谁的，另一方覆盖已有任务。

### 查看队列

平台管理员用 `GET /api/v1/upstream-resources` 跨组织查看，可按 organization_id、project_id（须同时给 organization_id）、api_key_id、kind 和 state 筛选。常用查询：

| 查询 | 用途 |
| --- | --- |
| `state=failed` | 30 次尝试后放弃的清理，副本可能仍在上游 |
| `state=pending` | 待清理和失败后等待重试的任务；`purge.last_error` 和 `purge.next_attempt_at` 给出最后的错误代码与下次尝试时间 |
| `organization_id=<org>&state=retained` | 该组织仍在上游、尚未排队清理的资源 |

组织的删除请求另记在 `publication.purge_requests`，`publication.purge_request_items` 链接它覆盖的任务；进度由这些任务的状态汇总得到，不另存。

### 失败处理

清理失败按指数退避重试，最长每天一次，第 30 次失败后放弃（`failed`）。`last_error` 常见取值：

| 错误代码 | 处理 |
| --- | --- |
| `deployment_unavailable` | 部署已删除，或部署换了凭据（任务绑定原凭据）。恢复部署或原凭据后任务会在下次尝试成功 |
| `credential_unavailable`、`credential_rejected` | 节点取不到或上游拒绝部署凭据，检查上游凭据 |
| `upstream_throttled`、`upstream_unavailable`、`upstream_unreachable` | 上游限流或不可用，通常自行恢复 |
| `batch_in_progress` | 批处理仍在运行，节点已请求取消，下次尝试删除 |

修复原因后，对已放弃的任务再发起一次删除请求即可重新排队：组织所有者在管理后台“上游资源”页操作，或由平台管理员调用 `POST /organizations/{org}/upstream-purges`。新请求把放弃的任务恢复为待清理并从第 1 次尝试开始，原因代码保持不变。

### 审计

| 动作 | 写入方 | 说明 |
| --- | --- | --- |
| `upstream_resource.purge_requested` | 管理 API | 组织或平台管理员提交删除请求；详情含 `request_id`、`api_key_id`、`kind`、新排队数 `queued`、恢复重试数 `retried` 和覆盖数 `covered` |
| `upstream_resource.purged`、`upstream_resource.purge_failed`、`upstream_resource.purge_abandoned` | Relay 节点 | 每个任务的结果，详情含 `purge_id`、`kind`、`reason`、`attempts` 和错误代码 |

审计按组织归属，组织 owner 可在 `GET /audit-events?organization_id=<org>` 中看到。`orphan` 任务没有组织归属，只在平台范围可见。

## Relay 可观测性

### 存活与就绪

| 路径 | 监听 | 含义与用法 |
| --- | --- | --- |
| `GET /live` | 主监听和指标监听 | 进程与事件循环能应答即返回 200，不访问 PostgreSQL 或 Redis。用作存活探针，连续失败才重启进程 |
| `GET /ready` | 主监听 | 最近一次快照刷新和心跳成功且仍有并发余量时返回 200，否则 503。用作负载均衡摘流，不应据此重启 |
| `GET /metrics` | 指标监听 | Prometheus 文本格式指标 |

收到 SIGTERM 后节点开始排空：主监听停止接受连接，`/ready` 和主监听上的 `/live` 不再应答；指标监听继续提供 `/live` 和 `/metrics`，直到在途请求和完成上报结束、进程退出。已打开的 SSE 流和 WebSocket 当前轮次最多再运行 `NEXUS_RELAY_DRAIN_SECONDS`（默认 15 秒），到时连接被关闭，结果按客户端断开或未知上报并进入核对；完成上报另有最多 90 秒。存活探针指向指标监听时，排空期间不会被误判为失效。

### 指标

指标监听由 `NEXUS_RELAY_METRICS_LISTEN` 设置，默认只绑定 `127.0.0.1:9464`，因此企业客户端经入口负载均衡无法访问。该端口不鉴权，生产部署绑定内网地址并限制来源，例如容器内绑定 Pod 地址，用网络策略只放行 Prometheus 和 kubelet。不放在主监听上：入口会把主监听的所有路径转给企业客户端，而指标不应被客户端访问；单独监听也不受请求连接的超时和关闭策略影响，排空时仍可抓取。

标签取值都是固定集合，不使用请求、组织、项目、部署或 Key 标识，序列数量在构建时确定：

- `endpoint`：客户端调用的端点，`anthropic_messages` 或 `openai_responses`
- `transport`：`http`、`sse` 或 `websocket`（Responses 的 WebSocket 轮次）
- `outcome`：前四个值即 `request.closed` 上报的交付结果 `succeeded`、`upstream_failed`、`client_cancelled`、`unknown`；`rejected` 表示在发起上游尝试之前被拒绝，原因包括 Key、请求参数、限额、预算、节点容量和依赖不可用。尝试开始前客户端离开计为 `client_cancelled`，WebSocket 上游握手失败计为 `upstream_failed`
- `dependency` 与 `operation`：`database` 的操作名是 `gateway_v1` 函数名；`redis` 为 `acquire`、`renew`、`finish`、`heartbeat`、`subscribe`
- `kind`：`rejected` 表示网关契约返回了业务错误码（SQLSTATE P0001，例如余额不足），`unavailable` 表示连接、超时或协议失败
- 故障转移的 `reason`：被证明未执行的原因，`connect`（DNS、TCP、TLS 或连接超时）、`signing`（Bedrock 签名失败），或上游拒绝的状态码 `status_429`、`status_503`、`status_529`
- 故障转移的 `result`：之后的处理。`switched` 换到另一部署并提交了新尝试；`no_candidate` 快照内没有符合冻结上界的其他候选；`credential_unavailable` 候选的凭据包无法打开；`start_failed` 续租或 `start_attempt` 失败，新尝试未能提交；`attempt_limit` 已达集群策略 `relay.max_attempts`；`budget_exhausted` 已超过 `relay.failover_budget_ms`；`client_gone` 客户端已离开。除 `switched` 外，客户端都收到这次失败的结果

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
| `purge_id`、`resource_kind`、`provider_id` | 上游清理任务 ID、资源类型和供应商资源 ID；供应商 ID 只是定位符，只在清理日志中出现 |

日志中不会出现企业调用 Key（包括摘要）、上游凭据、集群私钥、数据库或 Redis 口令、请求或响应正文和工具参数；排查时用 `request_id` 关联 billing 中的请求和尝试。

| 事件 `message` | 级别 | 时机 |
| --- | --- | --- |
| `relay starting`、`relay listening`、`metrics listening` | info | 启动，含 `cluster_id`、`version`、`listen` |
| `configuration snapshot applied` / `configuration snapshot rejected` | info / warn | 确认新快照，或以 `unsupported_capability`、`invalid_resources`、`credential_unavailable` 拒绝；被拒的快照在每次刷新重试时再记一次 |
| `control-plane refresh failed` / `control-plane refresh recovered` | warn / info | 每次刷新失败，`reason=heartbeat` 表示 Redis 心跳失败；恢复后的首次成功 |
| `request finished` | info | 每个开始了上游尝试的请求或轮次结束，含最后一次尝试的部署、交付结果、尝试数和耗时 |
| `upstream attempt declined` | warn | 故障转移决策点：一次尝试被证明未执行。含 `request_id`、`attempt`、原部署 `deployment_id`、`reason` 和 `outcome`（取值同指标 `result`）；选出候选时另含 `next_deployment_id` |
| `upstream copy deleted` / `upstream copy not deleted` | info / warn | 一个清理任务的结果，含 `purge_id`、`resource_kind`，失败时含 `reason` |
| `upstream purge claim failed`、`upstream purge not recorded` | warn | 领取或记录清理任务时控制平面不可用 |
| `orphaned upstream resource not recorded` | warn | 本地保存失败、立即删除失败且无法入队；含 `resource_kind`、`deployment_id`、`provider_id`，需人工清理 |
| `attempt set unresolved; request close withheld for review` | warn | 新尝试的提交结果和是否存在都无法确认，请求不提交关闭事件，留待预留到期后核对 |
| `upstream request failed`、`upstream response rejected`、`upstream stream interrupted`、`upstream request interrupted`、`upstream websocket handshake failed`、`upstream websocket turn interrupted` | warn | 上游失败，含 `request_id`、`deployment_id` 和原因 |
| `completion event replay scheduled` / `completion event accepted after replay` | warn / info | 完成事件第一次提交失败开始重放，以及重放后被接受 |
| `completion event conflicts with recorded facts`、`completion event abandoned after replay deadline` | error | 放弃上报，请求需进入核对 |
| `reservation cancellation unconfirmed`、`quota lease finalization unavailable` | warn | 取消预留或释放 Redis 租约未得到确认 |
| `websocket session opened`、`websocket turn ended`、`websocket session closed` | info | WebSocket 会话建立、每轮结束和会话关闭；`websocket turn started` 为 debug |
| `shutdown signal received`、`drain started`、`drain finished` | info | 排空开始与结束，`drain deadline reached; ...` 为 warn |
| `relay stopped with an error` | error | 启动或运行失败，进程以非零状态退出 |

生产环境保持依赖库在 `info` 或更高级别。临时开启 `debug` 排查时，依赖库的字段仍被替换，但事件数量会显著增加。

## 升级到 0036（身份认证滥用防护）

1. 执行迁移后尽快同时替换 API 和 Worker。0036 只改动 `identity.auth_policy` 和 `identity.email_challenges` 两张小表。替换完成前，旧 API 实例仍按旧逻辑接受注册；旧 Worker 不认识新的 `exists` 提示邮件，会把它当作失败重试，直到新 Worker 接手。
2. **自助注册在迁移后关闭**，包括此前已开放注册的部署。需要开放时，先配置发信渠道，再由平台管理员 `PUT /settings/authentication` 提交 `{"registration":true,"email_verification":true,...}`。注册只能与邮箱验证同时开启。
3. **配置 BFF 共享密钥**。凡设置了 `NEXUS_AUTH_PROXY_PEERS` 的 API，必须同时设置至少 32 个字符的 `NEXUS_AUTH_PROXY_SECRET`，否则拒绝启动；在 BFF 上设置同名同值的变量。BFF 没有密钥时不转发客户端 IP，API 回退到按 BFF 地址限流。生成方式见 [BFF 配置](../src/web/README.md)。
4. 拥有平台角色的用户默认不能再用邮件找回密码，改由其他管理员签发 setup token。确需保留时打开身份策略的 `platform_email_recovery`。
5. 按业务量检查发信额度（见[发信限额](identity-api.md#发信限额)）和保留登录名，两者都在身份策略里。默认全局每小时 1000 封。
6. 已有账号不受新的登录名规则影响，可继续用原写法登录。

## 升级到 0037（网关自动发布）

1. 迁移 0037 修改 `publication.gateway_clusters`：加两列，并重建范围 CHECK 约束。这一步要取 ACCESS EXCLUSIVE 锁，表很小，但每次 Relay 调用都会读集群行，所以执行期间网关调用会短暂等待。迁移还会在 identity、catalog、billing 三张 `source_revision` 表上各建一个通知触发器。遇到长事务时，迁移会按 `NEXUS_MIGRATION_LOCK_TIMEOUT_MS` 超时，处理掉长事务后重新执行即可。
2. 同时替换 API 和 Worker。只有新 Worker 才有 `auto_publish` 任务；旧 Worker 继续分发已有修订，不会自动发布。
3. 存量集群的 auto_publish 为 false。逐个确认集群当前快照不是有意保留的回滚，也不是在等发布窗口的内容，然后再用 PATCH 打开。打开后如果内容和源一致，不会产生新修订。
4. 修订数会随自动发布增加。关注 Redis 内存和 PostgreSQL 中 `publication.config_snapshots` 的增长（见[配置快照规模](control-plane-acceptance.md#配置快照规模)），以及 `nexus_publication_auto_*` 指标。Redis 中被取代的快照副本 10 分钟后过期。
5. `all` 和 `personal` 集群只负责下发企业。企业要能调用，仍需要平台访问策略、路由和可用余额。自 0036 起自助注册默认关闭。

## 策略文件导入

`nexus settings import --file PATH [--dry-run]` 把 YAML 或 JSON 策略文件写入各模块的策略表。命令使用 API 进程的配置（`nexus_api` 数据库角色，同一组 Secret/ConfigMap），只连 PostgreSQL，要求数据库已迁移到最新版本。

```yaml
version: 1
groups:
  identity.auth_policy:          # 与 PUT /api/v1/settings/authentication 的请求体同构
    mode: managed
    values:
      sign_in: {per_ip: 100}
      sessions: {session_ttl_seconds: 43200}
  billing.policy:                # /api/v1/settings/billing
    mode: initial
    values: {currency: CNY}
  reporting.export_policy:       # /api/v1/settings/reporting
    mode: initial
    values: {exports: {max_rows: 20000}}
  catalog.quota_defaults:        # /api/v1/settings/resource-quotas
    mode: managed
    values: {vector_stores_per_key: 50}
  publication.cluster_defaults:  # /api/v1/settings/cluster-defaults
    mode: managed
    values: {relay: {max_attempts: 3}, auto_publish: {quiet_seconds: 5}}
```

`values` 可以只写部分字段，没写的字段保持数据库现值。不在文件里的组完全不受影响。单个集群的覆盖值和单个企业的资源数量覆盖属于运行时数据，不进文件。

| 模式 | 写入 | 锁定 |
| --- | --- | --- |
| `initial` | 只在该组从未被修改过（版本仍为 1）时写入文件值，之后由管理员在后台维护；同一文件或改过的文件再次导入都不覆盖 | 解除锁定。已锁定的组声明 `initial` 即解锁，值保持不变 |
| `managed` | 每次导入都用文件值覆盖所列字段；值没变就不写，不产生新版本 | 锁定整组：管理接口的写请求返回 409 `policy_managed_externally`，后台设置页只读并显示文件摘要和导入时间 |

命令先读取并校验文件里的所有组（结构、未知字段、取值范围、组合规则，例如开放注册需要邮箱验证、币种在已有账务数据后不能再改），任何一组不合法就整体拒绝、不写入任何组，退出码 1，stderr 只给出组名和字段路径。全部合法后逐组写入，每个有变化的组推进版本，并在所属模块写一条审计：`actor_id` 为空，`details` 含 `actor: "config-import"`、`file_sha256`、`mode` 和逐字段的 `[旧值, 新值]`。各模块分别提交事务；写入阶段遇到存储故障时，已写入的组保持写入，重新执行即可（幂等）。

stdout 是一行 JSON，`--dry-run` 只输出将会发生的变化：

```json
{"file_sha256": "…", "dry_run": true,
 "groups": {"identity.auth_policy": {"status": "applied", "mode": "managed", "managed": true,
            "changes": {"sign_in.per_ip": [60, 100], "managed": [false, true]}},
            "billing.policy": {"status": "skipped", "mode": "initial", "managed": false, "changes": {}}}}
```

`status` 为 `applied`（有写入，或 dry-run 时将会写入）、`unchanged`（与数据库一致）或 `skipped`（`initial` 模式下该组已被修改过，文件值被忽略）。

### 在 k3s 中执行

把策略文件放进 ConfigMap，用 Helm hook Job 在迁移之后执行导入，安装和每次升级都会运行：

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: nexus-import-policies
  annotations:
    helm.sh/hook: pre-install,pre-upgrade
    helm.sh/hook-weight: "-5"          # migrate Job 用 -10
    helm.sh/hook-delete-policy: before-hook-creation,hook-succeeded
spec:
  backoffLimit: 1
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: import
          image: registry.example.com/nexus-control-plane:1.0.0
          args: ["settings", "import", "--file", "/etc/nexus/policies.yaml"]
          envFrom:
            - secretRef: {name: nexus-api}
            - configMapRef: {name: nexus-config}
          volumeMounts:
            - {name: policies, mountPath: /etc/nexus, readOnly: true}
      volumes:
        - name: policies
          configMap: {name: nexus-policies}
```

先用 `kubectl run` 或同一镜像加 `--dry-run` 检查差异，再升级。Job 失败时 Helm 升级中止，数据库保持原值。要把某组从 Git 移交给后台，在文件里把它改为 `initial` 并升级一次。

## 数据库迁移

### 锁等待与超时

升级时每个修订各自在一个事务中提交，修订取得的锁在提交时释放，不会一直持有到整次升级结束。降级仍在一个事务内完成：某个修订因保留数据而拒绝降级时，整次降级回滚，不会留下已被删除的上层表。迁移连接在取得迁移咨询锁之后设置：

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `NEXUS_MIGRATION_LOCK_TIMEOUT_MS` | 5000 | 语句等待表锁超过该时长即失败，0 表示无限等待 |
| `NEXUS_MIGRATION_STATEMENT_TIMEOUT_MS` | 0 | 单条语句的最长执行时间，0 表示不限 |

`lock_timeout` 防止的是锁排队：`ALTER TABLE` 一旦在长事务后面等待 ACCESS EXCLUSIVE 锁，之后所有读写该表的请求都会排在它后面。升级时超时，当前修订回滚，之前的修订已经提交；排查长事务（`pg_stat_activity`）后重新执行 `nexus migrate` 即可从失败的修订继续。在业务流量下升级时，可以把 `NEXUS_MIGRATION_STATEMENT_TIMEOUT_MS` 设为能接受的最长阻塞时间，让意外的长时间重写失败，而不是长时间锁表。

### 0017 需要维护窗口

`0017_request_correlation` 已经发布，不再改写。它在同一个修订内对 5 张审计表执行 `ADD COLUMN` 和非 CONCURRENTLY 的 `CREATE INDEX`：建索引期间持有 SHARE 锁，阻塞对 `billing.audit_events` 等表的写入，Relay 的 `reserve_request` 和 `start_attempt` 会因此停住。尚未执行 0017 的部署：

- 在维护窗口内升级，或先停止 Relay 流量（从负载均衡摘除节点，或发送 SIGTERM 让节点排空，见下文“存活与就绪”），等在途请求和完成上报结束后再执行迁移；
- 审计表很大时，按行数估算建索引时间，并临时调高 `NEXUS_MIGRATION_LOCK_TIMEOUT_MS`，以免在长事务后面反复超时；
- 执行完 0017 后恢复流量，其余修订可以照常执行。

已经执行过 0017 的部署无需处理。

### 大表迁移规范

- 一个修订只做一件可以整体回滚的事；数据回填与约束、索引分开成不同的修订。
- 加列不带易变默认值（PostgreSQL 11 起常量或 `now()` 这类稳定默认值不重写表）；需要非空约束时，先回填，再以 `NOT VALID` 添加 CHECK 约束并单独 `VALIDATE CONSTRAINT`。
- 业务表上的索引在 `upgrade()` 中用 `nexus.infrastructure.migrations.create_index_concurrently` 建立，删除用 `drop_index_concurrently`；`downgrade()` 不使用它们，以保持降级的原子性。辅助函数会提交修订中之前的语句，在 autocommit 下执行 `CONCURRENTLY`，并在该语句上取消迁移级超时（并发建索引只取 SHARE UPDATE EXCLUSIVE 锁，不阻塞读写，但要等待库内所有更早的事务结束）。中断留下的 INVALID 索引在重跑时会被删除重建。`0036_identity_abuse_limits` 是参考用法。
- 模块角色要对已有的表建外键时，在 `with nexus.infrastructure.migrations.references([...], role):` 块内建，不要直接写 `GRANT REFERENCES`（`tests/test_migration_sources.py` 会检查）。原因是 GRANT 会改写表在 `pg_class` 中的行；如果 autovacuum 的 ANALYZE 已持有该表的 SHARE UPDATE EXCLUSIVE 锁，它会等本事务结束才能写这一行，而随后的外键要等 ANALYZE 释放锁，两者死锁，修订失败。辅助函数先以外键本来就要取的 SHARE ROW EXCLUSIVE 锁住表，再授权，块结束后收回。
- 大批量回填分批提交，每批按主键范围更新，避免长事务和长时间行锁。
- 会被 Relay 访问的表（`billing.*` 的请求、尝试和审计表，`catalog.deployments` 等）上的锁表操作，按 0017 的方式安排维护窗口或先摘流。
