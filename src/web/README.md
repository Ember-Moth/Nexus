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

## 会话与安全

```
浏览器 ──同源──▶ /api/session              登录、取 CSRF token、退出
       ──同源──▶ /api/nexus/api/v1/...     代理到 NEXUS_API_URL，附加 Bearer token
       ──同源──▶ /api/password             兑换一次性凭据（POST /auth/password）
```

- **access_token 不进入浏览器 JavaScript**。`POST /api/session` 调用 `POST /auth/sessions`，把 token 写入 `__Host-nexus_session`（httpOnly、Secure、SameSite=Lax、到期时间与会话一致），响应体只返回 `expires_at` 和 CSRF token。
- **CSRF**：登录时另发 `__Host-nexus_csrf`（httpOnly、SameSite=Strict）。前端通过同源的 `GET /api/session` 取得该值，写操作带 `X-CSRF-Token`；服务端做常量时间比较，并要求 `Origin`（或 `Sec-Fetch-Site`）为同源。登录本身也校验 Origin。
- **代理白名单**：只转发 `/api/v1/...`，拒绝 `auth/*` 与非法路径段；请求头只转发 `Content-Type`、`Accept`、`If-Match`、`Idempotency-Key`，响应头只回传 `ETag`、`Content-Type`、`Content-Disposition`、`Retry-After`、`X-Request-Id`，并统一 `Cache-Control: no-store`。上游返回 401 时清除 Cookie。
- **退出**：`DELETE /api/session` 调用 `DELETE /auth/sessions/current` 撤销服务端会话，并无条件清除 Cookie。
- `src/proxy.ts` 只负责把没有会话 Cookie 的页面访问重定向到 `/login?next=…`；授权仍由 BFF 和 API 逐个请求判断。
- 安全响应头见 `next.config.ts`（`X-Frame-Options: DENY` 等）。Cache Components 保持关闭：页面都依赖会话，且路由保活会让一次性显示的 Key 明文在离开页面后仍留在 DOM 中。

## API 约定的封装

| 约定 | 位置 | 做法 |
| --- | --- | --- |
| 类型化请求 | `src/lib/api/client.ts` | `nexus.GET/POST/...`（openapi-fetch，路径、参数、请求体均来自生成类型）；`call()` 把错误信封转成 `ApiError` 并返回 `{data, etag, status}` |
| 错误与 trace_id | `src/lib/api/errors.ts`、`src/components/feedback.tsx` | `ApiError{status, code, message, traceId, fields}`；`ErrorAlert` 和 `useErrorToast` 显示可复制的追踪 ID，已知错误码映射到文案键 |
| ETag / If-Match | 各写操作 | 打开对话框时读取单体资源拿 ETag，提交时原样放入 `if-match`；412 显示 `ConflictAlert`，重新加载后再提交 |
| Idempotency-Key | `useIdempotentMutation`（`src/lib/api/hooks.ts`） | 每个业务操作生成一个键；变量（含 If-Match 版本）不变时重试复用同一个键，结果未知（网络错误、502–504、429）自动重试；成功或关闭对话框后换新键 |
| 202 轮询 | `pollUntil()` | 2 秒 → 5 秒 → 10 秒逐步放慢，到终态停止；用于充值订单和导出任务 |
| 游标分页 | `useCursorList()` | `next_cursor` 作为下一页的 `cursor`，筛选条件放进 query key 保持一致；页脚“加载更多”，不显示总数 |
| 金额 | `src/lib/money.ts`、`src/components/money.tsx` | 不转浮点；显示千分位和两位小数，后续有效小数以浅色补在后面，不四舍五入；输入按渠道精度校验，超精度直接报错 |
| 401 | `onUnauthorized()` + `AuthGuard` | 会话失效后清空缓存，回到 `/login?expired=1&next=…` |

## API 类型生成

```sh
NEXUS_API_URL=http://127.0.0.1:8000 pnpm api:types   # 拉取 /openapi.json，更新 openapi/openapi.json 和 src/lib/api/schema.d.ts
pnpm api:types --from-snapshot                       # 不连 API，按已提交的快照重新生成
pnpm api:types:check                                 # CI：生成结果必须与已提交文件一致
```

契约快照和生成文件都已提交。`/openapi.json` 只在开发环境开放。

> 另一个会话正在修订 OpenAPI 契约：给 reporting、财务运营、publication、routing 的 27 个操作补响应模型，路由/限额策略改为 PUT，统一分页和路径参数命名。合并后重新运行 `pnpm api:types`。目前这些接口在 `src/lib/api/types.ts` 中用手写类型（按运行中的 API 实际返回整理），届时替换为生成类型。

## 文案与多语言

界面文字集中在 `src/i18n/messages/zh-CN.ts`，按页面分组；`en.ts` 目前只翻译了公共部分，缺失的键回退到中文。组件里用 `useT()("keys.issueDialog.title")`，键名有类型检查。语言保存在 `nexus_locale` Cookie，登录页和用户菜单可切换。

## 页面状态

已在本地 API 上实际走通：登录 → 选择企业 → 查看余额 → 签发、轮换、撤销 Key → 查看流水，以及 412 冲突、导出任务轮询与 CSV 下载、退出登录。

| 页面 | 状态 | 说明 |
| --- | --- | --- |
| 登录 `/login` | 已完成 | 登录名/密码错误（含追踪 ID）、429 限流提示、会话过期提示、`next` 跳转、语言切换 |
| 设置密码 `/setup-password` | 已完成 | 兑换一次性凭据；通过 BFF 接口验证（204 后新用户可登录），表单未在浏览器中走查 |
| 应用外壳 | 已完成 | 按 `/me` 角色计算的两层导航、企业切换（搜索、平台入口、浏览全部企业）、用户菜单与退出、移动端抽屉导航 |
| 企业概览 `overview` | 已完成 | 可用余额/账户余额/在途预留/本月净消费，14 天用量柱状图（含投影水位），预算周期，最近充值；开发者只见用量 |
| 调用 Key `keys` | 已完成 | 列表与筛选、签发（明文仅显示一次、复制、确认后才能关闭、重试返回 200 时提示不可再取回）、轮换（重叠期）、撤销（输入前缀确认）、412 |
| 项目 `projects` | 已完成 | 项目列表/新建/停用恢复（If-Match、412），服务账号列表/新建/停用恢复，从服务账号签发 Key；“开发者授权”“访问策略”页签为骨架 |
| 服务账号 `service-accounts` | 已完成 | 只读列表，按项目筛选 |
| 钱包与流水 `wallet` | 已完成 | 账户摘要、时间范围与口径、项目筛选、游标分页、导出 CSV |
| 请求明细 `requests` | 已实现，本地无请求数据 | 筛选与游标分页；没有网关流量，只验证了空状态 |
| 充值 `recharge` | 已实现，未联调支付 | 支付方式、按精度校验金额、创建订单（202 + Idempotency-Key）、轮询订单、订单列表；本地未配置支付渠道，只验证了“无可用支付方式”状态 |
| 审计与导出 `audit` | 已完成 | 审计事件（按动作筛选、分页）；导出任务创建、轮询、下载 |
| 平台 · 企业 `platform/organizations` | 已完成 | 列表、新建（开通中自动刷新）、进入工作区 |
| 用量、可用模型与价格、费用明细、预算、成员、访问策略、限额 | 骨架 | 页面内列出计划使用的接口 |
| 平台：用户与平台角色、模型、上游与凭据、部署、路由策略、售价/成本价版本、企业折扣、集群与发布、收款账户、人工入账、调整、核对事项、支付异常、支付渠道、审计事件、运行指标 | 骨架 | 同上；设计稿见第一阶段画布 |

## 发现的 API 缺口

前端没有修改后端，以下缺口按影响排序：

1. **成员和授权没有人名**：`MemberView`、`GrantView` 只有 `user_id` / `membership_id`，`GET /users` 仅平台管理员可用；企业所有者的成员页、项目授权、审计和流水的“操作人”只能显示 ID。`/me` 的成员关系也不带企业名称。
2. **导出任务没有列表接口**：只有 `GET /organizations/{org}/exports/{id}`，后台只能在本次浏览器会话里记住创建过的任务。
3. **调用 Key**：`KeyView` 没有状态字段（需由 `revoked_at`/`rotated_at`/`expires_at` 推导）和 `last_used_at`；列表不能按状态筛选。签发和轮换的幂等重放返回 200，契约只声明了 201。
4. **发布**：没有发布历史列表、`GET /gateway-clusters/{id}` 单体接口和节点（principals）列表，节点只出现在某个修订的状态里。
5. **聚合与 N+1**：没有计数（服务账号下的有效 Key 数等）；预算概览需先列策略再逐个查周期；流水不能按类型筛选；请求和流水只返回项目、Key、模型的 ID。
6. **登录限流与 BFF**：API 按直连地址做每 IP 限流且不信任转发头，经 BFF 登录时所有用户共享 BFF 的地址。部署时需要为 BFF 配置可信代理，或由 API 提供受信的客户端地址传递方式。
7. **响应模型**：reporting、导出、审计、publication、核对事项等接口没有响应模型，错误响应也未声明（api-contract 会话处理中）。

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
    login, setup-password                            公共页面
    (console)/o/[orgId]/...                          企业工作区
    (console)/platform/...                           平台管理
  components/   ui/（shadcn）、shell/（导航外壳）、data-table、feedback、money …
  i18n/         文案与语言切换
  lib/api/      生成类型、客户端、hooks、手写类型
  lib/server/   仅服务端使用的配置与会话工具
  proxy.ts      未登录重定向
openapi/        契约快照
scripts/        类型生成、开发数据
```
