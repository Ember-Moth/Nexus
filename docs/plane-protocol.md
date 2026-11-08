# 两平面通信协议

协议设计 v1 已随项目设计基线 v1.0 定稿。控制平面通过 PostgreSQL 发布持久化事实，通过 Redis 分发配置副本和协调运行状态。Rust 网关暂不实现；控制平面实现阶段使用模拟器验证契约。

## 通信边界

| 方向 | 通道 | 内容 |
| --- | --- | --- |
| 控制平面到网关 | PostgreSQL 快照，Redis 副本与 Pub/Sub | 模型、路由、鉴权摘要、限额、凭据引用 |
| 网关到权威状态 | PostgreSQL `gateway_v1` 函数 | 请求授权、资金与预算预留、开始上游尝试 |
| 网关到计量处理 | PostgreSQL 用量收件箱 | 尝试结果、用量证据、请求结束事实 |
| 网关集群协作 | Redis 原子操作与租约 | RPM、TPM、并发占用 |
| 网关到运维 | Redis 心跳，PostgreSQL 配置确认 | 节点状态、已应用配置版本 |

客户 AI 请求和响应正文不经过 PostgreSQL、Redis 或管理 API。传输通道承载的是控制状态与用量元数据。

## 数据库身份与权限

数据库划分内部业务 schema 和版本化的 `gateway_v1` schema。网关使用独立的受限数据库身份，服务端通过数据库认证角色映射 `gateway_principals`，确定其集群、节点和允许范围，不信任消息自报的 `node_id`。

网关角色仅能执行指定函数，不能直接读写账户、价格、用户、账本或任意收件箱行。节点身份由部署引导流程配置，网关不能通过提交一个节点名称自行注册获得权限。

gateway_v1 本身不拥有业务表。组合入口由应用迁移装配，各模块发布需要的版本化 SQL 契约：publication 提供节点和集群授权，identity 提供实时 Key/组织授权，catalog 提供模型与部署资格，billing 提供资金预留和计量写入。只有这些显式契约可以被组合入口调用，不暴露模块私有表或 ORM。

一次网关变更只写一个业务模块：reserve_request、start_attempt、append_usage_event、cancel_unstarted_request 写 billing；ack_config 写 publication。跨模块的身份、配置和目录检查是业务只读操作，允许必要的行级锁保持到提交，不顺手更新其他模块的 last_seen 等字段。其执行权限只授予桥接角色，普通网关角色不能绕过组合入口直接调用内部能力。

这项规则是 Nexus 直连 PostgreSQL 的特定适配。Python 业务模块仍通过门面和窄端口协作，不使用跨模块写事务；SQL 定义、锁顺序、模块所有权和权限清单必须一并测试。

如果函数使用 `SECURITY DEFINER`，必须由非超级用户的专用角色拥有、固定安全的 `search_path`、全限定对象名、撤销 PUBLIC 执行权限，并校验调用身份和所有关联资源。函数不得将客户端参数拼接为动态 SQL。迁移角色、API 角色、Worker 角色和网关角色分别授权。[PostgreSQL 函数安全说明](https://www.postgresql.org/docs/current/sql-createfunction.html)

网关身份基于实际认证的 `session_user` 映射，不使用在 SECURITY DEFINER 中已变为函数所有者的 `current_user`。网关没有切换到其他网关角色的权限；连接代理必须保留这一身份边界，不能统一成一个超级用户再相信参数中的节点名称。

## PostgreSQL 桥接操作

函数名属于设计契约，具体参数将由迁移和契约测试固定。PostgreSQL 函数在调用方事务内运行，函数返回一行并不等于事务已经提交。网关适配层必须收到 COMMIT 成功后，才把授权或事件接收视为成功；网络断开视为结果未知，用原业务 ID 查询或重试。

| 操作 | 输入要点 | 行为 |
| --- | --- | --- |
| `get_config_snapshot` | 指定修订号或当前版本 | 只返回本集群允许读取的完整快照 |
| `get_credential_envelope` | 凭据 ID 与版本 | 校验集群授权，返回加密包，不返回控制平面根密钥 |
| `ack_config` | 修订号、摘要、应用结果 | 记录实际应用状态；旧确认不覆盖较新成功版本 |
| `reserve_request` | request_id、Key 标识、模型、配置版本、计量上界 | 校验实时权限，锁定售价和客户折扣，原子预留资金和金额预算 |
| `get_request_status` | request_id | 返回本节点有权查询的授权、预留和处理状态 |
| `start_attempt` | request_id、attempt_id、序号、deployment_id | 校验候选部署与有效身份，持久化尝试意图并锁定成本价 |
| `append_usage_event` | 版本化事件 | 校验身份与关联，插入收件箱；调用方确认事务提交后才视为持久化成功 |
| `cancel_unstarted_request` | request_id、原因 | 仅在不存在任何尝试意图时取消并释放预留 |

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

集群解密材料通过部署密钥设施提供，不放入公开配置、普通 Redis 缓存或日志。具体封装库和节点引导方式在发布模块实现前确定。凭据轮换支持短暂的新旧版本重叠，撤销版本在 `start_attempt` 中也要校验。

## 用量事件

通用字段包括 `schema_version`、`event_id`、`event_type`、`request_id`、可选 `attempt_id`、`occurred_at`、`config_revision`、`payload`。组织、项目、Key、售价与成本版本、客户折扣版本从已经持久化的请求与尝试关联获取，不允许消息覆盖。网关提交用量事实，不提交一个可直接扣除的钱数。

首期定义两类原始事实：

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
| RPM 和 TPM | `nexus:v1:limit:{organization_id}:scope:metric:window` | 原子窗口计数，过期回收 |
| 并发租约 | `nexus:v1:concurrency:{organization_id}:scope` | 请求租约集合，续租及过期回收 |
| 节点心跳 | `nexus:v1:node:node_id` | TTL，重启可重建 |

同一组织的多层限流键使用相同 hash tag，以便一个原子操作检查和占用适用的组织、项目、Key 限额。Redis Cluster 的多键操作要求相关键处于同一 hash slot，不能把跨 slot 的多层扣减描述为天然原子。[Redis Cluster 约束](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/)

RPM 首期按请求被全局限流器接纳时计数，即使后续余额不足也占用该次请求次数；TPM 先按授权上界占用，已确认用量后在原窗口内核销。错误、退款和未知用量的释放行为必须显式定义，不能任由每个节点自行解释。金额预算始终在 PostgreSQL 事务中控制。

并发使用请求级租约和唯一占用 ID，支持幂等释放、续租与有界存活。节点失联导致租约过期时，旧节点必须在续租失败的安全截止前停止继续占用上游；否则只能承诺有界近似并发，不能承诺绝对硬上限。该行为属于未来网关验收项。

单一热点组织仍可能集中在一个 Redis slot，不能仅通过增加节点解决所有热点。实际压力测试后再决定更复杂的容量划分。

## 版本、兼容与替代通道

消息 schema、数据库接口 schema 和配置 revision 是不同概念。兼容升级允许增加可选字段，读取端忽略未知可选字段；未知必需能力、未知事件类型、关键枚举或不兼容主版本必须拒绝，不静默降级。

迁移遵循先扩展、再升级调用方、最后移除旧契约的顺序。网关旧版本和新版本共存期间，配置只能使用目标集群共同支持的必需能力。

PostgreSQL `LISTEN/NOTIFY` 可作为无 Redis 配置通知的替代，但不是首期第二套完整实现。消息只携带版本标识，离线恢复依赖快照。开始监听需要先提交 LISTEN，再读取当前状态并复查通知，不能假设监听前的事件会重放。[PostgreSQL LISTEN](https://www.postgresql.org/docs/current/sql-listen.html)、[NOTIFY](https://www.postgresql.org/docs/current/sql-notify.html)

如果未来采用 Redis Streams，配置广播不能简单让所有网关共享一个消费组，因为同组消费者分摊消息；结算 Worker 则适合使用同组分摊。Streams 的重试、保留、持久化与故障切换策略必须另行设计，不在首期默认为已解决。[Redis 消费组](https://redis.io/docs/latest/commands/xreadgroup/)
