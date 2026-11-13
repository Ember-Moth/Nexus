# 登录与企业成员管理

M2 第一部分已实现管理会话、平台用户创建、一次性密码设置、组织和成员管理。项目、服务账号和调用 Key 的后续实现见[项目与应用凭据](project-credentials.md)；模型与上游目录仍待实现。

## 启动与升级

沿用[本机开发说明](development.md)启动原生 PostgreSQL/Redis，执行以下升级后启动 API：

```sh
uv run nexus --env-file .env.migrate migrate
uv run nexus --env-file .env.api api
```

迁移 `0002_identity_access` 保留 M1 已有管理员和密码哈希。开发环境可在 `/docs` 查看实际生成的 OpenAPI；生产环境默认关闭文档入口。已存在的数据库升级后不需要重新引导管理员。

## 使用流程

所有业务路径以下列 `/api/v1` 为前缀。管理会话通过 `Authorization: Bearer ...` 传递，返回的令牌只在登录响应中出现。

1. 初始管理员调用 `POST /auth/sessions`，提交 login 和 password，取得管理会话。
2. 管理员调用 `POST /users` 创建用户，返回 pending 用户、一次性 setup_token 和有效期。公开自助注册未开放。
3. 将一次性凭据交给对应用户，用户调用 `POST /auth/password` 提交 token 和新 password。凭据只可兑换一次，成功后用户进入 active。此阶段不自动发送邮件。
4. 管理员调用 `POST /organizations` 创建企业，可通过 owner_user_id 指定已激活的用户为首位 owner；省略时使用当前管理员。
5. 企业 owner 使用自己的账号登录，调用 `POST /organizations/{org}/members` 关联已激活用户，指定 org_owner、org_developer 或 org_finance。
6. 成员通过 `GET /me` 查询自己的身份、平台角色与有效成员关系；`GET /organizations` 只返回其可访问的企业。

新企业保持 provisioning，创建组织、首位 owner 与 `identity.organization_created` outbox 事件在同一事务中提交。组织管理可以使用，但不会伪造计费账户已准备完成；普通管理接口不能直接把 provisioning 改为 active。后续账户事件消费和收费能力由后续阶段接入。

## 已实现接口

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

平台管理员管理用户与企业，但不会隐式获得 platform_finance。平台财务可以读取企业基本信息，不因此拥有用户创建或企业管理权限。企业 owner 管理本企业资料和成员，开发者及企业财务不具有成员管理权限。

企业之外的资源以及不匹配该企业路径的成员 ID 返回 404。每次请求重新查询会话、用户状态、平台角色及相应成员关系；移除成员或撤销角色后，同一旧会话不能继续使用失去的权限。用户停用时会话也不能继续认证，但全局用户停用和平台角色分配的管理接口不属于本次切片。

成员变更锁定组织行并重新检查调用者权限，防止并发操作移除最后一名有效 owner。每个组织与用户最多一条成员关系；已停用关系可使用 PATCH 恢复。归档组织不再接受资料或成员变更。

暂停和归档仅允许平台管理员执行。暂停时保留之前的状态，恢复时只能回到该状态；不能借恢复操作绕过账户准备流程。暂停中的企业成员仍能读取本企业基本状态，但不能管理成员。

管理操作与其审计在 identity 事务内提交。已认证请求的 403/404 拒绝另行保存审计依据，失败业务修改不会部分提交。撤销后的新请求会失败，已经通过授权并开始处理的请求可能完成。

## 凭据与运行参数

会话与一次性凭据均使用 32 字节随机值，数据库只保存 SHA-256 摘要；两类凭据使用不同前缀，不能与未来的数据平面 API Key 混用。密码使用 Argon2id，验证与哈希在有并发上限的线程任务中执行。过期会话和密码凭据在读取时失效，当前不自动清理历史记录。

密码验证采用 argon2-cffi 的标准验证接口；会话生命周期遵循服务端验证、到期与撤销的设计原则。[argon2-cffi 说明](https://argon2-cffi.readthedocs.io/en/stable/howto.html)、[OWASP 会话管理说明](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)

| 参数 | 默认值 |
| --- | --- |
| `NEXUS_SESSION_TTL_SECONDS` | 28800，8 小时 |
| `NEXUS_PASSWORD_SETUP_TTL_SECONDS` | 86400，24 小时 |
| `NEXUS_LOGIN_WINDOW_SECONDS` | 60 |
| `NEXUS_LOGIN_PER_ACCOUNT` | 每窗口 10 次 |
| `NEXUS_LOGIN_PER_IP` | 每窗口 60 次 |

登录与密码兑换受 Redis 原子限流控制，键中不保留明文登录名、IP 或令牌。Redis 不可用时拒绝新的认证请求，已建立会话的业务授权仍以 PostgreSQL 为准。当前使用直接连接的客户端地址，CLI 关闭默认代理头信任，不能用 X-Forwarded-For 绕过限流；真实反向代理的信任配置需在部署时明确。

凭据与管理响应设置 `Cache-Control: no-store`。验证错误只返回字段位置与错误类型，不回显输入中的密码、令牌或底层 SQL 详情；错误响应带可关联的 trace_id。
