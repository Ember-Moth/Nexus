# 登录与企业成员管理

以下路径均以 `/api/v1` 为前缀。后续请求通过 `Authorization: Bearer <access_token>` 传递会话。

## 自助注册与邀请

1. 用户调用 `POST /auth/registrations`，提交 `login` 和 `password`。登录名去除首尾空格并转成小写，长度为 3–254 个字符，不能含空白。密码长度为 16–1024 个字符。关闭邮箱验证时返回 201，响应含 `access_token`、`expires_at` 和 `personal_organization_id`。已有登录名返回 409。
2. 注册事务同时创建账号、个人组织和绑定人的 `org_owner` 成员关系。默认名称为 `Personal's Organisation`。通过 `GET /me` 可找回个人组织 ID。要改名，先读取 `GET /organizations/{org}`，再以其 ETag 为 `If-Match` 调用 `PATCH /organizations/{org}`，提交 `{"name":"我的工作室"}`。
3. Owner 调用 `POST /organizations/{org}/invitations`，提交目标登录名和角色，例如 `{"login":"alice","role":"org_developer"}`。将响应中一次性展示的 `token` 私下交给对方。邀请有效期为 7 天，向同一组织内同一登录名重发邀请会使旧令牌失效。
4. 受邀者可以先注册，再以自己的会话调用 `POST /invitations/accept`，提交 `{"token":"<邀请令牌>"}`。加入后保留自己的个人组织。Owner 可以列出成员，变更角色或停用成员。已接受的邀请不能用来恢复被停用的成员或重新取得旧角色。

个人组织永久绑定用户 ID。数据库禁止删除、归档、转让该组织，绑定人在该组织中的成员关系必须保持为有效的 `org_owner`。平台引导账号、管理员创建的账号以及升级前已有的账号同样拥有个人组织。加入其他组织时，按目标组织的成员角色授权。

## 邮箱验证、登录和密码找回

客户端先调用 `GET /auth/options` 读取 `email_verification`、`turnstile`、`turnstile_site_key` 和 `email_delivery_available`。邮箱验证开关控制新用户自助注册。已有账号仍可使用原登录名和密码登录，再绑定找回密码用的邮箱。登录名恰好是邮件地址的老账号也需要完成绑定。

| 用途 | 接口 | 请求与结果 | Turnstile action |
| --- | --- | --- | --- |
| 注册或重发注册邮件 | `POST /auth/registrations` | 提交 `login`、`password` 和可选的 `email`。开启验证时必须提供邮箱，也可使用邮箱格式的登录名。关闭验证时立即开户，若显式提交邮箱且已配发信渠道，会发送可选的绑定令牌。开启验证时返回 202 和 `email_verification_required: true` | `register` |
| 确认注册 | `POST /auth/registrations/verify` | 提交邮件中的 `token` 与本次注册的 `password`。成功返回 201，会话和个人组织在同一事务中创建 | `confirm_registration` |
| 登录 | `POST /auth/sessions` | 提交 `login`、`password`，成功返回 201 和会话 | `login` |
| 绑定或更换邮箱 | `POST /me/email` | 携带现有会话，提交 `email` 和当前 `password`，成功受理返回 202 | `bind_email` |
| 确认绑定 | `POST /me/email/verify` | 携带同一账号的会话，提交邮件中的 `token`。成功返回 204，可从 `GET /me` 读取 `verified_email` | `confirm_email` |
| 忘记密码 | `POST /auth/password/forgot` | 提交 `email`。对格式正确的邮箱统一返回 202 和 `{"accepted":true}`，仅向已绑定该邮箱的有效账号发信 | `forgot_password` |
| 重置密码 | `POST /auth/password/reset` | 提交邮件中的 `token` 和新 `password`。成功返回 204 并撤销旧会话和其他重置凭据，随后重新登录 | `reset_password` |
| 管理员签发凭据设置密码 | `POST /auth/password` | 提交管理员签发的 `token` 和新 `password`，成功返回 204 | `set_password` |

启用 Turnstile 后，表中每次请求还要提交与该 action 对应的新 `turnstile_token`。后端向 Siteverify 校验成功标志、业务 action 和预期 hostname。令牌失效或动作不匹配时返回 403，依赖不可用时返回 503。[Cloudflare 服务端校验](https://developers.cloudflare.com/turnstile/get-started/server-side-validation/)

邮箱令牌有效期为 15 分钟，成功兑换后失效。API 将发送任务与验证记录一起写入 PostgreSQL。Worker 领取并发送，加密暂存的令牌在发送成功后清除，失败时在有效期内退避重试。SMTP 和 Cloudflare 在供应商已受理但回执丢失时可能重复投递同一令牌。重发找回密码或绑定邮箱的请求会替换该账号上一次同用途的令牌。

## 认证开关与发送渠道

平台管理员读取 `GET /settings/authentication`，用返回的 ETag 作为 `If-Match` 调用 `PUT /settings/authentication`，提交两个独立开关，例如 `{"email_verification":true,"turnstile":false}`。两项默认关闭。Turnstile 需在 API 进程配置 `NEXUS_TURNSTILE_SITE_KEY`、`NEXUS_TURNSTILE_SECRET_KEY` 和实际展示组件的 `NEXUS_TURNSTILE_HOSTNAME`。

API 与 Worker 配置相同的 `NEXUS_MAIL_PROVIDER`、`NEXUS_MAIL_FROM` 和所选渠道凭据。邮箱验证或密码找回需要运行 Worker。[配置示例](../.env.example)

| 渠道 | `NEXUS_MAIL_PROVIDER` | 渠道配置 |
| --- | --- | --- |
| SMTP | `smtp` | 设置 `NEXUS_SMTP_HOST`。默认端口 587、`NEXUS_SMTP_SECURITY=starttls`。隐式 TLS 使用 `tls` 并设置相应端口。需要认证时同时设置 `NEXUS_SMTP_USERNAME` 和 `NEXUS_SMTP_PASSWORD` |
| Resend | `resend` | 设置 `NEXUS_RESEND_API_KEY` 并使用可发信的地址。Worker 按发送任务 ID 传递幂等键。[Resend Send Email](https://resend.com/docs/api-reference/emails/send-email) |
| Cloudflare Email Service | `cloudflare` | 设置 `NEXUS_CLOUDFLARE_EMAIL_ACCOUNT_ID` 和 `NEXUS_CLOUDFLARE_EMAIL_API_TOKEN`，使用已开通 Email Sending 的发件域。[Cloudflare Email Service REST API](https://developers.cloudflare.com/email-service/api/send-emails/rest-api/) |

## 管理员开户

初始平台管理员由部署主机引导。管理员也可通过 `POST /users` 创建 pending 用户，将返回的一次性 `setup_token` 交给用户调用 `POST /auth/password` 设置密码。用户可随后调用 `POST /auth/sessions` 登录。[初始管理员](development.md#初始管理员)

管理员调用 `POST /organizations` 创建企业，传 `owner_user_id` 可指定已激活的用户为首位 owner，省略时使用当前管理员。组织 owner 可通过 `POST /organizations/{org}/members` 直接添加已激活用户。

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
| `POST /auth/registrations` | 自助注册，返回会话与个人组织 ID |
| `POST /auth/sessions` | 校验密码，创建有期限且可撤销的管理会话 |
| `DELETE /auth/sessions/current` | 撤销当前会话，重复注销不产生额外副作用 |
| `POST /auth/password` | 兑换一次性凭据并设置密码，同时撤销该用户已有管理会话 |
| `GET /me` | 当前用户、个人组织 ID、平台角色和有效组织成员关系 |
| `GET/POST /users` | 平台管理员查询或创建用户；不返回密码哈希 |
| `POST /users/{id}/password-reset-tokens` | 平台管理员重发一次性凭据；之前未用的凭据立即失效 |
| `GET/POST /organizations` | 按权限列出或创建企业 |
| `GET/PATCH /organizations/{org}` | 查询、改名及平台控制的暂停、恢复和归档 |
| `GET/POST /organizations/{org}/invitations` | 分页列出有效待接受邀请，或按登录名和组织角色发出邀请 |
| `DELETE /organizations/{org}/invitations/{id}` | 撤销未接受的邀请 |
| `POST /invitations/accept` | 使用受邀账号会话兑换邀请，返回成员关系 |
| `GET/POST /organizations/{org}/members` | 列出或添加本企业成员 |
| `GET/PATCH/DELETE /organizations/{org}/members/{id}` | 查询、变更角色、停用或恢复成员；DELETE 为保留历史的停用 |

创建和查询单个组织/成员返回 ETag。PATCH 和成员 DELETE 要求 `If-Match` 携带该版本，例如 `"1"`；缺少前提返回 428，版本冲突返回 412。列表使用 limit（1–100）和不透明 cursor，按 created_at 与 id 稳定分页。

## 权限与一致性

企业之外的资源以及不匹配该企业路径的成员 ID 返回 404。每次请求重新查询会话、用户状态、平台角色及相应成员关系；移除成员或撤销角色后，同一旧会话不能继续使用失去的权限。用户停用后，现有会话也会失去访问权限。

成员变更锁定组织行并重新检查调用者权限，防止并发操作移除最后一名有效 owner。每个组织与用户最多一条成员关系；已停用关系可使用 PATCH 恢复。归档组织不再接受资料或成员变更。

暂停和归档仅允许平台管理员执行。暂停时保留之前的状态，恢复时只能回到该状态；不能借恢复操作绕过账户准备流程。暂停中的企业成员仍能读取本企业基本状态，但不能管理成员。

管理操作与其审计在 identity 事务内提交。已认证请求的 403/404 拒绝另行保存审计依据，失败业务修改不会部分提交。撤销后的新请求会失败，已经通过授权并开始处理的请求可能完成。

## 凭据与运行参数

会话、设置密码凭据与邀请令牌分别使用独立前缀和 32 字节随机值，数据库仅保存 SHA-256 摘要。密码使用 Argon2id，验证与哈希在有并发上限的线程任务中执行。过期会话和密码凭据在读取时失效，历史记录按部署策略维护。

| 参数 | 默认值 |
| --- | --- |
| `NEXUS_SESSION_TTL_SECONDS` | 28800，8 小时 |
| `NEXUS_PASSWORD_SETUP_TTL_SECONDS` | 86400，24 小时 |
| `NEXUS_LOGIN_WINDOW_SECONDS` | 60 |
| `NEXUS_LOGIN_PER_ACCOUNT` | 每窗口 10 次 |
| `NEXUS_LOGIN_PER_IP` | 每窗口 60 次 |

注册、登录与密码兑换受 Redis 原子限流控制，键中不保留明文登录名、IP 或令牌。Redis 不可用时拒绝新的认证请求，已建立会话的业务授权仍以 PostgreSQL 为准。当前使用直接连接的客户端地址，CLI 关闭默认代理头信任，不能用 X-Forwarded-For 绕过限流；真实反向代理的信任配置需在部署时明确。

凭据与管理响应设置 `Cache-Control: no-store`。验证错误只返回字段位置与错误类型，不回显输入中的密码、令牌或底层 SQL 详情；错误响应带可关联的 trace_id。
