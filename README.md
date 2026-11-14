# Nexus

Nexus 是企业 AI API 的控制平面。平台管理员配置模型、上游和访问权限，企业按项目签发调用 Key。网关为每次调用预留资金，Worker 根据用量结算 USD 预付费账户。充值支持人工登记、Stripe、彩虹易支付 V1/V2 和 Antom。

控制平面由 Python 管理 API 和独立 Worker 构成。PostgreSQL 保存授权、价格、用量和账本，Redis 分发配置并协调限额。Rust 节点 `nexus-relay` 提供 Anthropic Messages 和 OpenAI Responses 的原生 HTTP、SSE 转发，以及 Responses 双端 WebSocket。每个 HTTP 请求和每个 WebSocket 生成轮次独立准入、计量和结算。Python 网关模拟器用于验证两平面协议。

## 从本机开始

按[运行指南](docs/development.md)启动 PostgreSQL 和 Redis，迁移数据库，创建第一个平台管理员，再分别启动 API 与 Worker。本机默认地址为 `http://127.0.0.1:8000`。打开 `/docs`，用 `POST /api/v1/auth/sessions` 登录，将返回的 `access_token` 填入 **Authorize**。调用 `GET /api/v1/me` 可确认身份和角色。

## 使用与接入

| 要做的事 | 文档 |
| --- | --- |
| 接入管理 API，处理认证、幂等重试和版本冲突 | [管理 API](docs/management-api.md) |
| 创建管理员、企业、成员，分配平台角色 | [登录与企业成员](docs/identity-api.md) |
| 创建项目和服务账号，签发、轮换或撤销调用 Key | [项目与应用凭据](docs/project-credentials.md) |
| 配置模型、上游凭据、部署和可见模型范围 | [模型与上游目录](docs/catalog-api.md) |
| 发布价格和折扣，设置预算，登记收款、调整和对账 | [定价与预付费账户](docs/billing-api.md) |
| 接入支付渠道，确认充值、查单和处理退款风险 | [支付网关与自动入账](docs/payments.md) |
| 配置节点、发布快照、调用 SQL 函数、上报用量 | [两平面通信协议](docs/plane-protocol.md) |
| 运行 Rust Relay、配置部署与验证结算 | [Relay 设计](docs/relay-design.md) · [开发计划](docs/relay-development.md) |
| 查询费用、导出 CSV、处理异常和查看指标 | [查询与运营](docs/operations.md) |

## 维护与验证

模块边界与故障行为见[系统架构](docs/architecture.md)。定位资金事实和报表投影时查[数据模型](docs/data-model.md)，复算预留、成本和折后费用时查[计量与账务](docs/billing.md)。压测口径、热点企业实测结果和备份恢复演练见[控制平面验收](docs/control-plane-acceptance.md)。
