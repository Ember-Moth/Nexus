# 管理 API

业务接口使用 `/api/v1` 前缀。本地 API 启动后可在 `/docs` 试调，在 `/openapi.json` 查看当前版本的请求模型、响应和必需请求头。生产配置关闭这两个入口；集成客户端可用同版本的开发实例取得契约。

## 登录与授权

调用 `POST /api/v1/auth/sessions`，提交 `login` 和 `password`。响应包含 `access_token`、`token_type` 和 `expires_at`。后续请求使用 `Authorization: Bearer <access_token>`。用 `GET /api/v1/me` 确认身份和角色，结束时调用 `DELETE /api/v1/auth/sessions/current` 撤销会话。管理会话、企业调用 Key 和节点数据库登录各有独立的用途。[初始管理员](development.md#初始管理员) · [用户与企业权限](identity-api.md)

## 按任务查接口

| 资源 | 接入指南 |
| --- | --- |
| 用户、平台角色、企业、成员 | [登录与企业成员管理](identity-api.md) |
| 项目授权、服务账号、调用 Key | [项目与应用凭据](project-credentials.md) |
| 模型、上游凭据、部署与访问策略 | [模型与上游目录](catalog-api.md) |
| 价格、折扣、账户、预算、收款和调整 | [定价与预付费账户](billing-api.md) |
| 支付渠道、充值订单、收据和通知 | [支付网关与自动入账](payments.md) |
| 集群、公钥、节点登记、路由、限额和发布 | [节点接入与配置发布](plane-protocol.md#节点接入与配置发布) |
| 费用、审计、导出、异常核对和解冻 | [查询与运营](operations.md) |

## 写入与重试

- **条件更新**。读取资源返回的 `ETag`，更新时按接口要求原样传入 `If-Match`，例如 `"3"`。收到 412 时重新读取资源，核对变更后重新提交。
- **按自然键写入**。路由策略和限额策略用 PUT 写在其自然键路径上：不带 `If-Match` 只创建并返回 201，已存在时返回 428；带 `If-Match` 时替换该版本并返回 200。[路由与限额](plane-protocol.md#节点接入与配置发布)
- **幂等命令**。需要 `Idempotency-Key` 的接口为每个业务操作生成一个键，允许 1–128 个字母、数字或 `_.:-`。若客户端丢失响应，用同一身份、键、请求体和原版本重试。相同键用于不同参数会得到 409。Key 签发的重试只返回资源引用，明文丢失时需轮换。
- **异步操作**。创建充值订单、发布配置、创建导出任务等请求返回 202。客户端应使用响应中的资源标识查询结果。支付查单、节点配置确认和文件生成各有独立状态。
- **金额与分页**。金额、价格和折扣使用十进制字符串，配置修订号也以字符串传输。账户接口给出权威余额、预留和可用金额。用量汇总另带投影水位。列表返回 `items` 与 `next_cursor`，以 `limit`（1–100，默认 50）控制每页数量，并将 `next_cursor` 作为下一次请求的 `cursor`，保持筛选条件一致；`next_cursor` 为 null 表示没有更多数据。

## 契约约定

OpenAPI 为每个操作声明成功响应的结构，以及该操作可能返回的错误状态码。路径参数按资源命名，例如 `organization_id`、`cluster_id`、`funding_record_id`；请求头使用规范写法 `If-Match` 与 `Idempotency-Key`。

标记为 `deprecated` 的操作保留原有行为，仅供已有客户端过渡，新集成应改用文档给出的替代接口：

| 废弃操作 | 替代操作 |
| --- | --- |
| `POST /api/v1/route-policies` | `PUT /api/v1/route-policies/{organization_id}/{model_id}` |
| `POST /api/v1/organizations/{organization_id}/quota-policies` | `PUT /api/v1/organizations/{organization_id}/quota-policies/{layer}/{scope_type}/{scope_id}/{metric}` |

## 错误处理

业务错误以 `error.code`、`error.message` 和 `error.trace_id` 返回，`trace_id` 与响应头 `X-Request-ID` 一致。请求参数或请求体无法解析时返回 422，`error.code` 为 `validation_error`，`error.fields` 列出各项的 `location` 与 `code`，不回显提交的值；业务规则拒绝的 422 不带 `fields`。OpenAPI 中错误响应分别引用 `ErrorResponse` 与 `ValidationErrorResponse`。支付厂商通知的应答遵循渠道协议。[通知及查单](payments.md)

SDK 可读取 `GET /api/v1/error-codes`，按业务码和 HTTP 状态查 `action` 与 `retry`。开发环境的 OpenAPI 同样提供顶层 `x-error-catalog`，各接口的公共错误，以及认证、支付下单、导出等操作的业务错误，在对应响应描述中提供 `x-error-codes`。这些列表用于处理已声明的情况，客户端应允许未来增加错误码，遇到未知值时保留状态、错误码和请求 ID 供排查。管理 API 下的未知路径返回 `route_not_found`，不支持的方法返回 `method_not_allowed` 并保留 `Allow` 响应头。

| `retry` | 调用方处理 |
| --- | --- |
| `after_correction` | 完成 `action` 指定的操作后再提交，如重新认证、获取新版本、修正配置或充值 |
| `backoff_if_safe` | 等待后再试；429 提供 `Retry-After` 时按秒等待。只重放可安全重复的操作，带幂等键的写入沿用原键 |
| `reconcile_first` | 查询已提交的业务状态；无法确定写入结果时凭请求 ID 核对 |

例如，登录名冲突返回 `login_in_use`，验证邮箱被其他账号占用返回 `email_in_use`。Turnstile 缺少可用配置返回 `turnstile_not_configured`，Siteverify 暂时不可用返回 `turnstile_unavailable`。部署使用的上游凭据尚未就绪返回 `credential_not_ready`。这些业务码替代原先对应场景的通用码。

| 状态码 | 客户端处理 |
| --- | --- |
| 401 | 检查会话是否缺失、已过期或已撤销 |
| 403 / 404 | 核对操作权限、组织归属和项目授权；不可见资源可返回 404 |
| 409 | 按错误码检查幂等参数、状态迁移、收款引用或可用资金 |
| 412 / 428 | 重新获取资源版本，或补充必需的版本与幂等请求头 |
| 422 | 检查必填字段、参数格式、币种、金额精度或版本头格式 |
| 429 | 降低请求频率并稍后重试 |
| 503 | 检查 API 与 Worker 依赖；资金命令若结果未知，保留原幂等键 |
