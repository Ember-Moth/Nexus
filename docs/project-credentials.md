# 项目与应用凭据

## 业务层次

组织拥有项目，项目拥有服务账号，服务账号持有 API Key。调用凭据代表应用，与创建它的员工身份分开管理。员工退出企业或失去项目授权，会立即失去对应管理权限；不会自动删除或撤销应用正在使用的 Key。

每个资源都携带组织归属。项目授权使用组织与成员/项目的复合外键，Key 使用组织、项目、服务账号的复合外键，轮换链也限定在同一服务账号内。客户端不能通过修改父级 ID 移动凭据。

## 签发流程

1. owner 创建项目，给需要管理该项目的 developer 成员添加授权。
2. owner 或获授权 developer 在项目内创建服务账号。
3. 提供 Idempotency-Key 签发 Key，保存首次响应中的 secret。
4. 使用列表/详情查询元数据，使用条件更新停用、撤销或轮换。

查询该 Key 可用的模型时提供组织、项目和 Key 上下文。[模型访问策略](catalog-api.md#模型访问策略)

## 权限范围

| 身份 | 项目基本信息 | 项目及授权管理 | 服务账号与 Key 管理 |
| --- | --- | --- | --- |
| platform_admin | 平台范围 | 可以 | 可以 |
| org_owner | 本组织全部项目 | 可以 | 本组织全部项目 |
| org_developer | 显式获授权项目 | 不可以 | 获授权项目 |
| org_finance / platform_finance | 相应组织范围的项目元数据 | 不因财务角色获得权限 | 不因财务角色获得权限 |

项目授权只能授予有效的 org_developer 成员。owner 已经具有组织范围权限；财务角色不能通过项目授权扩大成凭据管理权限。同一用户具有多种角色时按各角色的实际作用域检查。

成员和授权状态在每次请求中重新读取。越过组织或未授权项目的资源查询返回 404，已经处于可访问范围但缺少操作权限时返回 403。角色检查在服务层执行，创建、授权、轮换等写操作在组织锁下重新检查权限。

## 管理接口

下列路径均带前缀 `/api/v1/organizations/{org}`，使用管理会话 Bearer Token。

| 接口 | 行为 |
| --- | --- |
| `GET/POST /projects` | 按可见范围分页查询，owner 或平台管理员创建 |
| `GET/PATCH /projects/{id}` | 查询、改名、停用/恢复或归档 |
| `GET/POST /project-grants` | owner 或平台管理员查询和授予项目权限 |
| `GET/PATCH/DELETE /project-grants/{id}` | 查看、恢复或撤销授权；DELETE 保留记录 |
| `GET/POST /service-accounts` | 按项目权限查询或创建应用身份 |
| `GET/PATCH /service-accounts/{id}` | 查询、改名或停用/恢复，project_id 不可修改 |
| `GET/POST /api-keys` | 查询元数据或签发应用凭据 |
| `GET /api-keys/{id}` | 获取当前元数据及版本，不返回明文或摘要 |
| `POST /api-keys/{id}/revoke` | 永久撤销指定 Key |
| `POST /api-keys/{id}/rotate` | 创建替代 Key，并结束或缩短旧 Key 的有效期 |
| `GET /upstream-resources` | 各 Key 在上游供应商账户持有的资源及清理状态，见[上游资源](#上游资源) |
| `POST /upstream-purges`、`GET /upstream-purges/{id}` | 删除某个 Key 的全部或单个上游资源，返回 202 并可查询进度 |

列表使用 limit/cursor 分页。服务账号列表可按 project_id 筛选；Key 列表可按 project_id、service_account_id 筛选，筛选条件也必须在调用者权限范围内。

修改、授权撤销、Key 撤销及轮换要求 If-Match。创建和查询单个资源返回 ETag；版本过期返回 412，缺少条件返回 428。项目名称在组织内唯一，服务账号名称在项目内唯一；Key 名称只是标签。

## Key 签发与幂等

签发请求包含 service_account_id、name 和可选的 expires_at。项目和组织由服务账号确定，不接受客户端另外指定。expires_at 必须有时区且晚于当前数据库时间；省略表示没有单独的到期时间。

Key 由服务端生成 32 字节随机值，以 nxk_ 开头。数据库只保存 SHA-256 摘要与用于辨认的短前缀；管理会话 nxs_ 和密码凭据 nxp_ 不能与它互换。

签发和轮换必须提供 Idempotency-Key，长度 1–128，允许字母、数字、下划线、点、冒号和连字符。作用域包含组织、操作人与操作类型，参数摘要包含请求体及轮换目标和原 If-Match 版本。

- 首次成功返回 201，响应包含 key 元数据、secret 和 `secret_status=available`。
- 同一请求重试返回 200，指向原资源，secret 为 null，`secret_status=secret_unavailable`。
- 同一幂等键对应不同参数返回 409，不再次创建资源。
- 重试仍检查当前权限，已失去项目权限的用户不能通过幂等记录取回元数据。

明文只随第一次成功响应返回，幂等记录和审计不保存它。若第一次响应丢失，先用原幂等键取得资源 ID，再显式轮换该资源；不能把重试当作取回明文的接口。当前幂等记录不自动清理。

## 轮换与停用

轮换可提交 name、expires_at 和 grace_seconds。默认继承旧 Key 的名称和原到期时间。grace_seconds 默认为 0，即立即撤销旧 Key；可设置 1–3600 秒的重叠期，旧 Key 的到期时间取原到期时间与重叠截止时间中的较早者。

旧 Key 的状态与替代 Key、幂等记录和审计在同一个事务中提交。每个旧 Key 最多产生一个直接替代 Key，不能并发产生多条轮换分支。重试轮换应保留原请求体和原 If-Match；查看旧 Key 可获得实际截止时间与新版本。

已撤销、已过期或已经轮换的 Key 不可再次轮换；可以对最新替代 Key 继续轮换。撤销后的 Key 不能恢复。项目归档不可恢复，服务账号和项目停用后可恢复，但不会使单独撤销或过期的 Key 重新有效。

组织暂停或归档后停止正常写操作，平台管理员仍可紧急撤销 Key。项目或服务账号停用会使其下的 Key 在后续身份校验中失效，无需逐条修改 Key。

## 上游资源

部署在 `relay_profile.provider_features` 中启用供应商功能后，Key 通过 Relay 创建的已存储 response、文件、会话、向量库、代码执行容器和批处理保存在该部署的上游账户中。Nexus 只保存定位用的句柄和归属，每个资源归属于创建它的 Key，也就归属于该 Key 的项目和组织。Key 撤销、过期，或其组织、项目、服务账号停用后，这些资源不会立即消失：超过 Relay 宽限期（默认 7 天）后由清理任务删除上游副本，见[上游资源清理](relay-design.md#上游资源清理)。宽限期内，以及 Key 仍然可用时，组织可以通过下列接口查看并提前删除。

| 身份 | 查看 | 删除 |
| --- | --- | --- |
| platform_admin | 任意组织；另可用 `GET /api/v1/upstream-resources` 跨组织查看 | 可以 |
| org_owner | 本组织全部 Key | 可以，组织暂停或归档后仍可删除 |
| org_developer | 获授权项目的 Key | 不可以（403） |
| org_finance / platform_finance | 不可以（403；未加入组织时 404） | 不可以 |

权限沿用 Key 管理的组织角色检查：组织外或未获授权项目的 Key 返回 404，已在范围内但缺少删除权限时返回 403。已撤销和已过期的 Key 仍在查看和删除范围内。

**查看。** `GET /upstream-resources` 支持 project_id、api_key_id、kind、state、limit 和 cursor，按创建时间从新到旧分页。每项返回：

| 字段 | 说明 |
| --- | --- |
| `id`、`handle` | 句柄 ID 和 Relay 返回给 Key 持有者的 ID，例如 `file_nx_<id>`、`resp_nx_<id>`。上游已创建但本地句柄没有保存成功的资源 `handle` 为 null，`id` 是清理任务 ID |
| `kind` | `response`、`file`、`conversation`、`vector_store`、`container`、`batch` |
| `api_key_id`、`project_id`、`organization_id` | 所属 Key 及其项目和组织 |
| `created_at` | 句柄创建时间 |
| `state` | `retained` 仍在上游且未排队清理；`pending` 待清理（含失败后等待重试）；`purged` 已由清理任务删除；`failed` 清理失败，30 次尝试后放弃，副本可能仍在上游；`deleted` Key 持有者已自行删除 |
| `purge` | 清理任务的原因（`key_inactive` 宽限期后自动清理、`orphan` 本地句柄未保存、`owner_request` 组织主动删除）、尝试次数、最后的错误代码和下次尝试时间 |

供应商的资源 ID 和凭据不在响应中出现。尚未在上游创建成功的向量库和批处理不列出。

**删除。** `POST /upstream-purges` 必须带 Idempotency-Key。请求体 `{"api_key_id": …}` 删除该 Key 的全部上游资源；再加 `kind` 和 `resource_id`（句柄 ID）只删除一个。成功返回 202 和删除请求，`Location` 指向 `GET /upstream-purges/{id}`：

| 字段 | 说明 |
| --- | --- |
| `total`、`pending`、`purged`、`failed` | 本次请求覆盖的清理任务数及各状态数量 |
| `state` | 有任务待清理时为 `pending`；全部结束后，有失败为 `failed`，否则为 `purged`。没有需要删除的资源时 `total` 为 0、`state` 为 `purged` |

删除复用清理队列：请求为每个仍在上游的资源写入一条 `owner_request` 任务，由 Relay 节点按与自动清理相同的方式调用供应商删除接口、重试和记审计。删除不可恢复，之后用该句柄的请求返回 404。幂等规则如下：

- 同一幂等键、同一请求体重试返回原删除请求；不同请求体返回 409 `idempotency_conflict`。作用域包含组织和操作人。
- 每个上游副本最多一条清理任务。资源已被自动清理或先前的请求排队时，新请求覆盖已有任务而不是再排一条；已清理的资源不再排队。
- 清理失败（`failed`）的资源在新的删除请求中从头重试。
- 按 Key 删除只覆盖提交时已有的资源。Key 仍可用时，之后新建的资源不在本次范围内，需要时先撤销 Key 再删除。

每次新的删除请求写入审计 `upstream_resource.purge_requested`（对象为资源或 Key，详情含请求 ID、新排队数、重试数和覆盖数），每个任务的结果另有 `upstream_resource.purged`、`upstream_resource.purge_failed` 或 `upstream_resource.purge_abandoned`。
