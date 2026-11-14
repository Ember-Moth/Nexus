# 查询与运营

API 提供查询和核对命令，Worker 维护报表投影、导出任务和周期性对账。账户余额与流水直接从 billing 读取。

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

`GET /audit-events` 支持 organization_id、action、limit 和 cursor。平台角色可查平台范围，企业 owner 和财务须指定所属组织。审计合并各模块只读记录，返回操作人、时间、动作、对象、受控原因代码和决定引用。自由文本依据在平台财务核对详情内读取。

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

网关新增预留时检查全库 ready 结算任务数。达到 `billing.settings.settlement_backlog_limit` 时返回 `settlement_backlog`。默认阈值为 1024，部署方可在该单行配置中按实测容量设置正整数。已有请求可继续上报、重放或结算，积压下降后自动恢复新预留。该阈值按准入事务可见的已提交事实判断，已授权请求继续产生事件时，队列可以超过阈值。Worker 的 Redis 心跳包含进程累计的数据库连接取得次数与耗时。
