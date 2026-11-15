# Nexus 管理后台（src/web）

Nexus 控制平面的管理后台。浏览器只和本应用同源通信，由 Next.js 服务端（BFF）持有会话并代理调用管理 API。

| 项 | 选型 |
| --- | --- |
| 框架 | Next.js 16（App Router，Turbopack），React 19，TypeScript strict |
| UI | Tailwind CSS 4，shadcn/ui（Radix 基础组件、Vega 风格），lucide 图标 |
| 数据 | TanStack Query（请求与轮询），TanStack Table（表格），openapi-fetch（类型化客户端） |
| 表单 | react-hook-form + zod |
| 金额 | 字符串原样传递，运算用 big.js，显示时再格式化 |

## 环境要求

- Node.js ≥ 20.9（Next.js 16 的最低要求）。开发和 CI 使用 Node 24，见 `.node-version`。
- pnpm 12（`package.json` 的 `packageManager` 字段固定为 `pnpm@12.18.0`；可用 `corepack enable` 或独立安装的 pnpm）。

## 连接本地 API

管理 API 的启动方法见[运行指南](../../docs/development.md)。在仓库根目录：

```sh
uv sync --locked
LC_ALL=en_US.UTF-8 uv run python scripts/dev_services.py start   # macOS 需设置 LC_ALL，否则 PostgreSQL 报 "postmaster became multithreaded"
uv run nexus --env-file .env.migrate migrate
uv run nexus --env-file .env.api bootstrap-admin --login admin
uv run nexus --env-file .env.api api        # 127.0.0.1:8000，开发环境开放 /openapi.json
uv run nexus --env-file .env.worker worker  # 开户、导出、支付查单依赖 Worker
```

> 仓库位于很深的目录（例如 git worktree）时，`.local/native/socket/.s.PGSQL.<port>` 可能超过 Unix socket 的 103 字节上限，PostgreSQL 无法启动。可先把 `.local/native` 换成指向短路径的符号链接再执行 `start`。

然后在本目录：

```sh
cp .env.example .env.local   # 按需修改 NEXUS_API_URL
pnpm install
pnpm dev                     # http://localhost:3000
```

用 `bootstrap-admin` 创建的账号登录。可选：给本地 API 准备演示数据（授予管理员 `platform_finance`、创建“演示企业”、人工入账 USD 1000.00）：

```sh
NEXUS_API_URL=http://127.0.0.1:8000 NEXUS_ADMIN_PASSWORD_FILE=<存放管理员密码的文件> pnpm dev:seed
```

| 环境变量 | 说明 |
| --- | --- |
| `NEXUS_API_URL` | 管理 API 地址（不含 `/api/v1`），只在服务端使用，默认 `http://127.0.0.1:8000` |
| `NEXUS_COOKIE_SECURE` | 默认 Secure Cookie（浏览器在 `http://localhost` 上也接受）。仅在非 localhost 的明文 HTTP 主机上设为 `false` |
| `NEXUS_WEB_ORIGIN` | 部署在会改写 Host 的反向代理后时，填写对外 Origin，供 CSRF 的 Origin 校验使用 |
| `NEXUS_WEB_CLIENT_IP_HEADER` | 登录与设置密码的客户端 IP 来源。仅在入口代理覆盖该头、且客户端无法绕过代理直连 BFF 时设置，例如 `x-real-ip` |
| `NEXUS_AUTH_PROXY_SECRET` | 与 API 共享的密钥（API 侧同名变量），至少 32 个字符。BFF 用它对转发的客户端 IP 签名；未设置时不转发客户端 IP |

配置按客户端 IP 限流时：

1. 入口代理把已验证的单个客户端 IP 写入 `NEXUS_WEB_CLIENT_IP_HEADER` 指定的头。
2. 生成一个随机密钥（例如 `openssl rand -base64 33`），在 BFF 和 API 上都设为 `NEXUS_AUTH_PROXY_SECRET`。
3. 在 API 上将 BFF 的确切 TCP 来源 IP 加入 `NEXUS_AUTH_PROXY_PEERS`，例如本机部署为 `["127.0.0.1"]`。API 设置了 `NEXUS_AUTH_PROXY_PEERS` 却没有密钥时拒绝启动。

BFF 把地址放进 `X-Nexus-Client-IP`，并附上 `X-Nexus-Client-IP-Signature`：以共享密钥对 `nexus-client-ip:v1:<地址>` 计算的 HMAC-SHA256（base64url，无填充）。API 只在来源 IP 属于白名单、且签名用常量时间比较一致时采信该地址；仅凭来源 IP 不够，因为与 BFF 同机的反向代理（例如透传自定义请求头的 nginx）会让客户端伪造的头以白名单地址到达 API。签名缺失、错误或地址无效时沿用直连 peer。限流按 IPv4 地址或 IPv6 的 /64 网段计数。账户维度的 Redis 限额继续生效；默认配置下经过同一 BFF 的登录共享该 peer 的限额。

## 会话与安全

```
浏览器 ──同源──▶ /api/session              登录（可带 Turnstile 令牌）、取 CSRF token、退出
       ──同源──▶ /api/nexus/api/v1/...     代理到 NEXUS_API_URL，附加 Bearer token
       ──同源──▶ /api/password             兑换一次性凭据（POST /auth/password）
       ──同源──▶ /api/auth/options         注册、邮箱验证与 Turnstile 开关（GET /auth/options）
       ──同源──▶ /api/auth/register        注册（成功即写入会话 Cookie）
       ──同源──▶ /api/auth/register/verify 邮箱验证后完成注册（同上）
       ──同源──▶ /api/auth/forgot-password 申请重置令牌
       ──同源──▶ /api/auth/reset-password  用令牌重置密码
```

- **access_token 不进入浏览器 JavaScript**。`POST /api/session` 调用 `POST /auth/sessions`，把 token 写入 `__Host-nexus_session`（httpOnly、Secure、SameSite=Lax、到期时间与会话一致），响应体只返回 `expires_at` 和 CSRF token。
- **CSRF**：登录时另发 `__Host-nexus_csrf`（httpOnly、SameSite=Strict）。前端通过同源的 `GET /api/session` 取得该值，写操作带 `X-CSRF-Token`；服务端做常量时间比较，并要求 `Origin`（或 `Sec-Fetch-Site`）为同源。登录本身也校验 Origin。
- **代理白名单**：只转发 `/api/v1/...`，拒绝 `auth/*` 与非法路径段；请求头只转发 `Content-Type`、`Accept`、`If-Match`、`Idempotency-Key`，响应头只回传 `ETag`、`Content-Type`、`Content-Disposition`、`Retry-After`、`X-Request-Id`，并统一 `Cache-Control: no-store`。
- **401 的区分**：API 对“请求体里的令牌无效”（邮箱验证令牌、邀请令牌）和“会话失效”都返回 401 `unauthorized`。代理收到 401 时用同一 token 探测 `GET /me`：会话仍有效则保留 Cookie 并加 `X-Nexus-Session-Valid: 1`，前端不登出、只显示“令牌无效”；否则清除 Cookie，前端回到登录页。
- **公共认证路由**（`src/lib/server/public-auth.ts`）：同样校验 Origin，按白名单转发请求体，转发头由 `authRequestHeaders()` 生成（客户端 IP 等，见 `src/lib/server/auth-headers.ts`）；注册成功时从响应中取出 `access_token` 写入 Cookie，响应体不含 token。
- **退出**：`DELETE /api/session` 调用 `DELETE /auth/sessions/current` 撤销服务端会话，并无条件清除 Cookie。
- `src/proxy.ts` 只负责把没有会话 Cookie 的页面访问重定向到 `/login?next=…`（`/login`、`/setup-password`、`/register`、`/forgot-password`、`/reset-password` 公开）；授权仍由 BFF 和 API 逐个请求判断。
- **Turnstile**：`/auth/options` 返回 `turnstile: true` 时，登录、注册、找回和重置密码表单显式渲染 Cloudflare Turnstile，令牌随请求提交，失败后重置组件。
- 安全响应头见 `next.config.ts`（`X-Frame-Options: DENY` 等）。Cache Components 保持关闭：页面都依赖会话，且路由保活会让一次性显示的 Key 明文在离开页面后仍留在 DOM 中。

## API 约定的封装

| 约定 | 位置 | 做法 |
| --- | --- | --- |
| 类型化请求 | `src/lib/api/client.ts` | `nexus.GET/POST/...`（openapi-fetch，路径、参数、请求体均来自生成类型）；`call()` 把错误信封转成 `ApiError` 并返回 `{data, etag, status}` |
| 错误与 trace_id | `src/lib/api/errors.ts`、`src/components/feedback.tsx` | `ApiError{status, code, message, traceId, fields}`；`ErrorAlert` 和 `useErrorToast` 显示可复制的追踪 ID，已知错误码映射到文案键 |
| ETag / If-Match | 各写操作 | 打开对话框时读取单体资源拿 ETag，提交时原样放入 `If-Match`；412 显示 `ConflictAlert`，重新加载后再提交 |
| Idempotency-Key | `useIdempotentMutation`（`src/lib/api/hooks.ts`） | 每个业务操作生成一个键；变量（含 If-Match 版本）不变时重试复用同一个键，结果未知（网络错误、502–504、429）自动重试；成功或关闭对话框后换新键 |
| 202 轮询 | `pollUntil()` | 2 秒 → 5 秒 → 10 秒逐步放慢，到终态停止；用于充值订单和导出任务 |
| 游标分页 | `useCursorList()` | `next_cursor` 作为下一页的 `cursor`，筛选条件放进 query key 保持一致；页脚“加载更多”，不显示总数 |
| 金额 | `src/lib/money.ts`、`src/components/money.tsx` | 不转浮点；显示千分位和两位小数，后续有效小数以浅色补在后面，不四舍五入；输入按渠道精度校验，超精度直接报错 |
| 401 | `onUnauthorized()` + `AuthGuard` | 会话失效后清空缓存，回到 `/login?expired=1&next=…`；带 `X-Nexus-Session-Valid: 1` 的 401 不登出 |
| 错误码文案 | `errors.codes.*` | 契约中的错误码都有中文文案；503 优先用具体错误码；`invalid_snapshot` 等配置类错误在文案后附服务端原因 |
| 表格操作列 | `DataTable` | id 为 `actions` 的列固定在右侧，横向滚动时操作按钮始终可点 |

## API 类型生成

```sh
NEXUS_API_URL=http://127.0.0.1:8000 pnpm api:types   # 拉取 /openapi.json，更新 openapi/openapi.json 和 src/lib/api/schema.d.ts
pnpm api:types --from-snapshot                       # 不连 API，按已提交的快照重新生成
pnpm api:types:check                                 # CI：生成结果必须与已提交文件一致
```

契约快照和生成文件都已提交。`/openapi.json` 只在开发环境开放。

`src/lib/api/types.ts` 只是生成类型的别名，没有手写的响应类型。契约变化后重新运行 `pnpm api:types`，`pnpm typecheck` 会指出受影响的调用（路径模板键、`If-Match` 等请求头名称都来自契约）。

路由策略和限额只用自然键上的 `PUT` 写入：不带 `If-Match` 只创建（201；已存在返回 428，界面提示到列表中编辑），带 `If-Match: "<version>"` 才更新（200；版本过期返回 412，界面显示冲突提示）。已废弃的 `POST /route-policies` 和 `POST /organizations/{organization_id}/quota-policies` 不再使用。

## 文案与多语言

界面文字集中在 `src/i18n/messages/zh-CN.ts`，按页面分组；体量较大的区域拆到 `zh-CN/` 下（`ws` 企业工作区、`pc` 平台目录与价格、`pf` 平台财务、`id` 身份），在 `zh-CN.ts` 中合并；`en.ts` 目前只翻译了公共部分，缺失的键回退到中文。组件里用 `useT()("keys.issueDialog.title")`，键名有类型检查。语言保存在 `nexus_locale` Cookie，登录页和用户菜单可切换。

## 页面状态

“已完成”表示在本地 API 上用浏览器走通了主要操作；“已实现”表示页面完整，但本地缺少数据或外部依赖，只验证了部分路径。

### 身份（公开页面与账号）

| 页面 | 状态 | 说明 |
| --- | --- | --- |
| 登录 `/login` | 已完成 | 登录名/密码错误（含追踪 ID）、429、会话过期提示、`next` 跳转、语言切换；Turnstile 开启时渲染组件；“忘记密码”“注册账号”入口 |
| 设置密码 `/setup-password` | 已完成 | 兑换一次性凭据 |
| 注册 `/register` | 已完成 | 两次密码校验、登录名占用（409）；无需邮箱验证时直接进入新建的个人企业。`registration_closed` 显示“当前不开放自助注册”，待身份加固会话加上开关后联调 |
| 完成注册 `/register/verify` | 已实现 | 令牌可从 `?token=` 带入或粘贴；本地没有发信渠道，只验证了无效令牌提示 |
| 找回密码 `/forgot-password` | 已实现 | 结果统一显示为已受理；本地未配置发信，验证了 `mail_unavailable` 提示和邮箱格式错误 |
| 重置密码 `/reset-password` | 已实现 | 密码长度、两次输入校验；验证了无效令牌提示（不会登出） |
| 账号与邮箱 `/account` | 已实现 | 登录名、平台角色、个人企业入口；绑定邮箱分“发送令牌”“确认绑定”两步。本地无发信，验证了不可用提示和无效令牌（会话保留） |
| 接受邀请 `/invitations/accept` | 已完成 | 以受邀账号接受，显示加入的角色并进入企业 |

### 企业工作区

| 页面 | 状态 | 说明 |
| --- | --- | --- |
| 应用外壳、概览、调用 Key、服务账号、钱包与流水、限额 | 已完成 | 见上一阶段 |
| 用量 `usage` | 已实现 | 按企业/项目/Key/模型/日汇总、时间范围；本地无网关流量，只验证了空状态 |
| 请求明细 `requests` | 已实现 | 同上 |
| 可用模型与价格 `models` | 已完成 | 当前企业可用的模型、协议、上限与生效售价（含折扣），价格详情抽屉 |
| 费用明细 `charges` | 已实现 | 筛选与分页；导出任务已走通（排队 → 就绪），本地无扣费数据 |
| 充值 `recharge` | 已实现 | 用 mock 渠道创建订单并拿到支付入口；未在 mock 收银台完成付款 |
| 预算 `budgets` | 已完成 | 新建（层级、范围、周期、限额）、修改限额与启停（If-Match）、周期用量 |
| 成员 `members` | 已完成 | 邀请（一次性链接与令牌，确认保存后才能关闭）并以受邀账号接受；修改角色、停用、移除与撤销邀请已实现，未在浏览器逐一走查 |
| 访问策略 `policies` | 已完成 | 平台/企业/项目/Key 各层策略，下级只能收窄；新建与修改（If-Match、412） |
| 项目 `projects` | 已完成 | 新增“开发者授权”（授权/撤销开发者）和“模型访问策略”（继承的上级策略 + 本项目及其 Key 的策略）页签 |
| 审计与导出 `audit` | 已完成 | 审计事件；导出任务改用服务端列表（`GET …/exports`），刷新页面后仍可见 |

### 平台管理

| 页面 | 状态 | 说明 |
| --- | --- | --- |
| 企业 `organizations`、路由策略 `routes` | 已完成 | 见上一阶段 |
| 用户与平台角色 `users` | 已完成 | 新建用户并签发一次性凭据、重新签发、平台角色（未激活用户会被拒绝，提示明确）；认证设置卡片（邮箱验证、Turnstile 开关）只读取未提交 |
| 模型 `models` | 已完成 | 列表（按状态、按企业视角预览）、新建（协议、计量维度、工具维度、上限），配置前提检查 |
| 上游与凭据 `upstreams` | 已完成 | 列表与详情；凭据新建、撤销（输入名称确认）与轮换步骤提示；新建/编辑上游表单未在浏览器提交 |
| 部署 `deployments` | 已完成 | 新建（模型、上游、凭据联动），Relay 档案编辑器按 `RELAY_FIELDS` 配置渲染，可“按上游填充建议值”；后续新增能力开关只需在 `src/components/platform-catalog/relay-profile.ts` 加字段 |
| 售价版本 `sale-prices` | 已实现，待联调 | 版本时间线、草稿编辑、立即/定时发布。列表接口当前 500，页面显示降级提示并保留本次会话创建的版本；已验证新建草稿 |
| 成本价版本 `cost-prices` | 已实现，待联调 | 同上；已验证新建草稿、启用价格被拒（部署停用）、改为停用价格后发布 |
| 企业折扣 `discounts` | 已完成 | 按企业的版本时间线，新建草稿、编辑、立即/定时发布 |
| 收款账户 `receiving` | 已完成 | 新建、编辑、启停 |
| 人工入账 `funding` | 已完成 | 选择企业 → 账户摘要；登记草稿、过账（确认对话框，Idempotency-Key + If-Match） |
| 调整 `adjustments` | 已完成 | 选择原流水、金额上限提示、草稿、过账 |
| 核对事项 `reconciliation` | 已实现 | 待处理/已处理队列、详情与处理表单、登记上游账单；本地无核对事项，只验证了空状态与表单 |
| 支付异常 `payment-cases` | 已实现 | 支付异常与支付订单两个页签，订单详情含查单记录与“请求查单”；本地无异常 |
| 支付渠道 `channels` | 已完成 | 按适配器 schema 生成配置表单（也可切到 JSON），新建 mock 渠道并启用 |
| 集群与发布 `clusters` | 已完成 | 新建集群（企业、部署、公钥）、编辑范围与启停、轮换公钥；登记节点（数据库角色、能力）；发布当前资源或回滚（202 后进度抽屉轮询）；详情页节点与发布历史，系统发起的发布显示为“系统（自动发布）” |
| 审计事件 `audit` | 已完成 | 按企业、动作、追踪 ID 筛选，操作人与企业显示名称 |
| 运行指标 `metrics` | 已完成 | 经 `GET /api/metrics`（仅平台管理员）读取 `/internal/metrics`，按 API 请求、数据库、发布、用量、支付、异常分组，15 秒刷新 |

### 本地联调准备

- 支付：仓库根目录 `scripts/mock_payment_provider.py` 启动 mock 收银台；API 和 Worker 需要同一组 `NEXUS_MOCK_*`、`NEXUS_PAYMENT_*` 环境变量。在“支付渠道”新建 `mock` 渠道时，商户标识、API Key 与 Webhook 密钥要与 mock 服务的启动参数一致。
- 节点登记要求数据库里已有专用登录角色：只属于 `nexus_gateway`、有 LOGIN、无其他权限，例如 `CREATE ROLE nexus_gateway_dev LOGIN; GRANT nexus_gateway TO nexus_gateway_dev;`。
- 发布要求集群范围内每个部署都被所选企业的路由策略引用，否则返回 `invalid_snapshot`（界面会显示服务端原因）。

## 新增的后端只读接口

均在 `publication`、`reporting` 模块内，带集成测试并已写入契约快照：

| 接口 | 说明 |
| --- | --- |
| `GET /api/v1/gateway-clusters/{cluster_id}` | 集群单体，带 ETag，供编辑使用 |
| `GET /api/v1/gateway-clusters/{cluster_id}/principals` | 节点列表（游标分页）：节点 ID、启用、已应用修订、最近确认、数据库角色、能力 |
| `GET /api/v1/gateway-clusters/{cluster_id}/releases` | 发布历史（游标分页）：修订、SHA-256、时间、发起人（可为空，表示系统）、回滚来源、副本写入情况 |
| `GET /api/v1/organizations/{organization_id}/exports` | 导出任务列表（游标分页）；非平台角色只看到自己发起的任务，`attempt_costs` 只对平台财务可见 |

## 发现的 API 缺口

前端已做降级（显示 ID、提示或本地记忆），以下按影响排序：

1. **价格版本列表 500**：`GET /sale-price-versions`、`GET /cost-price-versions` 抛出 `TypeError`（`pricing.list_prices` 把 `kind` 放进 `**filters`）。计费会话修复后需复查两个价格页的时间线。
2. **令牌错误与会话失效同为 401 `unauthorized`**：邮箱验证、邀请等令牌无效时，前端只能靠代理额外探测 `/me` 区分。建议改用独立错误码（如 `invalid_token`）。
3. **个人企业同名**：迁移 `0015` 把所有个人企业命名为 “Personal's Organisation”，平台侧企业选择器只能追加短 ID 区分。建议用登录名生成名称。
4. **名称与 N+1**：费用明细、用量、审计、流水只返回项目、Key、模型、用户 ID；平台财务不能列用户或 Key，只能显示 ID。项目授权不带成员登录名（前端用成员列表补齐）。
5. **列表筛选缺失**：访问策略、项目授权不能按项目筛选；核对事项列表没有“全部状态”，事项类型与状态枚举不在契约中；审计事件只能按时间正序。
6. **单体接口缺失**：没有 `GET` 单个折扣版本、单个收款账户；核对事项详情不返回 ETag。
7. **用户与平台角色**：`UserView` 不含平台角色，用户列表不能显示角色列（对话框逐个读取）。
8. **状态操作缺失**：访问策略不能删除或恢复继承；调整草稿不能取消。
9. **契约细节**：`ChannelPatch.verification_grace_seconds` 在契约中为必填，但未配置验签材料时提交会被拒；渠道配置只返回 `configured` 布尔值；价格的 `unit_quantity` 未说明单位；价格版本没有按范围的序号；`relay_profile` 校验失败只返回 `value_error`，没有字段路径；`/auth/options` 还没有“是否开放注册”字段（由身份加固会话加入）。
10. **其他**：`mail_unavailable` 用 503，前端需特殊处理；`/internal/metrics` 不在 OpenAPI 中，BFF 单独代理；发布记录不保存系统发起人（`actor_id` 为空即视为系统）；`node_config_acks` 缺少 `(node_id, created_at)` 索引，节点列表的“最近确认”在数据量大时会变慢。

## 检查

```sh
pnpm lint            # ESLint，零警告
pnpm typecheck       # next typegen + tsc --noEmit
pnpm api:types:check # 生成类型与契约快照一致
pnpm build           # 生产构建
```

CI 在 `.github/workflows/check.yml` 的 `web` job 中依次运行以上命令。

## 目录

```
src/
  app/
    api/session, api/nexus/[...path], api/password   BFF 路由
    api/auth/*, api/metrics                          公共认证路由、运行指标代理
    login, setup-password, register, forgot-password, reset-password   公共页面
    (console)/account, (console)/invitations/accept  账号与邮箱、接受邀请
    (console)/o/[orgId]/...                          企业工作区
    (console)/platform/...                           平台管理
  components/   ui/（shadcn）、shell/（导航外壳）、data-table、feedback、money、turnstile …
                workspace/、platform-catalog/、platform-finance/  各区域共用的组件
  i18n/         文案与语言切换
  lib/api/      生成类型及其别名、客户端、hooks
  lib/server/   仅服务端使用的配置与会话工具
  proxy.ts      未登录重定向
openapi/        契约快照
scripts/        类型生成、开发数据
```
