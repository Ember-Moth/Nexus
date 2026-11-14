# 两平面通信协议

网关使用受限 PostgreSQL 登录调用 `gateway_v1`，通过 Redis 接收配置通知并取得运行租约。本文给出节点接入顺序、SQL 签名和消息格式。仓库中的[Python 模拟器](../src/nexus/gateway_simulator.py)可用于联调。

## 通信边界

| 方向 | 通道 | 内容 |
| --- | --- | --- |
| 控制平面到网关 | PostgreSQL 快照，Redis 副本与 Pub/Sub | 模型、路由、鉴权摘要、限额、凭据引用 |
| 网关到权威状态 | PostgreSQL `gateway_v1` 函数 | 请求授权、资金与预算预留、开始上游尝试 |
| 网关到计量处理 | PostgreSQL 用量收件箱 | 尝试结果、用量证据、请求结束事实 |
| 网关集群协作 | Redis 原子操作与租约 | RPM、TPM、并发占用 |
| 网关到运维 | Redis 心跳，PostgreSQL 配置确认 | 节点状态、已应用配置版本 |

客户 AI 请求和响应正文不经过 PostgreSQL、Redis 或管理 API。传输通道承载的是控制状态与用量元数据。

## 节点接入与配置发布

1. 部署管理员创建 `nexus_bridge` 和 `nexus_gateway` 两个 NOLOGIN 角色。迁移身份须能 SET ROLE 到 identity、catalog、billing、publication 的模块角色及 nexus_bridge，以建立函数所有权。节点使用各自的 LOGIN 身份，继承 nexus_gateway 的权限并具有目标数据库 CONNECT 权限。各函数的实际授权清单随迁移固化。仓库的本机依赖脚本会准备组角色，节点登录及其凭据由部署方配置。
2. 平台管理员调用 `POST /api/v1/gateway-clusters`，提供 name、region、organization_ids、deployment_ids 和 public_key。公钥使用 Base64 编码的 32 字节 Curve25519 公钥。catalog 用 PyNaCl/libsodium sealed box 封装上游凭据，密文内部绑定集群、凭据和版本。部署方把私钥交给相应集群。
3. 用 `PUT /api/v1/gateway-clusters/{cluster_id}/principals/{node_id}` 登记 database_role、node_id、capabilities 与 active。节点身份由 PostgreSQL 登录角色确定。集群 PATCH 使用 If-Match，可暂停集群、修改范围或轮换公钥。key_grace_seconds 指定旧密钥包读取窗口，0 表示立即截止。缩小组织或部署范围会使先前范围的快照失效。新增范围随下一次发布生效。
4. 路由和限额按自然键用 PUT 写入。`PUT /api/v1/route-policies/{organization_id}/{model_id}` 为组织及模型设置部署候选 candidates，每个候选含 deployment_id、priority 和 weight。组织 owner 可通过 `PUT /api/v1/organizations/{organization_id}/quota-policies/{layer}/{scope_type}/{scope_id}/{metric}` 设置 customer 层限额，请求体为 `{"limit_value": …}`；platform 层由平台管理员维护。scope_type 为 organization 时 scope_id 即组织 ID。多个适用上限共同约束请求。不带 If-Match 时只创建，成功返回 201；策略已存在则返回 428。更新时携带上一版本的 If-Match，成功返回 200；版本不是当前版本或策略不存在时返回 412。`GET /api/v1/route-policies`、`GET /api/v1/organizations/{organization_id}/quota-policies` 和 `GET /api/v1/gateway-clusters` 使用 limit 与 cursor 分页。原 `POST /api/v1/route-policies` 和 `POST /api/v1/organizations/{organization_id}/quota-policies` 仍按原语义工作，但已标记为废弃，新客户端应改用 PUT。
5. 调用 `POST /api/v1/gateway-clusters/{cluster_id}/releases`，附带 Idempotency-Key，正常发布使用空对象。rollback_revision 可选择历史资源集合。发布前重新核对源版本、当前价格和凭据资格，回滚生成更高修订。查询 `GET /api/v1/gateway-clusters/{cluster_id}/releases/{revision}` 获取期望修订、已分发修订、任务和各节点最近的确认。
6. 节点调用 get_config_snapshot，校验原始字节的 SHA-256 后应用并确认。普通更新给已应用旧配置的节点保留最多 5 秒传播窗口。权限收缩、Key 失效、凭据撤销及集群范围变动由数据库实时检查。旧节点仍可提交其在途请求的结束证据。分发 Worker 每 30 秒核对并重写当前副本，Redis 清空期间节点可从 PostgreSQL 恢复。

## 数据库身份与权限

节点权限从 PostgreSQL 的 `session_user` 和 `publication.gateway_principals` 取得。部署角色负责建立节点登录，平台管理员登记集群绑定；连接代理需保留各节点的认证身份。`nexus_gateway` 获授 `gateway_v1` 函数的执行权限，业务表读写由函数所属角色完成。

`reserve_request`、`start_attempt`、`append_usage_event`、`cancel_unstarted_request` 写 billing，`ack_config` 写 publication。身份与目录校验可以在同一短事务中取得必要的读锁。各函数的授权和所有者见[网关迁移](../migrations/versions/0008_gateway_protocol.py)。

## PostgreSQL 桥接操作

PostgreSQL 函数在调用方事务内运行，函数返回一行并不等于事务已经提交。网关适配层必须收到 COMMIT 成功后，才把授权或事件接收视为成功；网络断开视为结果未知，用原业务 ID 查询或重试。

Python 模拟器在 autocommit 连接上以单条 SQL 调用一个函数，等待普通缓冲查询完整返回后再确认成功。网关调用要求独占一个事务边界，拒绝已有事务或 pipeline 中的调用。网关的新预留和开始尝试先按企业排队，再检查钱包状态。资金预留在持有账户写锁时一次写入所有命中的预算期间；开始尝试读取账户状态并持有共享锁至提交，以便冻结和尝试相互排序。

所有函数由 `gateway_v1` 提供，返回 JSONB。请求、尝试、模型、部署、凭据与 Key 标识使用 UUID。SQL 中修订号使用 bigint，JSON 中使用十进制字符串。

| 调用 | 含义 |
| --- | --- |
| `get_config_snapshot(revision DEFAULT NULL)` | 当前或历史完整包，含 Base64 原文、摘要与节点身份 |
| `get_credential_envelope(revision, credential_id, credential_version)` | 所选集群的密文包 |
| `ack_config(revision, hash, applied, reason DEFAULT '')` | 成功修订单调推进，reason 使用有界错误代码 |
| `reserve_request(rid, kid, model, rev, inputs, outputs)` | 服务端确定组织、账户、价格和预算，重复返回 replayed 与原状态 |
| `get_request_status(request_id)` | 本节点请求、冻结的价格引用、尝试、关闭与处理状态 |
| `start_attempt(rid, aid, seq, dep)` | 序号从 1 递增，候选来自授权时快照，重试使用原 attempt_id |
| `append_usage_event(message)` | UTF-8 JSON 文本，最多 65536 字节，事务提交后读取 accepted 或 conflict |
| `cancel_unstarted_request(rid, reason)` | 在同一事务释放无尝试且未关闭的请求与各预算占用 |

`reserve_request` 重试使用同一个 request_id 和相同语义参数。已有请求返回原授权及当前状态；不重新预留，也不重新选择售价或折扣。相同 ID 参数冲突则拒绝。已开始或已结清的请求不能因为拿到重复响应再次执行上游调用。

请求默认由最初授权的节点负责，不自动迁移在途请求。其他节点不能拿同一个 request_id 继续执行。客户端重新发送到另一个节点属于新的调用，首期不承诺跨客户端重试的上游恰好一次执行。

## 配置快照

快照按集群发布，带独立的 schema 版本、单调递增 revision 和内容摘要。修订号以字符串传输，避免不同语言处理大整数时丢失精度。

以下是简化的外层清单；具体模型、路由、策略和验证摘要放在对应资源集合，不能把此最小示例当作完整可运行配置：

```json
{
  "schema_version": 1,
  "cluster_id": "00000000-0000-4000-8000-000000000001",
  "revision": "42",
  "created_at": "2026-11-08T09:00:00Z",
  "required_capabilities": ["reservation.v1", "usage-events.v1"],
  "resources": {
    "models": [],
    "deployments": [],
    "routes": [],
    "access_policies": [],
    "quota_policies": [],
    "key_verifiers": [],
    "credential_refs": []
  }
}
```

控制平面保存序列化后的 UTF-8 原始字节，并对这些字节计算 SHA-256；网关先校验收到的原始字节，再解析内容，不对 JSON 重新序列化后比较摘要。摘要与原文分别传输，避免把摘要自身包含在哈希输入中。摘要检查用于发现损坏，发布者身份依赖经过认证的传输与访问控制。

发布流程如下：

1. 从属主门面收集带 revision 的不可变身份、目录和计价能力快照，组装前后核对来源 revision 向量；任一来源变化就重试。校验引用、模型能力、价格可用性和凭据授权，不由 publication 跨模块 JOIN 私有表。
2. 将不可变快照、期望版本和 outbox 任务一起提交。未明确发布的草稿不进入网关可见集合。
3. Worker 先确保必要凭据包可用，再写 Redis 完整快照，使用原子比较更新当前版本指针，最后发送变更通知。
4. Worker 记录分发结果；节点拉取、校验并一次性切换本地快照，然后调用 `ack_config`。
5. 管理 API 分别展示期望版本、已分发版本和节点应用状态，不把“已提交发布”显示为“所有节点已生效”。

节点启动时读取完整快照，再周期检查版本；收到通知时提前刷新。通知丢失、重复、乱序或 Worker 重试都不会使节点回退。读取与订阅之间也必须通过版本复查消除竞态。

回滚通过发布更高 revision、复用旧配置内容实现，保留新的来源记录；不能降低当前版本号。历史价格和已授权请求不随配置回滚被改写。

Key 撤销、组织冻结和权限收缩在请求授权时查数据库实时状态，不仅依赖配置刷新。撤销提交前已授权的在途请求按其状态处理，不宣称撤销会瞬间终止所有现有连接。普通路由与限速配置允许受刷新周期约束的传播延迟。

## 上游凭据包

快照只发布 `credential_id`、`credential_version` 和解密包引用。控制平面将上游凭据封装为指定集群可解密的包，记录集群公钥版本、算法标识和密文；使用成熟的加密库或密钥设施，不自创算法。

原始凭据由 catalog 拥有。catalog 的受限公开能力根据已验证的目标集群参数生成加密包，publication 只接收并存储封装结果，不直接查询 catalog 凭据表或传播解密后的明文。

集群解密材料通过部署密钥设施提供，不放入公开配置、普通 Redis 缓存或日志。当前封装使用 PyNaCl/libsodium sealed box，节点登录和集群私钥由部署方配置。凭据轮换支持短暂的新旧版本重叠，撤销版本在 `start_attempt` 中也要校验。

## 用量事件

通用字段包括 `schema_version`、`event_id`、`event_type`、`request_id`、可选 `attempt_id`、`occurred_at`、`config_revision`、`payload`。组织、项目、Key、售价与成本版本、客户折扣版本从已经持久化的请求与尝试关联获取，不允许消息覆盖。网关提交用量事实，不提交一个可直接扣除的钱数。

协议接受两类原始事实。

| 类型 | 必要内容 | 用途 |
| --- | --- | --- |
| `attempt.finished` | 尝试 ID、结果、上游请求标识、用量格式、原始用量、耗时 | 计算该尝试成本，并为客户费用提供证据 |
| `request.closed` | 全部已开始尝试 ID、交付结果、最终交付尝试 ID 或 null | 确定客户请求结束及计费策略适用结果 |

以下用量格式仅用于契约模拟器，不代表某个厂商的原生格式：

```json
{
  "schema_version": 1,
  "event_id": "00000000-0000-4000-8000-000000000301",
  "event_type": "attempt.finished",
  "request_id": "00000000-0000-4000-8000-000000000101",
  "attempt_id": "00000000-0000-4000-8000-000000000201",
  "occurred_at": "2026-11-08T09:00:02Z",
  "config_revision": "42",
  "payload": {
    "outcome": "succeeded",
    "upstream_request_id": "upstream-example-1",
    "usage_format": "fixture.tokens.v1",
    "raw_usage": {
      "input_total_tokens": 1000,
      "cached_input_tokens": 200,
      "output_tokens": 100
    },
    "elapsed_ms": 1250
  }
}
```

```json
{
  "schema_version": 1,
  "event_id": "00000000-0000-4000-8000-000000000302",
  "event_type": "request.closed",
  "request_id": "00000000-0000-4000-8000-000000000101",
  "attempt_id": null,
  "occurred_at": "2026-11-08T09:00:02Z",
  "config_revision": "42",
  "payload": {
    "delivery_outcome": "succeeded",
    "attempt_ids": ["00000000-0000-4000-8000-000000000201"],
    "delivered_attempt_id": "00000000-0000-4000-8000-000000000201"
  }
}
```

`delivery_outcome` 枚举为 `succeeded`、`client_cancelled`、`upstream_failed`、`unknown`。成功必须有属于该请求的交付尝试；关闭事件中的尝试集合必须与持久化的开始记录一致。关闭与开始操作锁定同一请求，关闭后不允许新增尝试。

`attempt.finished` 结果可以是 `succeeded`、`failed`、`cancelled`、`unknown`。没有可信用量时 `raw_usage` 为 null，不填充虚构的零。两类事件可以乱序到达，Worker 等待所需事实齐全后结算。

## 接收、幂等与恢复

- 首期单事件上限为 64 KiB，限制字段数与字符串长度；拒绝正文、密钥和无限嵌套对象。
- `event_id` 全局唯一；同 ID、相同 JSON 语义内容返回原接收结果，不同内容返回冲突并留审计。
- 每个尝试最多一个原始完成事实，每个请求最多一个原始关闭事实。换 event_id 提交第二个事实同样会被约束拒绝。
- 只有在包含 `append_usage_event` 的事务提交成功后，网关适配层才能报告 `accepted`。该结果表示已持久化，尚不表示已扣费。
- 提交结果未知时，网关保留同一事件 ID 重试；禁止生成新的 ID 隐藏不确定性。
- 重试、任务接管和回放都可能重复，唯一约束与事务保证同一业务事实只产生一次初始扣费。
- 语法有效但业务证据不足的事件保存后进入核对状态。非法身份、跨租户引用和不支持的协议版本在接收前拒绝。

网关启动上游前已经持久化预留和尝试意图，但最终用量生成到提交之间仍有故障窗口。无状态网关不能仅依靠内存队列承诺零丢失；发生该故障时，以未结束尝试和保留资金进入核对。未来是否增加持久化接入服务，应由可接受损失和吞吐要求决定。

## Redis 键与集群协调

以下为键命名约定，花括号是 Redis Cluster hash tag；ID 使用稳定内部标识，不使用用户名或明文 Key。

| 用途 | 键示例 | 权威来源或过期规则 |
| --- | --- | --- |
| 配置当前版本 | `nexus:v1:cfg:{cluster_id}:current` | PostgreSQL，版本单调增加 |
| 配置快照 | `nexus:v1:cfg:{cluster_id}:snapshot:42` | PostgreSQL，不可变 |
| 配置通知频道 | `nexus:v1:cfg:cluster_id:changed` | 提示信息，不作为可靠记录 |
| RPM 和 TPM | `nexus:v1:limit:{organization_id}:policy_id:metric:window` | 原子窗口计数，过期回收 |
| 并发租约 | `nexus:v1:concurrency:{organization_id}:policy_id:concurrency` | 请求租约集合，续租及过期回收 |
| 节点心跳 | `nexus:v1:node:node_id` | TTL，重启可重建 |

同一组织的多层限流键使用相同 hash tag，以便一个原子操作检查和占用适用的组织、项目、Key 限额。Redis Cluster 的多键操作要求相关键处于同一 hash slot，不能把跨 slot 的多层扣减描述为天然原子。[Redis Cluster 约束](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/)

RPM 首期按请求被全局限流器接纳时计数，即使后续余额不足也占用该次请求次数；TPM 先按授权上界占用，已确认用量后在原窗口内核销。错误、退款和未知用量的释放行为必须显式定义，不能任由每个节点自行解释。金额预算始终在 PostgreSQL 事务中控制。

并发使用请求级租约和唯一占用 ID，支持幂等释放、续租与有界存活。节点失联导致租约过期时，旧节点必须在续租失败的安全截止前停止继续占用上游；否则只能承诺有界近似并发，不能承诺绝对硬上限。真实网关接入时需验证到期后的上游取消行为。

单一热点组织仍可能集中在一个 Redis slot，不能仅通过增加节点解决所有热点。实际压力测试后再决定更复杂的容量划分。

## 版本与兼容

网关应用配置前验证 schema 版本、摘要与必需能力，失败时上报拒绝原因并保留可用版本。用量事件按 v1 的字段集合和枚举校验。升级协议时先部署兼容调用端的数据库函数，待目标节点支持所需能力后再发布配置。

## 协议模拟器

fixture.tokens.v1 要求 input_total_tokens、cached_input_tokens 和 output_tokens。启用缓存写入计价时还需相应 cache_write_5m_tokens 或 cache_write_1h_tokens。reasoning_tokens 属于 output_tokens 的子集。fixture.dimensions.v1 接收与冻结价格维度完全一致的非负整数量。Worker 保存规则版本、证据引用和归一化数量。缺失最终事实保持等待，新事实提交会立即唤醒任务。证据无效或结果未知进入核对，预留继续有效。失败尝试独立记成本，上游失败的客户费用为零。

Redis 限额使用按组织 hash tag 的原子脚本。RPM 在限流通过时占用。TPM 按输入与输出总上界占用，已知用量仅在原 UTC 分钟窗口核销，资金授权被明确拒绝时按零用量释放 TPM。未知用量保留上界，事后财务退款仍沿用原运行计数。并发占用使用独立租约标识，可续租或幂等释放。节点在安全截止时间前续租，过期或连接中断时停止执行。发布器按组织初始化限额代次。已初始化的运行状态丢失或 Redis 实例更换后，受限流策略约束的新调用等待 60 秒恢复窗口，旧代次租约失效。计数库应配置为禁止逐出。PostgreSQL 保留资金预留与原始用量事实。

配置 NEXUS_GATEWAY_DSN、NEXUS_REDIS_URL、NEXUS_SIMULATION_API_KEY，启动 API 和 Worker，发布并登记集群后执行以下命令。模拟器按最低 priority 选择候选，并在相同优先级内按 weight 抽取部署。输出包含结算与成本处理状态。

```sh
uv run python -m nexus.gateway_simulator MODEL_UUID --input-tokens 2 --output-tokens 1
```

本机测试使用独立 PostgreSQL 节点登录和 Redis，覆盖授权到扣费、事务中断、乱序与重复事件、迟到成本、撤权、密钥轮换、旧确认和副本丢失恢复。真实厂商用量映射和 Rust 网关将在对应适配阶段验证。
