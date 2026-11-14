# 管理 API

管理 API 服务平台运营、财务和企业客户的管理操作，统一前缀为 `/api/v1`。本文给出资源与行为契约，不表示路由已经实现；完整 OpenAPI 将在实现阶段由实际模型生成并校验。

已提供身份、企业成员、项目凭据、模型与上游目录及模型访问策略，实际清单见[登录与企业成员管理](identity-api.md)、[项目与应用凭据](project-credentials.md)和[模型与上游目录](catalog-api.md)。M3 已实现的账户、定价、财务授权和账务接口见[定价与预付费账户](billing-api.md)。M3P 已提供通用支付流程与模拟适配器，实际接口见[支付网关与自动入账](payments.md)。M4 网关集群、节点登记、路由配额与配置发布接口见[通信协议](plane-protocol.md)。

支付通知是独立的机器入口 `/webhooks/payments/{channel_id}`，不使用管理会话；按具体渠道声明允许 GET 或 POST，并使用该渠道规定的验签方式。所有其他表中路径仍相对于管理 API 前缀。

网关的请求授权和用量提交通过 [gateway_v1 数据库契约](plane-protocol.md) 完成，不与管理 API 共用身份和权限。

业务 HTTP 接口按需放在所属业务包的 api.py 或相应 *_api.py，调用该包公开用例；进程级 api.py 负责应用生命周期和路由挂载。identity 拥有身份与授权接口，catalog 拥有模型和运行策略接口，billing 拥有价格、金额预算、支付、用量和资金接口，publication 拥有配置发布，reporting 拥有聚合查询与导出。handler 不获得全局 Store 或直接开启跨模块事务。

M5 的汇总、导出、审计和财务处理接口见[查询与运营](operations.md)。

## 认证与授权

首期使用本地用户和可撤销的管理会话。登录后返回高熵的不透明会话令牌，通过 Bearer 传递；数据库只保存摘要、归属、期限和撤销状态。密码使用成熟的密码哈希库处理，登录接口实施速率限制。

初始平台管理员通过部署引导命令创建，不提供默认密码或公开自助注册。企业成员由授权管理员创建或关联已有用户；初始口令设置使用短期一次性凭据，不把长期明文密码写入审计和日志。自动发送邮件、SSO 与外部身份提供者不在首期范围。

每个资源请求同时检查平台角色、组织成员关系和项目授权。URI 中的 organization_id 不是授权凭证，不能通过替换该字段访问其他企业。跨租户对象查询对外返回 404，内部审计保留真实原因。

财务写权限、定价权限、凭据管理权限分别检查。企业成员只能查看自己的售价与折扣，不能读取上游成本表或密钥。数据平面的企业调用 Key 不能登录管理 API。

## 资源与命令

表中的 `{org}`、`{id}` 为路径参数。通用读取和创建分别使用 GET、POST；更新草稿或可编辑资源使用 PATCH；过账、发布、撤销等领域操作使用显式命令。

| 资源或命令 | 方法与路径 | 权限和行为 |
| --- | --- | --- |
| 创建管理会话 | `POST /auth/sessions` | 验证登录凭据，返回有期限的令牌 |
| 注销 | `DELETE /auth/sessions/current` | 撤销当前会话，重复执行无额外副作用 |
| 设置密码 | `POST /auth/password` | 一次性凭据兑换；成功后撤销该用户原有会话 |
| 当前身份 | `GET /me` | 返回当前用户和有效角色，不含凭据 |
| 平台用户 | `GET/POST /users` | 平台管理员管理用户；密码设置使用独立的一次性流程 |
| 重发密码凭据 | `POST /users/{id}/password-reset-tokens` | 平台管理员执行；令原有未用凭据失效 |
| 企业组织 | `GET/POST /organizations` | 平台创建；查询受平台或成员范围限制 |
| 组织状态 | `PATCH /organizations/{org}` | 平台控制冻结；企业修改资料不能改平台状态 |
| 成员 | `GET/POST /organizations/{org}/members` | owner 或平台管理员管理，不得移除最后一个 owner |
| 成员详情与变更 | `GET/PATCH/DELETE /organizations/{org}/members/{id}` | 路径限定租户；变更需 If-Match，DELETE 为停用 |
| 项目 | `GET/POST /organizations/{org}/projects` | owner 创建，读取受项目权限限制 |
| 项目详情与状态 | `GET/PATCH /organizations/{org}/projects/{id}` | owner 或平台管理员改名、停用、恢复或归档 |
| 项目授权 | `GET/POST /organizations/{org}/project-grants` | owner 管理，不能扩大成员组织角色权限 |
| 授权变更 | `GET/PATCH/DELETE /organizations/{org}/project-grants/{id}` | 有效 developer 的项目授权；撤销保留记录 |
| 服务账号 | `GET/POST /organizations/{org}/service-accounts` | 受项目权限限制 |
| 服务账号详情与状态 | `GET/PATCH /organizations/{org}/service-accounts/{id}` | 所属项目不可更改；可停用或恢复 |
| 调用 Key | `GET/POST /organizations/{org}/api-keys` | 创建时返回一次明文，查询只返回标识和策略 |
| Key 详情 | `GET /organizations/{org}/api-keys/{id}` | 返回状态、版本与时间，不返回明文或摘要 |
| 撤销 Key | `POST /organizations/{org}/api-keys/{id}/revoke` | 不影响历史费用归属 |
| 轮换 Key | `POST /organizations/{org}/api-keys/{id}/rotate` | 创建新 Key，明确旧 Key 的重叠有效期 |
| 模型访问策略 | `GET/POST /organizations/{org}/access-policies` | 平台设置上限，企业在其范围内收紧限制 |
| 访问策略详情与修改 | `GET/PATCH /organizations/{org}/access-policies/{id}` | 作用域与层级不可改，只能在权限范围内更新模型集合 |
| 运行限额策略 | `GET/POST /organizations/{org}/quota-policies` | catalog 管理速率与并发，不能突破平台上限 |
| 金额预算策略 | `GET/POST /organizations/{org}/budget-policies` | billing 管理金额与周期预算，与资金预留在同域执行 |
| 公开模型 | `GET /models` | 返回调用者有权看到的模型与计量能力 |
| 模型详情 | `GET /models/{id}` | 客户带组织及可选项目/Key 上下文，拒绝不可见模型 |
| 模型管理 | `POST /models`、`PATCH /models/{id}` | 平台管理员维护公开模型定义 |
| 上游与部署 | `GET/POST /upstreams`、`GET/POST /deployments` | 平台管理员维护，读取不泄露密钥 |
| 上游与部署详情 | `GET/PATCH /upstreams/{id}`、`GET/PATCH /deployments/{id}` | If-Match 条件更新，校验父级状态、协议与凭据归属 |
| 上游凭据 | `POST /upstreams/{id}/credentials` | 接收并加密保存，查询接口不回传明文 |
| 凭据元数据 | `GET /upstreams/{id}/credentials`、`GET /upstreams/{id}/credentials/{credential_id}` | 不返回 secret、密文或根密钥 |
| 撤销凭据 | `POST /upstreams/{id}/credentials/{credential_id}/revoke` | 需要 If-Match，影响关联部署的 configured 状态 |
| 路由策略 | `GET/POST /route-policies` | 平台管理员维护并进行发布校验 |
| 售价版本 | `GET/POST /sale-price-versions` | 平台维护统一售价表和计价维度 |
| 成本版本 | `GET/POST /cost-price-versions` | 平台维护按部署划分的采购成本表 |
| 价格发布 | `POST /sale-price-versions/{id}/publish`、`POST /cost-price-versions/{id}/publish` | 校验维度、币种和生效顺序，发布后内容不可变 |
| 客户折扣版本 | `GET/POST /organizations/{org}/discount-versions` | 平台创建折扣，企业只读自己的规则 |
| 折扣发布 | `POST /organizations/{org}/discount-versions/{id}/publish` | 校验系数与生效顺序，保留历史版本 |
| 客户有效价格 | `GET /organizations/{org}/pricing` | 返回基础售价、折扣、折后展示价和版本，不返回成本 |
| 账户余额 | `GET /organizations/{org}/account` | 返回已记账余额、预留和可用余额 |
| 入账登记 | `GET/POST /organizations/{org}/funding-records` | 财务创建草稿，企业财务可查看允许的登记信息 |
| 入账过账 | `POST /organizations/{org}/funding-records/{id}/post` | 平台财务权限，必须幂等，原子增加余额与流水 |
| 支付类型 | `GET /payment-gateway-types` | 平台管理员读取已注册类型、配置 schema 和测试标识 |
| 支付渠道管理 | `GET/POST /payment-channels`、`PATCH /payment-channels/{id}` | 平台管理员配置，凭据写入后不读回；停用不丢弃在途订单 |
| 支付配置版本 | `GET /payment-channels/{id}/versions`、`POST /payment-channels/{id}/versions/{version_id}/revoke` | 只读脱敏元数据；撤销当前材料前先停用渠道 |
| 可用充值渠道 | `GET /organizations/{org}/payment-methods` | 返回允许使用的渠道、币种与金额限制，不含商户密钥 |
| 充值订单 | `GET/POST /organizations/{org}/payment-orders` | owner 或 org_finance 创建；金额、币种和渠道由服务端校验 |
| 充值订单状态 | `GET /organizations/{org}/payment-orders/{id}` | 分别返回支付、入账和核对状态，必要时返回受控支付入口 |
| 支付补单核对 | `POST /organizations/{org}/payment-orders/{id}/reconcile` | 平台财务触发服务端查单，不接受客户端提交 paid 标志 |
| 支付证据 | `GET /organizations/{org}/payment-orders/{id}/observations`、`GET /organizations/{org}/payment-orders/{id}/receipts` | 平台财务查询已验证的最小证据与实际收款，不返回支付密钥或支付入口参数 |
| 支付风险事项 | `GET /organizations/{org}/payment-cases` | 平台财务查询异常与关联证据；不提供任意改余额命令 |
| 账务调整 | `POST /organizations/{org}/adjustments` | 平台财务权限，必须关联来源、原因和依据 |
| 调整过账 | `POST /organizations/{org}/adjustments/{id}/post` | 校验可调整金额、资金约束并留审计 |
| 资金流水 | `GET /organizations/{org}/ledger` | 只读，游标分页 |
| 请求与费用 | `GET /organizations/{org}/requests`、`GET /organizations/{org}/charges` | 根据组织和项目权限过滤，返回结算状态 |
| 用量汇总 | `GET /organizations/{org}/usage` | 返回统计范围和数据水位，区别最终消费与在途预留 |
| 导出任务 | `POST /organizations/{org}/exports` | 后台执行、限流；下载时再次校验权限 |
| 导出结果 | `GET /organizations/{org}/exports/{id}` | 返回状态或短期受控下载入口 |
| 网关集群 | `GET/POST /gateway-clusters` | 平台管理登记；不等同于自动发放数据库凭据 |
| 配置发布 | `POST /gateway-clusters/{id}/releases` | 校验并创建不可变快照和 outbox，返回发布任务 |
| 发布状态 | `GET /gateway-clusters/{id}/releases/{revision}` | 显示期望、分发、节点应用状态 |
| 审计查询 | `GET /audit-events` | 平台范围或企业自己的授权范围，敏感字段脱敏 |
| 核对队列 | `GET /reconciliation-cases` | 平台财务查看异常来源、状态和处理依据 |
| 核对处理 | `POST /reconciliation-cases/{id}/resolve` | 平台财务提供结构化处理类型与依据，关联生成的结算或调整 |

核对结果通过受审计的结算或调整命令落实，不能提供一个任意改余额的 PATCH 接口。企业创建或修改的策略只能缩小平台许可范围。

核对处理类型首期限制为确认不收费、补充用量证据、重试处理、明确减免。每种类型有独立必填字段和资金约束，处理结果引用生成的费用、调整或释放记录，不允许仅修改异常状态绕过账务处理。

上述核对处理类型针对消费异常。支付异常通过订单查单和引用原收款的调整处理；真实支付结果必须有服务端证据，不能用消费的“确认不收费”命令清除支付差额。支付网关退款与消费退费区分，详见[支付设计](payments.md)。

## 幂等和并发控制

充值订单创建、人工入账、过账、调整、发布、Key 创建及轮换使用 `Idempotency-Key`。幂等范围包含调用身份、组织和操作类型，记录规范化参数摘要与结果引用。同键同参数返回原结果；同键不同参数返回 409。

Key 和一次性令牌的明文仅首次响应返回，幂等存储不保存这些秘密。重试可以返回已创建资源 ID 和 `secret_unavailable` 状态；首次响应丢失后必须显式轮换，不能用创建重试生成多个有效 Key。

已实现的 Key 签发与轮换首次成功返回 201，重试返回 200、secret 为 null、secret_status 为 secret_unavailable。轮换要求 If-Match 与 Idempotency-Key，重试保留原版本和请求体。默认立即撤销旧 Key，可显式设置最多 3600 秒重叠期，且不延长旧 Key 原有到期时间。控制平面现已管理模型访问策略，M4 PostgreSQL 桥接会实时核验这些权限。

财务业务来源的唯一约束不随 HTTP 幂等记录清理而消失。管理员改变幂等键也不能对同一外部收款参考号重复入账。

支付通知不要求第三方携带 Nexus 的 `Idempotency-Key`，通过外部事件、交易和本地订单的业务键去重。通知成功应答必须在可靠接收后发出；响应正文与状态由支付适配器决定，不强制使用管理 API 的统一 JSON。

支付通知 handler 按 Registry 的 NotificationStyle 保留原始 body 或多值参数，再调用 billing 的接收门面。认证与归一化通过一个 parse_notification 入口完成；handler 不自行导入厂商 SDK、不查询订单表、不执行余额更新。

可编辑资源返回 ETag，修改时要求 `If-Match`。版本不匹配返回 412，缺少必需条件返回 428。已发布价格、折扣、快照和已过账流水不开放直接修改。

## 金额和近实时读取

金额、单价、折扣以十进制字符串传输。首期运营币种已确定为 USD：

```json
{
  "currency": "USD",
  "balance": "100.000000000000",
  "held_amount": "1.250000000000",
  "available_balance": "98.750000000000",
  "as_of": "2026-11-08T09:00:03Z"
}
```

账户查询读取权威余额投影，不由有延迟的报表汇总计算。用户应能区分已经扣费、仍在预留和需要核对的请求。正常结算时效按[计费文档](billing.md)测量。

充值订单响应包含 `payment_state`、`credit_state`、`review_required` 和入账流水引用；外部支付成功但尚未入账必须如实显示。客户端查询订单不直接反复调用支付网关，查单由后台任务控制频率。

有效价格接口同时返回售价版本、折扣版本、适用时间和 `discount_factor`。折后单价可以用于展示，但最终扣费必须以基础单价和原始折扣精确计算，不能将展示时舍入后的单价用于记账。

## 返回与错误

创建资源使用 201，同步命令完成使用 200 或 204，异步发布和导出使用 202 并返回任务标识。202 不表示配置已经在所有节点生效，也不表示账务任务已经过账。

创建组织可以返回 201 和 provisioning 状态；billing 账户幂等准备完成后才进入 active。企业不能把组织资源已创建误认为已经可以充值或调用收费接口，查询响应应返回准备状态。

错误响应保持稳定的业务 code，并提供当前 HTTP 请求的 trace_id；不得返回数据库堆栈、密钥或其他客户资料。

```json
{
  "error": {
    "code": "idempotency_conflict",
    "message": "相同幂等键已用于不同参数",
    "trace_id": "trace-example-1"
  }
}
```

| HTTP 状态 | 业务情形 |
| --- | --- |
| 401 | 管理会话缺失、无效或过期 |
| 403 | 已认证但缺少该操作权限 |
| 404 | 资源不存在或不在允许访问的租户范围 |
| 409 | 幂等冲突、重复收款、非法状态迁移、资金约束冲突 |
| 412 / 428 | 资源版本不匹配 / 缺少条件更新前提 |
| 422 | 参数、币种、折扣、价格维度或生效顺序无效 |
| 429 | 管理接口限流，按策略附带重试提示 |
| 503 | 必要依赖不可用，结果未知的变更必须使用原幂等键重试 |

列表使用有上限的游标分页，时间过滤明确包含起点、不包含终点；排序使用时间与 ID 构成稳定顺序。费用查询分别支持发生时间和记账时间，默认值需在响应中说明。

CSV 导出按租户、权限和数据保留策略控制，字符串字段避免被表格软件解释为公式。导出只是数据副本，不能获得修改账本的能力。

## 健康与可观测性

`GET /health/live` 只表示进程存活；`GET /health/ready` 检查此进程必须的依赖和迁移版本。管理 API、发布 Worker、结算 Worker 的健康状态独立报告，不能用 API 正常掩盖结算停滞。

指标与内部诊断端点只对运维开放，不公开租户高基数标识或敏感信息。详细金额与请求来源通过带授权的业务查询和审计查询获取。
