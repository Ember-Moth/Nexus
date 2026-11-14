# 登录与企业成员管理

先在部署主机引导第一个平台管理员，再通过管理 API 创建人员账号和企业。启动命令见[运行指南](development.md#初始管理员)。

## 使用流程

所有业务路径以下列 `/api/v1` 为前缀。管理会话通过 `Authorization: Bearer ...` 传递，返回的令牌只在登录响应中出现。

1. 初始管理员调用 `POST /auth/sessions`，提交 login 和 password，取得管理会话。
2. 管理员调用 `POST /users` 创建用户，返回 pending 用户、一次性 setup_token 和有效期。公开自助注册未开放。
3. 将一次性凭据交给对应用户，用户调用 `POST /auth/password` 提交 token 和新 password。凭据只可兑换一次，成功后用户进入 active。一次性凭据由管理员通过受控渠道交付。
4. 管理员调用 `POST /organizations` 创建企业，可通过 owner_user_id 指定已激活的用户为首位 owner；省略时使用当前管理员。
5. 企业 owner 使用自己的账号登录，调用 `POST /organizations/{org}/members` 关联已激活用户，指定 org_owner、org_developer 或 org_finance。
6. 成员通过 `GET /me` 查询自己的身份、平台角色与有效成员关系；`GET /organizations` 只返回其可访问的企业。

企业创建后处于 `provisioning`。Worker 完成幂等开户，identity 收到账户就绪事件后将企业激活。启用收费调用前确认组织为 `active`、余额和预算足以预留。[账户与预算](billing-api.md)

## 平台角色与企业角色

| 角色 | 作用域 | 主要权限 |
| --- | --- | --- |
| `platform_admin` | 平台 | 用户、组织、平台角色、上游、模型和配置管理 |
| `platform_finance` | 平台 | 人工入账、账务调整、对账与财务导出 |
| `org_owner` | 组织 | 组织成员、项目、服务账号、Key、预算、充值订单和账务查看 |
| `org_developer` | 授权项目 | 服务账号和 Key 管理、模型使用、用量查看 |
| `org_finance` | 组织 | 创建充值订单；查询余额、流水、费用和导出，无直接账本写权限 |

平台管理员不会隐式获得财务写权限；需要执行人工入账时显式授予财务角色。组织内至少保留一位有效 owner。企业成员可以按权限发起支付，但不能直接增加余额、标记已收款、修改销售价或查看上游成本和凭据。

为已激活用户增授平台角色时，先读取 `GET /users/{id}/platform-roles`。用返回的 ETag 作为 `If-Match` 调用 `PATCH /users/{id}/platform-roles`，提交完整角色集合，例如 `{"roles":["platform_admin","platform_finance"]}`。服务保护最后一个有效的平台管理员。

## 接口

| 接口 | 行为 |
| --- | --- |
| `POST /auth/sessions` | 校验密码，创建有期限且可撤销的管理会话 |
| `DELETE /auth/sessions/current` | 撤销当前会话，重复注销不产生额外副作用 |
| `POST /auth/password` | 兑换一次性凭据并设置密码，同时撤销该用户已有管理会话 |
| `GET /me` | 当前用户、平台角色和有效组织成员关系 |
| `GET/POST /users` | 平台管理员查询或创建用户；不返回密码哈希 |
| `POST /users/{id}/password-reset-tokens` | 平台管理员重发一次性凭据；之前未用的凭据立即失效 |
| `GET/POST /organizations` | 按权限列出或创建企业 |
| `GET/PATCH /organizations/{org}` | 查询、改名及平台控制的暂停、恢复和归档 |
| `GET/POST /organizations/{org}/members` | 列出或添加本企业成员 |
| `GET/PATCH/DELETE /organizations/{org}/members/{id}` | 查询、变更角色、停用或恢复成员；DELETE 为保留历史的停用 |

创建和查询单个组织/成员返回 ETag。PATCH 和成员 DELETE 要求 `If-Match` 携带该版本，例如 `"1"`；缺少前提返回 428，版本冲突返回 412。列表使用 limit（1–100）和不透明 cursor，按 created_at 与 id 稳定分页。

## 权限与一致性

企业之外的资源以及不匹配该企业路径的成员 ID 返回 404。每次请求重新查询会话、用户状态、平台角色及相应成员关系；移除成员或撤销角色后，同一旧会话不能继续使用失去的权限。用户停用后，现有会话也会失去访问权限。

成员变更锁定组织行并重新检查调用者权限，防止并发操作移除最后一名有效 owner。每个组织与用户最多一条成员关系；已停用关系可使用 PATCH 恢复。归档组织不再接受资料或成员变更。

暂停和归档仅允许平台管理员执行。暂停时保留之前的状态，恢复时只能回到该状态；不能借恢复操作绕过账户准备流程。暂停中的企业成员仍能读取本企业基本状态，但不能管理成员。

管理操作与其审计在 identity 事务内提交。已认证请求的 403/404 拒绝另行保存审计依据，失败业务修改不会部分提交。撤销后的新请求会失败，已经通过授权并开始处理的请求可能完成。

## 凭据与运行参数

会话与一次性凭据均使用 32 字节随机值，数据库只保存 SHA-256 摘要；两类凭据使用不同前缀，用途分别为管理登录和设置密码。密码使用 Argon2id，验证与哈希在有并发上限的线程任务中执行。过期会话和密码凭据在读取时失效，历史记录按部署策略维护。

| 参数 | 默认值 |
| --- | --- |
| `NEXUS_SESSION_TTL_SECONDS` | 28800，8 小时 |
| `NEXUS_PASSWORD_SETUP_TTL_SECONDS` | 86400，24 小时 |
| `NEXUS_LOGIN_WINDOW_SECONDS` | 60 |
| `NEXUS_LOGIN_PER_ACCOUNT` | 每窗口 10 次 |
| `NEXUS_LOGIN_PER_IP` | 每窗口 60 次 |

登录与密码兑换受 Redis 原子限流控制，键中不保留明文登录名、IP 或令牌。Redis 不可用时拒绝新的认证请求，已建立会话的业务授权仍以 PostgreSQL 为准。当前使用直接连接的客户端地址，CLI 关闭默认代理头信任，不能用 X-Forwarded-For 绕过限流；真实反向代理的信任配置需在部署时明确。

凭据与管理响应设置 `Cache-Control: no-store`。验证错误只返回字段位置与错误类型，不回显输入中的密码、令牌或底层 SQL 详情；错误响应带可关联的 trace_id。
