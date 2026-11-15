# 运行指南

## 初始化本机环境

在 Linux 或 macOS 上准备 uv、项目指定的 Python、PostgreSQL 16 或更新版本及 Redis。服务脚本使用本机的 `initdb`、`pg_ctl`、`postgres` 和 `redis-server`。备份恢复测试还需与 PostgreSQL 主版本匹配的 `pg_basebackup` 和 `pg_verifybackup`。[Python 版本](../.python-version) · [依赖锁文件](../uv.lock)

在仓库根目录执行：

```sh
uv sync --locked
uv run python scripts/dev_services.py start
uv run nexus --env-file .env.migrate migrate
uv run nexus --env-file .env.api check
```

`start` 在 `.local/native` 创建项目专用实例，选择可用端口并监听回环地址。首次启动生成随机密码、32 字节加密根密钥和三个权限为 0600 的配置文件。后续启动复用同一组数据和配置。

| 配置 | 进程与数据库角色 |
| --- | --- |
| `.env.migrate` | Alembic 迁移使用 `nexus_migrator` |
| `.env.api` | 管理 API、就绪检查和管理员引导使用 `nexus_api` |
| `.env.worker` | 后台任务与心跳使用 `nexus_worker` |

用 `--env-file` 显式选择配置，进程环境变量优先。版本升级时先用迁移身份执行 `migrate`，默认升级到当前代码的 head，再启动匹配版本的 API 与 Worker。运行角色在事务内切换到所属模块的数据库角色。[配置示例](../.env.example)

## 初始管理员

完成迁移后，在部署主机执行：

```sh
uv run nexus --env-file .env.api bootstrap-admin --login admin
```

`admin` 可替换为自己的登录名。按提示输入并确认至少 16 字符的密码，成功返回 `user_id` 和 `created: true`。自动化可用 `--password-stdin` 传入密码。首次引导授予 `platform_admin`；相同登录名重复引导返回原用户，保留原密码。后续管理员和财务账号由已有管理员创建并分配角色。[登录与角色管理](identity-api.md)

## 启动与健康检查

分别在两个终端运行：

```sh
uv run nexus --env-file .env.api api
```

```sh
uv run nexus --env-file .env.worker worker
```

API 默认监听 `127.0.0.1:8000`，由 `NEXUS_HOST` 和 `NEXUS_PORT` 控制。开发和测试环境可在 `/docs` 登录并调用管理接口。新建企业后，Worker 消费开户事件并激活组织。配置分发、收款查单、用量结算、报表和导出也依赖 Worker。

```sh
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:8000/health/ready
uv run nexus --env-file .env.worker worker-health
```

`live` 检查 API 进程是否存活，`ready` 校验数据库角色、迁移版本、模块权限与 Redis。API 就绪失败时查看响应中的检查项。每个 Worker 使用独立的 `NEXUS_WORKER_ID`，通过对应配置运行 `worker-health` 检查其心跳和消费者状态。先停止 API 与 Worker，再运行 `uv run python scripts/dev_services.py stop`，本地数据会保留。

## 生产配置

- API、Worker 和迁移进程分别注入各自的凭据。运行时设置 `NEXUS_ENVIRONMENT=production`，PostgreSQL 连接使用 `sslmode=verify-full`，Redis 使用带认证的 `rediss://`。给每个网关节点登记独立的数据库身份。[网关权限与接入](plane-protocol.md)
- API 与 Worker 使用相同的 Redis 命名空间和应用加密密钥版本。先将新旧密钥分发到相关进程，再轮换当前版本，并保留历史密文所需的旧密钥。[凭据轮换](catalog-api.md#凭据加密与轮换)
- 账务币种是账务策略的 `currency`，默认 USD。在出现第一个账户、价格、收款账户或支付渠道之前选定，之后修改返回 409 `currency_locked`。
- 启用充值前配置 HTTPS 通知站点、返回地址和 live 商户渠道。先在相应商户环境验证付款、通知、查单及退款对账，再开放收款。启用收费模型前确认实际售价、成本、折扣、用量维度和可执行的输入输出上界。[支付接入](payments.md) · [定价与预算](billing-api.md)
- 按部署容量确定积压阈值、连接预算、保留期、可接受的数据丢失量（RPO）和恢复时间（RTO）。还原时需同时具备数据库备份、应用密钥和网关集群密钥。按实际环境验证跨机恢复、流量峰值和故障切换。[验收与恢复演练](control-plane-acceptance.md)

### 三层配置

配置分三层，每个值只有一个来源：

| 层 | 放什么 | 何时生效 |
| --- | --- | --- |
| Secret | 含凭据的连接串和密钥：`NEXUS_DATABASE_URL`、`NEXUS_MIGRATION_DATABASE_URL`、`NEXUS_REDIS_URL`、`NEXUS_ENCRYPTION_KEY`、`NEXUS_PREVIOUS_ENCRYPTION_KEYS`、`NEXUS_AUTH_PROXY_SECRET`、`NEXUS_SMTP_PASSWORD`、`NEXUS_RESEND_API_KEY`、`NEXUS_CLOUDFLARE_EMAIL_API_TOKEN`、`NEXUS_TURNSTILE_SECRET_KEY`；Relay 的 `NEXUS_RELAY_DATABASE_URL`、`NEXUS_RELAY_REDIS_URL`、`NEXUS_RELAY_PRIVATE_KEY` | 进程重启 |
| ConfigMap（`envFrom` 注入） | 启动参数和单个进程或节点的资源上限：`NEXUS_ENVIRONMENT`、`NEXUS_PROCESS_ROLE`、`NEXUS_HOST`、`NEXUS_PORT`、`NEXUS_ENCRYPTION_KEY_VERSION`、连接池、`NEXUS_WORKER_ID`、`NEXUS_REDIS_NAMESPACE`、`NEXUS_LOG_LEVEL`、`NEXUS_DEPENDENCY_TIMEOUT`、`NEXUS_SHUTDOWN_TIMEOUT`、`NEXUS_WORKER_INTERVAL`、`NEXUS_WORKER_HEARTBEAT_TTL`、`NEXUS_AUTO_PUBLISH_POLL_SECONDS`、Argon2 并发、迁移超时、支付地址、发信服务商与 Turnstile 的非密钥参数、`NEXUS_AUTH_PROXY_PEERS`；Relay 的监听地址、命名空间、上游 CA 路径和单节点上限（见[Relay 运行配置](relay-development.md#运行配置)）；Web 的 5 个变量 | 进程重启 |
| 数据库策略表 | 运营方随时调整、要求全局一致或按企业区分的业务策略（下表） | API 与 Worker 约 2 秒；Relay 随集群的下一次发布 |

ConfigMap 还可以挂载一个策略文件，在 Helm 安装或升级时由 `nexus settings import` 写入数据库：用它给新部署填初始值，或把一部分策略组交给 Git 管理（锁定后后台只读）。文件格式和 Job 示例见[运维：策略文件导入](operations.md#策略文件导入)。

Helm values 的建议结构（部署产物由 k3s 部署负责，这里只约定分层）：

```yaml
secrets:            # 渲染为 Secret，envFrom 注入
  NEXUS_DATABASE_URL: postgresql+psycopg://nexus_api:…@db:5432/nexus?sslmode=verify-full
  NEXUS_REDIS_URL: rediss://:…@redis:6379/0
  NEXUS_ENCRYPTION_KEY: …
config:             # 渲染为 ConfigMap，envFrom 注入
  NEXUS_ENVIRONMENT: production
  NEXUS_LOG_LEVEL: info
  NEXUS_REDIS_NAMESPACE: nexus:v1
  NEXUS_DB_POOL_SIZE: "10"
policies:           # 渲染为 ConfigMap 里的 policies.yaml，挂载给导入 Job
  version: 1
  groups:
    billing.policy: {mode: initial, values: {currency: CNY}}
    publication.cluster_defaults: {mode: managed, values: {relay: {max_attempts: 3}}}
```

```yaml
apiVersion: v1
kind: ConfigMap
metadata: {name: nexus-config}
data:
  NEXUS_ENVIRONMENT: production
  NEXUS_LOG_LEVEL: info
  NEXUS_REDIS_NAMESPACE: nexus:v1
  NEXUS_DB_POOL_SIZE: "10"
---
apiVersion: v1
kind: ConfigMap
metadata: {name: nexus-policies}
data:
  policies.yaml: |
    version: 1
    groups:
      billing.policy: {mode: initial, values: {currency: CNY}}
      publication.cluster_defaults: {mode: managed, values: {relay: {max_attempts: 3}}}
```

### 数据库中的策略

业务策略保存在所属模块的策略表里，每张表只有一个写入模块，带取值范围约束和版本号。平台管理员在管理后台“平台 → 设置”或通过下表的接口修改，写入需要 `If-Match`，版本不符返回 412，取值越界返回 422，每次修改写入所属模块的审计。被策略文件锁定的组拒绝接口写入（409 `policy_managed_externally`）。

| 策略组 | 表 | 接口 | 何时生效 |
| --- | --- | --- | --- |
| `identity.auth_policy`：注册与验证开关、登录与身份邮件限额、会话与密码设置凭据有效期、保留登录名、平台角色邮件找回 | `identity.auth_policy` | `GET/PUT /api/v1/settings/authentication` | 2 秒内，所有 API 与 Worker 进程 |
| `billing.policy`：账务币种、向量存储预付天数与到期策略、支付调用与通知限额 | `billing.settings` | `GET/PUT /api/v1/settings/billing` | 2 秒内；Worker 在每个账务事务中读取 |
| `reporting.export_policy`：导出频率、保留时间、行数上限 | `reporting.export_policy` | `GET/PUT /api/v1/settings/reporting` | 下一次导出 |
| `catalog.quota_defaults`：资源数量（文件、会话、容器、批处理、向量库）平台默认值 | `catalog.resource_quota_defaults` | `GET/PUT /api/v1/settings/resource-quotas` | 集群下一次发布后 |
| `publication.cluster_defaults`：Relay 尝试次数、故障转移时间预算、清理宽限期、续读流时长，自动发布的静默时间和最长延迟 | `publication.cluster_policy_defaults` | `GET/PUT /api/v1/settings/cluster-defaults` | Relay 策略随下一次发布；时间窗在 Worker 下一次检查 |
| 单个集群对上一行的覆盖（null 跟随默认值） | `publication.cluster_policies` | `GET/PUT /api/v1/gateway-clusters/{cluster_id}/policy` | 同上 |
| 单个企业的资源数量覆盖 | `catalog.quota_policies` | `PUT /api/v1/organizations/{organization_id}/quota-policies/platform/organization/{organization_id}/{metric}` | 集群下一次发布后 |

API 与 Worker 每个进程最多缓存策略 `POLICY_TTL_SECONDS`（2 秒），修改所在的进程立即使用新值。开启自动发布的集群在 Relay 策略或资源数量变化后自动发布；手动发布的集群在下一次发布时生效。生效的 Relay 策略或资源数量不等于 Relay 内置值时，快照要求节点声明 `relay.cluster-policy.v1`，见[网关协议](plane-protocol.md#集群策略)。

## 开发检查

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest -q -m 'not integration'
uv run python scripts/run_integration.py -q
uv build
```

集成脚本使用临时 PostgreSQL、Redis 和随机端口，结束后清理；容量用例显式传入 `--capacity`。维护模块目录和依赖时参考[模块组织](architecture.md#python-工程组织)。
