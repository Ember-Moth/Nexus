# 数据模型

排查业务记录时，先确定写入该记录的模块，再按组织和业务 ID 查关联。下表使用实际 PostgreSQL schema 与表名。数据库的字段、复合外键、检查约束、索引和角色授权以 [Alembic 迁移](../migrations/versions)及其 [SQL](../migrations/sql) 为准。

| Schema | 主要数据与查询线索 |
| --- | --- |
| `identity` | `users`、`admin_sessions`、`platform_role_bindings` 记录用户身份；`memberships` 和 `project_grants` 决定组织、项目范围。`api_keys` 归属于 `service_accounts`，可沿 `access_policies` 和 `access_policy_models` 检查模型权限。 |
| `catalog` | `deployments` 将 `models` 连接到 `upstreams` 和对应的 `upstream_credentials`。`route_policies` 与 `quota_policies` 提供候选部署和运行限额。 |
| `billing` | `accounts` 是企业钱包。`ledger_entries` 保存已过账流水；`request_runs` 和 `request_attempts` 分别保存客户结算与上游尝试成本。支付订单、外部观察、收据和账务核对均归本模块。 |
| `publication` | `gateway_principals` 把数据库登录绑定到节点与集群。`config_snapshots` 以集群和修订号标识内容，`credential_envelopes` 保存密文包，`config_outbox` 跟踪分发，`node_config_acks` 记录节点确认。`cluster_automation` 每个集群一行，记录期望快照对应的源向量、待发布的变化、最近一次自动发布和失败、回滚暂停，以及 Worker 租约。 |
| `reporting` | `request_facts` 保存可重建的费用与用量投影。`export_jobs` 跟踪导出状态、内容与过期时间，`audit_events` 记录导出操作。 |

## 跟踪一笔充值

1. 用组织和订单 ID 找到 `billing.payment_orders`，核对渠道、配置版本、商户订单号及报价。订单的 `collection_amount`、`collection_currency` 是网关收款口径，`credit_amount`、`currency` 是钱包入账口径。EPay 订单创建时锁定 `exchange_rate`。
2. `payment_events` 保存已验签的通知字段和摘要。`payment_operations` 保存下单或查单任务及其租约；`payment_observations` 保存服务端查询事实和所用配置版本。
3. 用 `payment_receipts` 核对外部交易。收款命名空间内的交易号唯一。已入账订单引用 `funding_records` 和 `ledger_entries`，以这两个引用复核资金变动。
4. 退款、争议或交易归属冲突进入 `payment_cases`。需追回资金时，`adjustments` 引用原流水，原收据和入账记录保留。[支付流程](payments.md) · [异常处理](operations.md#财务核对)

## 跟踪一次调用

1. 从 `billing.request_runs.id` 定位请求，核对组织、项目、服务账号、Key、模型及锁定的售价与折扣版本。`reserved_amount`、`reservation_state` 记录资金占用；`budget_holds` 将它关联到各个 `budget_periods`。
2. `request_attempts` 中每次尝试都有自己的成本价格版本。`usage_events` 保存原始完成与关闭事件，`normalized_usage` 关联尝试、归一化规则和数量，`usage_jobs` 记录处理状态。
3. 结算后，`request_runs.charged_amount` 和 `charge_details` 保留扣费与计算明细。非零客户扣费对应一条消费流水。上游采购成本记录在尝试上，后续纠正保存在 `cost_corrections`。
4. `review_cases` 记录消费、成本或账户差异；财务处理决定保存在 `case_decisions`。`report_changes` 记录待同步的请求版本，Worker 将对应事实复制到 reporting。费用汇总有水位，核对余额时以账户、预留和已过账流水为依据。[计量规则](billing.md) · [运营查询](operations.md)

## 生命周期与租户隔离

租户资源带组织 ID，项目、服务账号、Key、预算和资金单据通过复合外键维护归属。人员停用、Key 撤销、模型归档后保留历史费用引用。支付渠道轮换保留历史配置引用和验签版本；密文依赖的历史根密钥由部署方保管。

请求发生、事件接收、资金记账和投影复制分别记录时间。按发生时间与按记账时间查询时，跨期退费可能落在不同期间。导出文件和 Redis 心跳按各自期限过期；原始用量、历史价格、收款事实、资金流水及核对依据由持久化存储保留。部署方确定归档和备份保留期，并验证还原后的账户核对结果。[恢复演练](control-plane-acceptance.md#物理备份恢复)
