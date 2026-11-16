# k3s 部署手册

Nexus 在 k3s 上从单节点起步：内置 etcd、一台 server 同时承载控制面和全部工作负载。之后加 agent 节点扩计算容量，加到 3 台 server 获得控制面高可用，不需要迁移数据存储。本文覆盖镜像、Helm chart、三层配置、安装、扩容、升级、回滚、密钥轮换、备份恢复和上线检查。项目尚未上线，本文按全新安装编写，不处理存量数据的迁移。仓库中的部署产物都在 [`deploy/`](../deploy) 下。

## 组成

| 组件 | Kubernetes 对象 | 镜像 | 说明 |
| --- | --- | --- | --- |
| 管理 API | Deployment `nexus-api` | `nexus-control-plane` | `nexus api`，经 Traefik 暴露 `/api/` |
| Worker | Deployment `nexus-worker` | `nexus-control-plane` | `nexus worker`，可多副本，见[Worker 副本](#worker-副本) |
| 管理后台（BFF） | Deployment `nexus-web` | `nexus-web` | Next.js standalone，经 Traefik 暴露 |
| Relay | StatefulSet `nexus-relay` | `nexus-relay` | 每个序号是一个网关节点，有独立数据库登录和 node_id |
| 迁移、建库、Relay 节点登记、策略导入 | Helm hook Job | `nexus-control-plane` | 见[发布钩子](#发布钩子) |
| PostgreSQL | CloudNativePG `Cluster nexus-pg` | `ghcr.io/cloudnative-pg/postgresql:17.12` | `nexus-data` chart；也可用外部托管服务 |
| Redis | Deployment `nexus-redis` | `redis:8.10.3-alpine` | `nexus-data` chart；TLS、密码认证、`noeviction` |
| 入口 | k3s 自带 Traefik | — | TLS 终止，cert-manager 签证书 |

两个 chart 分开安装到同一个命名空间（默认 `nexus`）：

- [`deploy/helm/nexus-data`](../deploy/helm/nexus-data)：有状态依赖。数据库 `Cluster` 和备份 `ObjectStore` 带 `helm.sh/resource-policy: keep`，卸载 release 也不会删除数据。
- [`deploy/helm/nexus`](../deploy/helm/nexus)：无状态应用和发布钩子。升级、回滚、卸载都不碰数据。

拆开的原因是 Helm 的 pre-install 钩子在普通资源之前执行：迁移 Job 需要数据库已经存在，数据库就不能和它放在同一个 release 里。

验证过的版本：k3s v1.36.6+k3s1、Helm v4.3.2、cert-manager v1.21.3、CloudNativePG 1.30.1、Barman Cloud 插件 v0.16.0、Traefik（k3s 自带）。

## 镜像

| 镜像 | Dockerfile | 运行 | 压缩后大小 |
| --- | --- | --- | --- |
| `nexus-control-plane` | [control-plane.Dockerfile](../deploy/images/control-plane.Dockerfile) | `python:3.14.7-slim-trixie`，`ENTRYPOINT ["nexus"]` | 约 82 MB |
| `nexus-relay` | [relay.Dockerfile](../deploy/images/relay.Dockerfile) | `distroless/cc-debian13:nonroot`，无 shell | 约 18 MB |
| `nexus-web` | [web.Dockerfile](../deploy/images/web.Dockerfile) | `distroless/nodejs24-debian13:nonroot`，Next.js standalone | 约 70 MB |

- 多阶段构建，运行阶段只复制产物。三个镜像都以 UID 65532 运行，chart 设置 `readOnlyRootFilesystem: true`：Python 字节码在构建时编译，Next.js 缓存写到 emptyDir。
- 依赖锁定：`uv sync --locked`、`cargo build --locked`、`pnpm install --frozen-lockfile`。基础镜像同时固定 tag 和多架构索引摘要，更新时两者一起改。
- 镜像不含任何环境相关配置。管理后台的 `NEXUS_API_URL` 等变量在服务端运行时读取，同一镜像可用于所有环境。
- 从仓库根目录构建：`docker build -f deploy/images/relay.Dockerfile -t nexus-relay .`，其余两个同理。
- CI：[`images.yml`](../.github/workflows/images.yml)。拉取请求只构建 amd64 版本，用来确认能构建成功；`main` 和 `v*` 标签在原生 amd64 与 arm64 runner 上分别构建，按摘要推送到 GHCR，再合成多架构标签（`sha-<commit>`、分支名、版本号），并附带 provenance 和 SBOM。同一 workflow 还会 lint 和渲染两个 chart。生产 values 按摘要引用镜像。

## 三层配置

| 层 | 放什么 | 载体 | 改动方式 |
| --- | --- | --- | --- |
| 密钥 | 连接口令、加密密钥、集群私钥、共享密钥、第三方凭据 | Kubernetes Secret，chart 只引用名称和键 | External Secrets 或 SOPS；改后重启读取它的组件 |
| 启动参数 | 非敏感的启动参数、单节点资源上限 | 每个组件一个 ConfigMap，由 values 的 `config.*` 生成，`envFrom` 注入 | `helm upgrade`；checksum 注解使对应组件滚动重启 |
| 业务策略 | 运营方随时调整、要求全局一致或按企业区分的策略：认证与邮件限额、账务币种与支付限额、导出、资源数量默认值、Relay 集群策略 | 数据库各模块的策略表，以数据库为准，见[数据库中的策略](development.md#数据库中的策略) | 管理后台“平台 → 设置”或 `/api/v1/settings/*`；values 的 `policy.groups` 经导入 Job 写入初始值或锁定由 Git 管理的组 |

应用不读写 k3s 的 etcd，也不调用 Kubernetes API。唯一调用 Kubernetes API 的是 Relay 节点登记 Job，它只读写节点登录 Secret。

### 密钥

chart 中不出现任何明文。生产用 [External Secrets 示例](../deploy/examples/external-secrets.yaml)（Vault KV，可换成云厂商的密钥服务），或 [SOPS 模板](../deploy/examples/sops/secrets.template.yaml)加 [`.sops.yaml`](../deploy/examples/sops/.sops.yaml)，经 Flux 或 `sops -d … | kubectl apply -f -` 应用。

| Secret（默认名） | 键 | 读取者 |
| --- | --- | --- |
| `nexus-db-api`、`nexus-db-worker`、`nexus-db-migrator` | `username`、`password`（basic-auth，标签 `cnpg.io/reload=true`） | 对应进程；同时是 CloudNativePG managed role 的密码来源 |
| `nexus-pg-superuser` | `username`、`password` | 仅建库和 Relay 节点登记 Job；CloudNativePG 生成。外部数据库改用 `secrets.database.admin` |
| `nexus-redis` | `password` | Redis、API、Worker、Relay |
| `nexus-encryption` | `encryption-key`、`previous-encryption-keys`（可选 JSON）；当前版本号在 ConfigMap 的 `NEXUS_ENCRYPTION_KEY_VERSION` | API、Worker、策略导入 Job |
| `nexus-auth-proxy` | `secret`（至少 32 个字符） | 管理后台和 API |
| `nexus-relay-cluster` | `private-key`（Base64 的 32 字节 Curve25519 私钥） | Relay |
| `nexus-relay-node-<序号>` | `username`、`password`、`node-id` | 只有该序号 Relay Pod 的 init 容器；由登记 Job 创建，也可预先由 External Secrets 提供 |
| `nexus-integrations` | `smtp-password`、`resend-api-key`、`cloudflare-email-api-token`、`turnstile-secret-key`，均可选 | API、Worker |
| `nexus-deployer` | `login`、`password` | Relay 节点登记 Job 用这个平台管理员登录 |
| `nexus-backup-r2` | `ACCESS_KEY_ID`、`ACCESS_SECRET_KEY`、`ACCESS_REGION`（R2 为 `auto`） | CloudNativePG 的 WAL 归档和备份；备份 bucket 的 R2 API Token |
| `nexus-app-r2` | `ACCESS_KEY_ID`、`ACCESS_SECRET_KEY` | API、Worker、策略导入 Job，必需；应用 bucket 的 R2 API Token：API 用它签发导出下载地址，Worker 用它上传和删除导出文件 |

数据库和 Redis 口令通过 Kubernetes `$(VAR)` 展开拼进连接 URL，因此只能包含 URL 安全字符，例如 `openssl rand -hex 32` 的输出。需要整条连接串时，把 `secrets.database.<角色>.urlKey` 设为 Secret 中保存完整 URL 的键。

k3s 必须以 `--secrets-encryption` 启动，etcd 里的 Secret 才会加密落盘。etcd 快照仍然包含全部 Secret：解开快照需要 server token，所以快照和 token 要加密保存、分开存放，见[备份](#备份与恢复)。

### 启动参数

`config.controlPlane` 同时进入 API 和 Worker 的 ConfigMap，`config.api`、`config.worker`、`config.web`、`config.relay`、`config.migrate` 分别只进入各自组件。chart 另外生成以下派生键：

| 组件 | 派生键 |
| --- | --- |
| API | `NEXUS_HOST=0.0.0.0`、`NEXUS_PORT=8000` |
| 管理后台 | `NEXUS_API_URL`（集群内 API Service）、`NEXUS_WEB_ORIGIN`（`https://<hosts.console>`） |
| Relay | `NEXUS_RELAY_LISTEN`、`NEXUS_RELAY_METRICS_LISTEN`、`NEXUS_RELAY_CLUSTER_ID`、`NEXUS_RELAY_DRAIN_SECONDS`、`NEXUS_RELAY_NAMESPACE`（等于控制面的 `NEXUS_REDIS_NAMESPACE`）、`NEXUS_RELAY_UPSTREAM_CA` |

Relay 的 ConfigMap 只允许 `NEXUS_RELAY_*` 键，写错组件时渲染失败。CA 证书一律以文件挂载：PostgreSQL CA 在 `/etc/nexus/postgres-ca/ca.crt`，Redis CA 在 `/etc/nexus/redis-ca/ca.crt`。上游私有 CA 来自 `relay.upstreamCA.configMap`，挂到 `/etc/nexus/upstream-ca/`，与公共根证书一起生效。

每个工作负载的 Pod 模板带有自身 ConfigMap 的 `checksum/config` 注解。`helm upgrade` 改了某个组件的配置，只会滚动这个组件；冒烟演练 A 验证了这一点。直接 `kubectl edit` ConfigMap 不会触发重启，配置以 values（Git）为准。

### 业务策略

values 的 `policy.groups` 渲染成策略文件 `policy.yaml`（`{version: 1, groups: {<组名>: {mode, values}}}`），由 pre-install / pre-upgrade 钩子在迁移之后执行 `nexus settings import --file` 写入数据库。导入在任何 Pod 滚动之前完成；文件中任何一组不合法，导入整体拒绝，发布中止，数据库保持原值。命令参数、文件格式和输出见[策略文件导入](operations.md#策略文件导入)。

| 组 | 对应接口 | `values` 示例 |
| --- | --- | --- |
| `identity.auth_policy` | `PUT /api/v1/settings/authentication` | `{sign_in: {per_ip: 100}}` |
| `billing.policy` | `PUT /api/v1/settings/billing` | `{currency: USD}`，只能在出现第一个账户、价格、收款账户或支付渠道之前设定 |
| `reporting.export_policy` | `PUT /api/v1/settings/reporting` | `{exports: {max_rows: 20000}}` |
| `catalog.quota_defaults` | `PUT /api/v1/settings/resource-quotas` | `{vector_stores_per_key: 50}` |
| `publication.cluster_defaults` | `PUT /api/v1/settings/cluster-defaults` | `{relay: {max_attempts: 3}, auto_publish: {quiet_seconds: 5}}` |

每个组选一种模式：

- `initial`：只在该组从未被修改过时写入，之后以管理后台为准，下次部署不会覆盖后台的修改。对已锁定的组声明 `initial` 会解除锁定。
- `managed`：由 Git 管理。每次部署都用文件值覆盖所列字段，并锁定整组：后台设置页只读，接口写请求返回 409 `policy_managed_externally`。

`values` 可以只写部分字段，没写的字段保持数据库现值；文件里没有的组不受影响。单个集群和单个企业的覆盖值属于运行时数据，在后台或接口中维护，不进文件。`policy.groups` 为空时不渲染导入 Job。`policy.dryRun: true` 只在 Job 日志中输出将会发生的变化，不写入。

### 环境变量归属

与[三层配置](development.md#三层配置)一致，每个变量只有一个来源。业务策略不再有环境变量。“派生”指 chart 自动生成，不需要配置。

| 变量 | 层 | 说明 |
| --- | --- | --- |
| `NEXUS_DATABASE_URL` | 密钥 | 口令来自 `nexus-db-api` / `nexus-db-worker`，主机、库名、`sslmode=verify-full` 和 CA 路径由 chart 拼接 |
| `NEXUS_MIGRATION_DATABASE_URL` | 密钥 | 迁移 Job，口令来自 `nexus-db-migrator` |
| `NEXUS_PROVISION_DATABASE_URL`、`NEXUS_PROVISION_*_PASSWORD` | 密钥 | 建库 Job；外部数据库模式才传入三个登录口令 |
| `NEXUS_REDIS_URL` | 密钥 | `rediss://`，口令来自 `nexus-redis`，`ssl_ca_certs` 指向挂载的 CA |
| `NEXUS_ENCRYPTION_KEY`、`NEXUS_PREVIOUS_ENCRYPTION_KEYS` | 密钥 | `nexus-encryption` |
| `NEXUS_AUTH_PROXY_SECRET` | 密钥 | API 与管理后台共用 |
| `NEXUS_SMTP_PASSWORD`、`NEXUS_RESEND_API_KEY`、`NEXUS_CLOUDFLARE_EMAIL_API_TOKEN`、`NEXUS_TURNSTILE_SECRET_KEY` | 密钥 | `nexus-integrations`，均可选 |
| `NEXUS_ENCRYPTION_KEY_VERSION` | 启动参数 | `config.controlPlane`，与 `nexus-encryption` 中的当前密钥对应，轮换时两者在同一次发布中修改 |
| `NEXUS_ENVIRONMENT`、`NEXUS_LOG_LEVEL`、`NEXUS_REDIS_NAMESPACE`、`NEXUS_DEPENDENCY_TIMEOUT`、`NEXUS_SHUTDOWN_TIMEOUT` | 启动参数 | `config.controlPlane` |
| `NEXUS_PROCESS_ROLE` | — | 由 `nexus api` / `nexus worker` 命令决定，不需要设置 |
| `NEXUS_HOST`、`NEXUS_PORT` | 派生 | API |
| `NEXUS_DB_POOL_SIZE`、`NEXUS_DB_MAX_OVERFLOW` | 启动参数 | 单进程连接上限，按 PostgreSQL `max_connections` 分配 |
| `NEXUS_LOGIN_HASH_CONCURRENCY`、`NEXUS_ACCOUNT_HASH_CONCURRENCY`、`NEXUS_HASH_QUEUE_TIMEOUT_SECONDS` | 启动参数 | 单 Pod 的 Argon2 并发，随 CPU 配额调整 |
| `NEXUS_AUTH_PROXY_PEERS` | 启动参数 | 默认 k3s Pod 网段 `["10.42.0.0/16"]`，见[客户端 IP](#真实客户端-ip) |
| `NEXUS_WORKER_ID` | 派生 | Worker 的 Pod 名 |
| `NEXUS_WORKER_INTERVAL`、`NEXUS_WORKER_HEARTBEAT_TTL`、`NEXUS_AUTO_PUBLISH_POLL_SECONDS` | 启动参数 | Worker。自动发布的静默时间和最长延迟是 `publication.cluster_defaults` 策略 |
| `NEXUS_MAIL_PROVIDER`、`NEXUS_MAIL_FROM`、`NEXUS_SMTP_HOST`、`NEXUS_SMTP_PORT`、`NEXUS_SMTP_SECURITY`、`NEXUS_SMTP_USERNAME`、`NEXUS_CLOUDFLARE_EMAIL_ACCOUNT_ID` | 启动参数 | 发信服务商的非密钥参数 |
| `NEXUS_TURNSTILE_SITE_KEY`、`NEXUS_TURNSTILE_HOSTNAME` | 启动参数 | 是否启用 Turnstile 是 `identity.auth_policy` 策略 |
| `NEXUS_PAYMENT_PUBLIC_BASE_URL` | 启动参数 | API 的公开地址，用于支付通知，生产环境必须是 HTTPS。充值完成后的返回页是站点策略的控制台地址（`identity.site_policy.console_url`），不是环境变量 |
| `NEXUS_MOCK_PAYMENT_URL` | — | 生产环境禁止设置 |
| `NEXUS_MIGRATION_LOCK_TIMEOUT_MS`、`NEXUS_MIGRATION_STATEMENT_TIMEOUT_MS` | 启动参数 | `config.migrate`，只进迁移 Job |
| `NEXUS_OBJECT_STORAGE_ENDPOINT_URL`、`NEXUS_OBJECT_STORAGE_BUCKET`、`NEXUS_OBJECT_STORAGE_REGION` | 派生 | 来自 `objectStorage.*`，API、Worker 和策略导入 Job 都必需；`objectStorage.endpointURL` 没有默认值，不填时 chart 渲染失败。见[对象存储](#对象存储cloudflare-r2) |
| `NEXUS_OBJECT_STORAGE_ACCESS_KEY_ID`、`NEXUS_OBJECT_STORAGE_SECRET_ACCESS_KEY` | 密钥 | `nexus-app-r2` |
| `NEXUS_API_URL`、`NEXUS_WEB_ORIGIN` | 派生 | 管理后台 |
| `NEXUS_COOKIE_SECURE`、`NEXUS_WEB_CLIENT_IP_HEADER` | 启动参数 | 管理后台，后者默认 `x-real-ip` |
| `NEXUS_RELAY_DATABASE_URL` | 密钥 | 每个节点一份，init 容器从节点 Secret 生成文件，经 `NEXUS_RELAY_DATABASE_URL_FILE` 读取 |
| `NEXUS_RELAY_REDIS_URL`、`NEXUS_RELAY_PRIVATE_KEY` | 密钥 | 所有节点相同 |
| `NEXUS_RELAY_CLUSTER_ID`、`NEXUS_RELAY_NAMESPACE`、`NEXUS_RELAY_LISTEN`、`NEXUS_RELAY_METRICS_LISTEN`、`NEXUS_RELAY_DRAIN_SECONDS`、`NEXUS_RELAY_UPSTREAM_CA` | 派生 | 来自 `relay.*` values |
| `NEXUS_RELAY_LOG`、`NEXUS_RELAY_POOL_SIZE`、`NEXUS_RELAY_CONCURRENCY`、`NEXUS_RELAY_LEASE_MS`、`NEXUS_RELAY_REQUEST_BYTES`、`NEXUS_RELAY_RESPONSE_BYTES`、`NEXUS_RELAY_EVENT_BYTES`、`NEXUS_RELAY_STREAM_IDLE_MS`、`NEXUS_RELAY_WS_SESSIONS`、`NEXUS_RELAY_PURGE_INTERVAL_MS` | 启动参数 | 单节点资源上限，`config.relay`。连接池按[连接预算](#连接预算)设置 |
| `NEXUS_RELAY_TENANT_ADMISSIONS`、`NEXUS_RELAY_ADMISSION_WAIT_MS` | 启动参数 | 集群范围：同一企业同时处于数据库准入调用中的请求数上限（默认 32，0 关闭）和等待名额的时间（默认 5000 ms，超时返回 429）。企业的 platform 层 `admission_concurrency` 策略覆盖前者，见[企业准入并发](plane-protocol.md#企业准入并发) |
| `NEXUS_RELAY_REQUESTS_PER_TENANT`、`NEXUS_RELAY_STREAMS_PER_TENANT`、`NEXUS_RELAY_WS_SESSIONS_PER_TENANT`、`NEXUS_RELAY_WS_TURNS_PER_TENANT` | 启动参数 | 单节点的租户公平性上限，每个节点独立计数，不是集群级配额 |
| `SSL_CERT_FILE` | 派生 | Relay 的信任库：公共根证书加 Redis CA |
| `NEXUS_GATEWAY_DSN`、`NEXUS_SIMULATION_API_KEY`、`RELAY_ACCEPTANCE_*` | — | 模拟器和验收测试，不部署 |

Relay 的尝试次数、故障转移时间预算、清理宽限期、续读流时长和资源数量上限都是集群策略，随配置快照下发，见[Relay 运行配置](relay-development.md#集群策略)。

### 连接预算

- **Relay 连接池**：`节点数 × NEXUS_RELAY_POOL_SIZE` 不超过 PostgreSQL 可用核数的 2–4 倍。同一企业的预留在企业锁上串行，连接再多也只是多排队；压测中 64 个节点各 8 个连接时，热点企业的突发请求有三分之一返回 503，降到 2 个连接后全部成功（[容量验收](relay-acceptance.md#推荐的默认参数)）。16 个节点以内可以保留默认的 8，64 个节点设为 2。
- **刷新连接**：每个节点另有 1 个配置刷新连接，不占请求连接池。请求的锁等待不会让刷新失败，节点也就不会因此被标为不健康。
- **`/ready`**：连接池耗尽时 `/ready` 仍返回 200，节点不退出负载均衡。连接池耗尽通常是所有节点共有的数据库争用：把节点摘掉不会减轻数据库负载，只会让其余节点承担更多请求，甚至让整个集群同时“不可用”。新请求拿不到连接时等待 5 秒后返回 503，热点企业由准入名额（`NEXUS_RELAY_TENANT_ADMISSIONS`）限流。只有配置刷新失败（例如集群范围失效）或节点并发已满时，`/ready` 才返回 503。
- **PostgreSQL `max_connections`**：不小于 `Relay 节点数 × (NEXUS_RELAY_POOL_SIZE + 1) + API 副本 × (NEXUS_DB_POOL_SIZE + NEXUS_DB_MAX_OVERFLOW) + Worker 副本 × NEXUS_DB_POOL_SIZE + 运维余量（约 30）`。chart 默认值：16 个节点槽 × 9 + 2 × 5 + 2 × 5 + 30 = 194（2 个 API、2 个 Worker），`nexus-data` 的 `postgresql.parameters.max_connections` 为 200。
- **`statement_timeout`**：Relay 节点登录的 `statement_timeout`（8 秒）是角色设置，由 relay-node Job 的 `ensure_gateway_login` 写入，在 PgBouncer 的 transaction 模式下也生效。Relay 启动时发现它为 0 会拒绝启动。

## 单节点安装

### 主机要求

- 磁盘必须是 SSD 或 NVMe，并且 fsync 延迟低。etcd 每次写入都要 fsync，延迟高时租约续期超时，控制器管理器会失去 leader 身份，k3s 随之退出重启。冒烟虚拟机上，一次性应用 CloudNativePG 的大块 CRD 时 etcd 写入耗时 0.8 秒，就复现了这个过程。上线前可用 `fio --rw=write --ioengine=sync --fdatasync=1 --bs=2300 --size=22m` 测量，fdatasync 的 99 分位应在 10 ms 以下。
- 起步规格：8 vCPU、16 GiB 内存、200 GiB NVMe。etcd 数据目录 `/var/lib/rancher/k3s/server/db` 和 PostgreSQL 卷最好放在不同磁盘上，至少不要和日志共用。
- 时钟同步（chrony 或 systemd-timesyncd）。开放 443/TCP，必要时开放 80/TCP（ACME HTTP-01 和跳转）；6443 只对管理网开放。

### 安装 k3s

把 [`deploy/k3s/config.yaml`](../deploy/k3s/config.yaml) 复制到 `/etc/rancher/k3s/config.yaml`，填写 R2 设置（account_id 和备份 bucket 的 Token）和 `tls-san`，权限设为 0600。其中关键设置：

- `cluster-init: true`：从第一台就使用内置 etcd，以后加 server 不用迁移数据存储。
- `secrets-encryption: true`：Secret 加密落盘。安装后执行 `k3s secrets-encrypt status`，确认 `Encryption Status: Enabled`。
- `etcd-snapshot-schedule-cron: "0 */6 * * *"`、`etcd-snapshot-retention: 28`、`etcd-s3: true` 和 `etcd-s3-*`：每 6 小时做一次快照，上传到 R2 备份 bucket 的 `etcd/<环境>/` 前缀下（`etcd-s3-endpoint: <account_id>.r2.cloudflarestorage.com`、`etcd-s3-region: auto`、`etcd-s3-bucket`、`etcd-s3-folder`）。k3s 按保留份数同时清理本地和 R2 上的旧快照，28 份约等于 7 天。R2 凭据可以写在这个文件里，也可以放进 kube-system 下由 `etcd-s3-config-secret` 指定的 Secret。另外要离线保存一份：在新主机上恢复时，集群和它的 Secret 都还不存在。

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.36.6+k3s1 sh -s - server
```

```bash
sudo k3s secrets-encrypt status
```

```bash
sudo cat /var/lib/rancher/k3s/server/token
```

etcd 快照包含全部 Secret。开启 `--secrets-encryption` 后，快照里的 Secret 是用加密密钥加密的，而这个密钥（`/var/lib/rancher/k3s/server/cred/encryption-config.json`）连同其他引导数据又由 server token 保护。没有 token 和加密密钥，快照就无法恢复，所以把 token 和 `encryption-config.json` 一起离线保存，与 R2 上的快照分开存放；每次执行 `k3s secrets-encrypt rotate-keys` 之后要重新保存 `encryption-config.json`。

主机设置了 `HTTP_PROXY`/`HTTPS_PROXY` 时，安装脚本会把它们写进 `/etc/systemd/system/k3s.service.env`。k3s 会自动把集群网段加入 `NO_PROXY`，但不会加入节点自己的地址；API server 访问 kubelet 的请求（`kubectl logs`）于是经过代理并失败。把各节点地址、`.svc` 和 `.cluster.local` 追加到 `NO_PROXY`/`no_proxy`，然后重启 k3s。

### 集群组件

```bash
kubectl apply -f deploy/k3s/traefik-config.yaml
```

```bash
kubectl apply --server-side -f https://github.com/cert-manager/cert-manager/releases/download/v1.21.3/cert-manager.yaml
```

```bash
kubectl apply --server-side -f https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.30/releases/cnpg-1.30.1.yaml
```

```bash
kubectl apply -f https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/v0.16.0/manifest.yaml
```

另外创建一个 ACME `ClusterIssuer`（例如 `letsencrypt`，HTTP-01 使用 `ingressClassName: traefik`），并在 values 的 `ingress.clusterIssuer` 中引用它。

[Traefik 设置](../deploy/k3s/traefik-config.yaml)包括：

- `externalTrafficPolicy: Local`，保留客户端源地址。
- 80 端口跳转到 443。
- websecure 入口的 `readTimeout: 0`、`writeTimeout: 0`，避免 Go HTTP 服务的超时截断 SSE 流和 WebSocket 会话。Traefik v3 的默认 `readTimeout` 是 60 秒。
- 不信任客户端发来的 `X-Forwarded-*` 和 `X-Real-Ip`，并开启访问日志。

Traefik 原生支持 Upgrade，WebSocket 不需要额外配置。

### 真实客户端 IP

Traefik 用 `X-Real-Ip` 传递客户端地址，来源有两种：

- `externalTrafficPolicy: Local`：ServiceLB 只把连接转给本节点上的 Traefik，不做 SNAT。
- 前面另有 L4 负载均衡时，改用 proxy protocol（`ports.websecure.proxyProtocol.trustedIPs`）。

管理后台按 `NEXUS_WEB_CLIENT_IP_HEADER=x-real-ip` 读取地址，用 `NEXUS_AUTH_PROXY_SECRET` 签名后转给 API。BFF Pod 每次重启都会换 IP，所以 `NEXUS_AUTH_PROXY_PEERS` 改为接受网段，默认是 k3s Pod 网段 `10.42.0.0/16`。API 仍然只采信签名正确的地址。Traefik 的 Pod 也在这个网段里，因此 API 的入口路由用一个 middleware 删除客户端自带的 `X-Nexus-Client-IP*` 头。冒烟测试会发送伪造的 `X-Real-Ip`，确认入口把它替换成了真实的来源地址。

## 对象存储（Cloudflare R2）

备份和应用各用一个 R2 bucket：

| bucket | 前缀 | 内容 | 写入者 | Secret / 凭据 |
| --- | --- | --- | --- | --- |
| `nexus-backups` | `postgres/<环境>/` | WAL 归档和基础备份 | CloudNativePG（Barman Cloud 插件） | `nexus-backup-r2` |
| `nexus-backups` | `etcd/<环境>/` | k3s etcd 快照 | k3s server | `config.yaml` 中的 `etcd-s3-access-key` 等 |
| `nexus-app` | `exports/<企业 ID>/<导出 ID>/` | 导出 CSV，每次执行一个对象（`<代次>.csv`） | Worker 上传和删除；API 只在本地签发下载地址 | `nexus-app-r2` |

- **端点**：`https://<account_id>.r2.cloudflarestorage.com`，region 固定为 `auto`。CloudNativePG 的 `endpointURL` 带 `https://`；k3s 的 `etcd-s3-endpoint` 不带协议。
- **API Token**：在 Cloudflare 控制台的 R2 → Manage API Tokens 中签发，权限选 **Object Read & Write**，并限定到单个 bucket：备份 bucket 一个、应用 bucket 一个。CloudNativePG 和 k3s 可以共用备份 bucket 的 Token；分开签发则可以单独吊销。恢复演练另签一个只读（Object Read only）的 Token。Token 的 Access Key ID 和 Secret Access Key 只显示一次，直接存进密钥库。
- **数据驻留**：有驻留要求时，创建 bucket 时就指定 jurisdiction，例如 `wrangler r2 bucket create nexus-backups --jurisdiction eu`。之后端点改为 `https://<account_id>.eu.r2.cloudflarestorage.com`。jurisdiction 创建后不能更改，数据也不能在 jurisdiction 之间搬移。
- **其他 S3 兼容存储**：配置项完全相同，只换 endpoint 和凭据。冒烟用的本地替身就是这样配置的。
- **应用 bucket**：values 的 `objectStorage.endpointURL` 必填，`bucket`、`region` 默认是 `nexus-app` 和 `auto`，凭据来自 `objectStorage.credentialsSecret`（默认 `nexus-app-r2`）。API 和 Worker 缺少任何一项都无法启动，没有退回数据库存储的选项。bucket 保持私有，不开公开访问，也不需要自定义域名或 CORS：用户点击下载时，API 核验权限后返回一个 5 分钟有效的预签名 GET 地址，浏览器直接到 `endpointURL` 下载，管理后台的 BFF 不中转文件。因此 `endpointURL` 必须是用户浏览器能访问的地址，R2 的 `https://<account_id>.r2.cloudflarestorage.com` 满足这一点；换成集群内部的 S3 服务时要另行暴露。文件的存放、代次和清理见[运维：导出和审计](operations.md#导出和审计)。

### 保留期

CloudNativePG 的 `retentionPolicy`（默认 `30d`）决定恢复窗口：它会保留恢复窗口之前最新的那一个基础备份，以及之后的全部 WAL，再删除更早的内容。所以需要保留的最旧对象，可能比 30 天还早一个基础备份周期。R2 的生命周期规则只作为兜底，过期天数必须大于“`retentionPolicy` + 基础备份间隔 + 余量”，否则 R2 会先删掉 CloudNativePG 恢复时还要用的基础备份或 WAL。

[`deploy/examples/r2/lifecycle-backups.json`](../deploy/examples/r2/lifecycle-backups.json) 给出了按这个原则设置的规则：

| 前缀 | 过期 | 依据 |
| --- | --- | --- |
| `postgres/` | 45 天 | 30 天窗口 + 每日基础备份 + 两周余量 |
| `etcd/` | 30 天 | k3s 自己只保留约 7 天，这条规则只清理遗留的快照 |
| 全部 | 未完成的分段上传 7 天后清理 | — |

可以在控制台的 bucket 设置 → Object lifecycle rules 中录入这些规则，也可以用 S3 API（`aws s3api put-bucket-lifecycle-configuration --endpoint-url https://<account_id>.r2.cloudflarestorage.com --bucket nexus-backups --lifecycle-configuration file://lifecycle-backups.json`）。调大 `retentionPolicy` 或拉长基础备份间隔时，要同步调大 `postgres/` 的过期天数。

应用 bucket 的导出文件由 Worker 在导出过期时删除（删除失败会重试），生命周期规则只兜底清理没删掉的对象，例如上传后、提交前崩溃且导出随后过期的那次执行留下的文件。规则的过期天数不能短于导出的最长保留期：`exports.ttl_seconds` 的上限是 2592000 秒（30 天），从导出创建时算起，而对象在创建之后才上传，所以 31 天的规则不会先于数据库删掉仍可下载的文件。[`deploy/examples/r2/lifecycle-app.json`](../deploy/examples/r2/lifecycle-app.json)：

| 前缀 | 过期 | 依据 |
| --- | --- | --- |
| `exports/` | 31 天 | `exports.ttl_seconds` 上限 30 天 + 1 天余量 |
| `exports/` | 未完成的分段上传 1 天后清理 | 导出文件不超过 16 MiB，用单次上传 |

```bash
aws s3api put-bucket-lifecycle-configuration --endpoint-url https://<account_id>.r2.cloudflarestorage.com --bucket nexus-app --lifecycle-configuration file://deploy/examples/r2/lifecycle-app.json
```

R2 的生命周期按对象上传时间计算，删除最多可能延后一天左右。代码里调高 `exports.ttl_seconds` 的上限时，要同步调大这条规则。

## 首次部署

1. **R2 与 Secret**：按[对象存储](#对象存储cloudflare-r2)创建两个 bucket、两个 Token 和生命周期规则；再按[密钥](#密钥)一节创建全部 Secret，`nexus-deployer` 暂时留空。`nexus-data` 的 values 中填写 `postgresql.backup.endpointURL` 和 `prefix`（例如 `postgres/production`）。
2. **数据层**：

   ```bash
   helm upgrade --install nexus-data deploy/helm/nexus-data -n nexus --create-namespace -f my-data-values.yaml --wait
   ```

   ```bash
   kubectl -n nexus wait --for=condition=Ready cluster/nexus-pg --timeout=10m
   ```

   CloudNativePG 创建数据库 `nexus` 和 managed roles：模块组角色和 `nexus_bridge`、`nexus_gateway` 都是 NOLOGIN；`nexus_api`、`nexus_worker`、`nexus_migrator` 是 NOINHERIT 的 LOGIN。同时开启到 R2 的 WAL 归档，并立即做一次基础备份，之后每天 03:00 一次。
3. **应用**：

   ```bash
   helm upgrade --install nexus deploy/helm/nexus -n nexus -f my-values.yaml --wait --timeout 15m
   ```

   `my-values.yaml` 至少填写 `hosts.*` 和 `objectStorage.endpointURL`（参考 [`deploy/examples/values-production.yaml`](../deploy/examples/values-production.yaml)）。首次安装时保持 `relay.replicas: 0`。pre-install 钩子依次执行建库（授予权限）、迁移和策略导入。
4. **初始化**：迁移已经创建内置管理员，默认邮箱 `admin@example.com`、登录名 `admin`、密码 `password`。在这一步完成之前，管理 API 只接受登录和初始化向导，其余接口返回 409 `setup_required`。安装一结束就打开 `https://<hosts.console>`，用默认账号登录，按向导填写站点信息（控制台地址填 `https://<hosts.console>`）、换成自己的邮箱和至少 16 个字符的新密码。提交后默认密码立即失效，内置管理员的其他会话全部退出。细节见[初始管理员](development.md#初始管理员)。

   默认账号是公开的：安装完成到你完成向导之间，别人也可能抢先登录并完成向导。如果你登录时发现向导已经完成、不是你做的，就重新安装：`helm uninstall nexus`，删除并重建数据库（`nexus-data` 的 `Cluster` 和 PVC，或外部数据库里的 `nexus` 库），再从第 2 步开始。只是忘了密码时，在 API Pod 里执行 `nexus setup reset`（见下文[重置内置管理员](#重置内置管理员)）。

   然后按 [登录与企业成员](identity-api.md) 建立日常账号。为 Relay 节点登记 Job 建一个专用平台管理员，例如 `deployer`，把它的登录名和密码写入 `nexus-deployer`。
5. **目录与计价**：配置模型、上游、凭据、部署（含 `relay_profile`）、售价、成本价、路由和企业。见[模型与上游目录](catalog-api.md)和[定价](billing-api.md)。
6. **集群**：用 `nexus-relay-cluster` 私钥对应的公钥调用 `POST /api/v1/gateway-clusters`，记下返回的 `id`。公钥可以这样计算：

   ```bash
   kubectl -n nexus get secret nexus-relay-cluster -o jsonpath='{.data.private-key}' | base64 -d | kubectl -n nexus exec -i deploy/nexus-api -- python -c 'import base64,sys;from nacl.public import PrivateKey;print(base64.b64encode(bytes(PrivateKey(base64.b64decode(sys.stdin.read())).public_key)).decode())'
   ```

7. **Relay 节点**：

   ```bash
   helm upgrade nexus deploy/helm/nexus -n nexus -f my-values.yaml --set relay.clusterId=<集群 ID> --set relay.replicas=2 --wait
   ```

   登记 Job 为序号 0、1 建立数据库登录和 Secret，并登记 principal。之后把这两个值写回 values 文件。
8. **发布**：新集群默认开启 auto_publish，Worker 会自动发布；也可调用 `POST /gateway-clusters/{id}/releases` 手动发布。`GET /gateway-clusters/{id}/principals` 中每个节点的 `applied_revision` 都追上最新修订后，Relay 才会就绪。

### 发布钩子

| 时机 | 权重 | Job | 作用 |
| --- | --- | --- | --- |
| pre-install / pre-upgrade | -10 | `provision-database` | `nexus provision-database`：补齐组角色、建库和权限，可重复执行。复用 `scripts/native_services.py` 的逻辑（`nexus.infrastructure.provisioning`）。CloudNativePG 模式下等待 managed roles 出现，外部数据库模式下用 Secret 中的口令创建登录 |
| pre-install / pre-upgrade | -5 | `relay-nodes-pre` | 建立节点登录和 Secret。API 已在运行时（即升级时）同时登记 principal |
| pre-install / pre-upgrade | 0 | `migrate` | `nexus migrate`，使用迁移角色和 `lock_timeout`。失败即中止发布，此时还没有任何 Pod 滚动 |
| pre-install / pre-upgrade | 5 | `policy-import` | `nexus settings import --file`，`policy.groups` 非空时才有。要求数据库已是最新修订，只连 PostgreSQL |
| post-install / post-upgrade | 5 | `relay-nodes-post` | 等 API 就绪后登记全部节点，可重复执行 |

节点登记放在迁移之前：迁移完成后，旧版 API 会因为 schema 修订不一致而变为未就绪（见[升级](#升级)），之后就无法通过它登记节点了。

### Worker 副本

Worker 默认 2 个副本，按主机分散，并有 `maxUnavailable: 1` 的 PodDisruptionBudget。每个后台任务领取工作时都用 `FOR UPDATE SKIP LOCKED` 或数据库租约：结算按钱包领取一批任务，出箱、邮件、导出、对账、发布分发都是逐行领取，自动发布按集群取得 120 秒租约。代码见 `billing/persistence/*`、`identity/persistence/email.py`、`reporting/persistence/repository.py` 和 `publication/persistence/repository.py`。每个副本以 Pod 名作为 `NEXUS_WORKER_ID`，心跳互不覆盖。一个副本退出后，另一个立即接手它的钱包；Worker 所在节点故障时也不用等它重新调度。单节点的测试环境可以设 `worker.replicas: 1`。扩容前先看 ready 任务数和热点钱包，见[结算 Worker](operations.md#结算-worker)。

## 扩容 Relay 节点

Relay 不能当成普通的无状态副本扩容：每个节点要有自己的 PostgreSQL LOGIN（继承 `nexus_gateway`），并且登记为集群的一个 principal。chart 把这些都交给登记 Job，扩容只需要一条命令：

```bash
helm upgrade nexus deploy/helm/nexus -n nexus -f my-values.yaml --set relay.replicas=3 --wait
```

1. pre-upgrade 的 `relay-nodes-pre` 读取 Secret `nexus-relay-node-2`；不存在就生成随机口令和 node_id 并创建它。如果数据库里该登录已经登记过 node_id，就沿用原来的 node_id。随后建立登录 `nexus_relay_node_2`（只属于 `nexus_gateway`，已存在时把口令重设为 Secret 中的值），并调用 `PUT /api/v1/gateway-clusters/{id}/principals/{node_id}` 登记。
2. StatefulSet 增加序号 2。init 容器只从投影卷中取出本序号的口令，写成内存卷里的连接 URL 文件。Relay 容器本身看不到其他节点的口令。
3. post-upgrade 的 `relay-nodes-post` 再登记一遍。auto_publish 下 Worker 会发布新修订，新节点应用之后变为就绪。

- 每个 Pod 都预先列出 `relay.nodeSlots`（默认 16）个可选的凭据位置。因此在这个上限以内改 `replicas`，不会改变 Pod 模板，已有节点不会滚动重启。
- 节点能力在 `relay.capabilities` 中设置。默认为空，登记控制平面的基线能力，也就是本版本 Relay 支持、每个快照都要求的全部能力；只有节点支持基线之外的能力时才需要列出，列出后会替换基线，所以要包含基线。
- 缩容时把 `replicas` 调小，最高序号的节点会排空后退出。确认它不再需要后，用 `PUT …/principals/{node_id}` 把 `active` 设为 false。登录和 Secret 会保留，以便以后重新扩回来。
- 不经 Helm 时，`deploy/helm/nexus/files/relay_nodes.py --phase post` 就是同一套逻辑。它需要 `ADMIN_DATABASE_URL`、`API_URL`、`RELAY_*` 和 `DEPLOYER_*` 环境变量，以及读写 Secret 的权限。

### Relay 排空

| 阶段 | 时长 | 发生什么 |
| --- | --- | --- |
| preStop | `relay.preStopSeconds`，默认 15 秒 | Pod 已被标记为终止，Traefik 把它从后端摘除，节点照常处理请求 |
| SIGTERM | — | 停止接受新连接，`/ready` 不再应答，指标监听上的 `/live` 继续应答，所以存活探针不会误杀 |
| 连接排空 | `relay.drainSeconds`，默认 120 秒（`NEXUS_RELAY_DRAIN_SECONDS`） | 进行中的 SSE 流和 WebSocket 当前轮次正常结束；到时仍未结束的连接被关闭，结果按客户端断开或未知上报并进入核对 |
| 完成上报 | 最多 90 秒 | 把在途请求的 `attempt.finished` 和 `request.closed` 提交给网关 |

`terminationGracePeriodSeconds` 默认等于 preStop + drain + 90 + 15 秒（默认值下为 240 秒），也可以直接设置。StatefulSet 每次只滚动一个节点（`maxUnavailable: 1`），新 Pod 就绪 10 秒后才继续下一个；PodDisruptionBudget 同样限制为每次一个。生成时间可能超过 `drainSeconds` 的模型，需要相应调大这个值。

## 升级

```bash
helm upgrade nexus deploy/helm/nexus -n nexus -f my-values.yaml --wait --timeout 20m
```

1. **先迁移**：pre-upgrade 钩子依次执行建库、节点登记、`nexus migrate` 和策略导入，迁移使用默认 5 秒的 `lock_timeout`。迁移失败时发布中止，Pod 都不会滚动。排查长事务后重新执行 `helm upgrade`，会从失败的修订继续。
2. **再滚动**：API、Worker 和管理后台按 `maxSurge: 1`、`maxUnavailable: 0` 逐个替换，Relay 每次替换一个节点，并按上节排空。
3. **schema 修订变化时的窗口**：API 的就绪检查要求数据库修订与代码完全一致。迁移完成后，旧版 API Pod 会变为未就绪，直到新版 Pod 就绪，这段时间管理 API 和管理后台不可用，一般在 1 分钟以内。Relay 直接调用 `gateway_v1`，不受影响。不改 schema 的升级没有这个窗口。

## 回滚

- **schema 未变化**：`helm rollback nexus <修订号>` 会滚回镜像和配置。回滚不会执行迁移钩子。
- **schema 已变化**：`nexus migrate` 只会升级，旧版 API 在新 schema 上不会就绪。可选的做法：
  - 优先前滚，修复后再发一个新版本；
  - 确需退回时，在维护窗口内用迁移角色执行 Alembic 降级（`alembic downgrade <修订>`，配置见 [`alembic.ini`](../alembic.ini)），再执行 `helm rollback`。部分修订在会丢失保留数据时会拒绝降级，此时整次降级回滚，不留下半降级的状态；
  - 最后的手段是把 PostgreSQL 恢复到升级前的时间点，见下文。这会丢失升级之后写入的数据。
- 应用回滚不涉及数据层 release。

## 密钥轮换

Secret 的变化不会触发 checksum 重启（checksum 只跟踪 ConfigMap），所以更新 Secret 后要手动重启读取它的组件。也可以部署 Reloader 一类控制器自动完成。

| 密钥 | 步骤 |
| --- | --- |
| 应用加密密钥 | 以当前版本 1 换成版本 2 为例。1. 在 `nexus-encryption` 中设置 `previous-encryption-keys` 为 `{"2":"<新密钥>"}`，重启 API 和 Worker，让所有实例都能解密版本 2；2. 把 `encryption-key` 改为新密钥、`previous-encryption-keys` 改为 `{"1":"<旧密钥>"}`，同时把 values 的 `config.controlPlane.NEXUS_ENCRYPTION_KEY_VERSION` 改为 `"2"`，执行 `helm upgrade`（ConfigMap 变化会滚动 API 和 Worker）。旧记录不会自动重新加密：仍有密文引用旧版本时，旧密钥必须保留。按[凭据轮换](catalog-api.md#凭据加密与轮换)重建上游凭据之后，才能删除旧版本 |
| 数据库口令 | 更新 basic-auth Secret。带 `cnpg.io/reload` 标签时 CloudNativePG 会立即修改角色口令，之后马上执行 `kubectl rollout restart deploy/nexus-api deploy/nexus-worker`。已建立的连接不受影响，旧 Pod 新建连接会失败，直到重启完成。外部数据库模式下，执行 `helm upgrade` 由建库 Job 修改口令 |
| Relay 节点口令 | 修改 `nexus-relay-node-<n>` 的 `password`，执行 `helm upgrade`（登记 Job 会把登录口令改成 Secret 中的值），再 `kubectl rollout restart statefulset/nexus-relay` |
| 集群私钥 | 生成新密钥对，用 `PATCH /gateway-clusters/{id}` 提交新公钥并设置 `key_grace_seconds`，然后更新 `nexus-relay-cluster`，并在宽限期内滚动重启 Relay |
| BFF 共享密钥 | 更新 `nexus-auth-proxy`，同时重启 API 和管理后台。两者不一致的短暂期间，登录限流退回按直连地址计数，不会出错 |
| Redis 口令 | 更新 `nexus-redis`，重启 Redis 以及 API、Worker、Relay。Redis 重启后，带限流策略的新准入要等 60 秒恢复窗口，应安排在低峰时段 |
| k3s Secret 加密密钥 | `k3s secrets-encrypt rotate-keys`，完成后用 `k3s secrets-encrypt status` 确认 |
| TLS 证书 | cert-manager 自动续期。Redis 不会重新加载证书文件，内部证书有效期为 1 年、提前 30 天续期，续期后要重启 Redis |
| 部署账号 | 在后台修改 `deployer` 的密码，并同步更新 `nexus-deployer` |

### 重置内置管理员

忘记内置管理员的密码，或需要重新走一遍初始化时，在 API Pod 里执行：

```bash
kubectl -n nexus exec -it deploy/nexus-api -- nexus setup reset
```

命令要求输入 `RESET` 确认（非交互执行时加 `--yes`）。它把内置管理员恢复成 `admin@example.com` / `password` 并恢复 `platform_admin`，吊销它的全部会话，同时把系统改回“需要初始化”：管理 API 在 2 秒内重新锁住，所有控制台路由都转到 `/setup`，直到有人再次完成向导。其他账号、企业和账务数据不受影响。重置期间默认账号又是公开的，执行后立即登录完成向导。

重置只收回内置管理员。如果怀疑默认账号曾被别人抢先使用，对方可能已经建了其他管理员或调用 Key，应当重新安装而不是重置。

## 扩展成集群

- **加计算容量**：加 agent 节点即可。在新主机上执行 `curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.36.6+k3s1 K3S_URL=https://<server>:6443 K3S_TOKEN=<agent token> sh -s - agent`。API、管理后台、Relay 带有 `topologySpreadConstraints`，会分散到各节点；Traefik 以 DaemonSet 运行在每个节点上；Worker 可调到 2 个副本。
- **控制面高可用**：需要 3 台 server（etcd 要求奇数个成员，2 台反而降低可用性）。后两台使用相同的 `config.yaml`，但不写 `cluster-init`，改为 `server: https://<第一台>:6443` 和 `token`。前面放一个固定地址（VIP 或 DNS 轮询），并加进 `tls-san`。
- **数据库高可用**：至少有 3 个节点、并且存储能跨节点后，把 `nexus-data` 的 `postgresql.instances` 调到 3。CloudNativePG 会建立流复制备库并自动切换主库。应用连接的 `nexus-pg-rw` 始终指向主库。
- 入口地址从单台主机改为负载均衡后，按[真实客户端 IP](#真实客户端-ip)选择 `Local` 或 proxy protocol。

## 存储迁移

k3s 默认的 local-path 存储把卷绑定在创建它的节点上。多节点之后，有两种方式迁移到能跨节点的存储（Longhorn、云盘 CSI 或 NFS 之外的块存储）：

1. **PostgreSQL**：不要直接搬 PVC。把 `postgresql.instances` 调到 2，并为新实例指定新的 `storageClass`（CloudNativePG 支持在扩容时用新存储类创建副本）。副本追平后执行 switchover（`kubectl cnpg promote nexus-pg <新实例>`），再删除旧实例的 PVC。也可以用新存储类新建一个 `Cluster`，以 `bootstrap.recovery` 从对象存储恢复，然后切换应用，切换期间需要维护窗口。
2. **Redis**：不持久化，换节点或换存储都不需要迁移。
3. **etcd**：数据目录在主机磁盘上。换盘按 k3s 的快照恢复流程操作（`--cluster-reset --cluster-reset-restore-path`）。

## 使用外部托管 PostgreSQL

`nexus-data` 设 `postgresql.enabled=false`。`nexus` 设置：

```yaml
postgresql:
  mode: external
  host: nexus.xxxx.rds.amazonaws.com
  port: 5432
  database: nexus
  maintenanceDatabase: postgres
  sslmode: verify-full
  ca: {configMap: rds-ca-bundle, key: ca.crt}
secrets:
  database:
    admin: {name: nexus-db-admin}   # username/password，需要 CREATEROLE 和 CREATEDB
```

- 建库 Job 用 `nexus-db-*` 中的口令创建 `nexus_api`、`nexus_worker`、`nexus_migrator`，以后执行 `helm upgrade` 时会把口令同步成 Secret 中的值。
- 托管服务要求 PostgreSQL 16 或更新版本，并支持 PostgreSQL 16 的 CREATEROLE 语义：创建者自动获得所建角色的 ADMIN OPTION。管理员角色不需要是超级用户。
- WAL 归档、时间点恢复和高可用都由托管服务负责，备份保留期和 RPO/RTO 按服务的配置验收。
- NetworkPolicy 管不到集群外的数据库。改为在数据库安全组中只放行 k3s 节点（或出口网关）的地址，并依靠数据库角色权限隔离组件。

## 备份与恢复

恢复需要四样东西，缺一不可：

| 内容 | 备份方式 | 存放 |
| --- | --- | --- |
| etcd 快照（含全部 Secret、节点登录 Secret、证书） | k3s 定时快照，上传到 R2；外加 server token 和 `encryption-config.json` | R2 `nexus-backups/etcd/`；token 和加密配置离线保存 |
| PostgreSQL | CloudNativePG Barman Cloud 插件：持续 WAL 归档加每日基础备份，可恢复到任意时间点 | R2 `nexus-backups/postgres/` |
| 应用加密密钥（`nexus-encryption`，包括全部历史版本） | 密钥库（Vault 等），同时离线保存 | 与数据库备份分开 |
| 网关集群私钥（`nexus-relay-cluster`） | 同上 | 同上 |

etcd 快照里本来就有后两样，但它们必须独立保存一份：只有数据库备份、没有 etcd 快照（例如换了一个新集群）时，仍然要能恢复。数据库登录口令和 Relay 节点 Secret 丢失后可以重建：登记 Job 会把节点口令重设为新 Secret 的值，并沿用数据库中的 node_id。

### 演练：从 R2 恢复（每季度一次，以及每次重大升级前）

1. **etcd**：
   - 先手动做一个快照：`sudo k3s etcd-snapshot save --name drill`，然后用 `sudo k3s etcd-snapshot ls` 确认列表里有 `s3://nexus-backups/etcd/<环境>/drill-…`。
   - 在一台临时主机上，放好离线保存的 `encryption-config.json`（`/var/lib/rancher/k3s/server/cred/`），然后从 R2 恢复：

     ```bash
     sudo k3s server --cluster-reset --token=<server token> --etcd-s3 --etcd-s3-endpoint=<account_id>.r2.cloudflarestorage.com --etcd-s3-region=auto --etcd-s3-bucket=nexus-backups --etcd-s3-folder=etcd/<环境> --etcd-s3-access-key=<key> --etcd-s3-secret-key=<secret> --cluster-reset-restore-path=<快照名>
     ```

   - 正常启动 k3s 后，确认 `kubectl -n nexus get secret nexus-encryption -o yaml` 能读出内容。
2. **PostgreSQL 时间点恢复**：
   - 用 [`deploy/examples/restore/postgres-pitr.yaml`](../deploy/examples/restore/postgres-pitr.yaml) 在 `nexus-drill` 命名空间引导一个新集群。它从 R2 读取生产的基础备份和 WAL，回放到 `recoveryTarget.targetTime`。凭据使用只读 Token，新集群也不配置 WAL 归档，确保演练不会写进生产归档。
   - `Cluster` 就绪后，检查 `nexus_meta.alembic_version`，以及目标时间点前后各写入一笔的数据：前一笔存在，后一笔不存在。
3. **应用**：在该命名空间安装 `nexus`，`postgresql.clusterName` 指向恢复出的集群，加密密钥和集群私钥从密钥库取得。然后检查：
   - API 就绪；
   - 手动发布一次，Relay 能解密上游凭据；
   - 账户余额、占用与恢复点一致；
   - Worker 补结在途请求后，余额和流水能对上，判断标准与[物理备份恢复](control-plane-acceptance.md#物理备份恢复)相同。
4. 记录 RPO（最后一个已归档 WAL 的时间）和 RTO（从开始恢复到 API 就绪），与目标对比。演练结束后删除 `nexus-drill` 命名空间和临时主机。

## 上线前检查清单

**平台**

- [ ] 磁盘 fsync 延迟达标；`k3s secrets-encrypt status` 为 Enabled；server token 已离线保存。
- [ ] R2 的 `nexus-backups` 和 `nexus-app` 已创建（需要时指定 jurisdiction）；两个 Token 都只能访问各自的 bucket；两个 bucket 的生命周期规则（含 `nexus-app` 的 `exports/` 31 天）按[保留期](#保留期)设置。
- [ ] etcd 定时快照已经出现在 R2 中，并完成过一次从 R2 的恢复演练；server token 和 `encryption-config.json` 已离线保存。
- [ ] `nexus-pg` 的 `ContinuousArchiving` 状态为 True，基础备份成功，完成过一次从 R2 的时间点恢复演练。
- [ ] 四样恢复材料都存放在集群之外。
- [ ] ACME 证书签发成功；HTTP 跳转到 HTTPS；HSTS 已生效。
- [ ] NetworkPolicy 生效：管理后台连不上 5432/6379，入口访问不到 Relay 的 9464、`/live`、`/ready` 和 API 的 `/internal`；Prometheus 能抓取指标。
- [ ] 告警已配置：Relay 指标（[建议告警](operations.md#指标)）、API `/internal/metrics/prometheus`、CloudNativePG、证书到期、etcd 快照失败。
- [ ] `NEXUS_ENVIRONMENT=production`，没有设置 `NEXUS_MOCK_PAYMENT_URL`，发信和 Turnstile 已配置。
- [ ] `policy.groups` 中每组的模式（`initial` 或 `managed`）已确定，`billing.policy.currency` 已按业务设定，策略导入 Job 成功。

**业务**

- [ ] **真实厂商验收**：每个要上线的上游都用 `tests/integration/test_live_*.py`（Bedrock、Responses WebSocket、Claude 压缩等）在其真实账户上通过，核对隔离账本扣费和厂商账单。
- [ ] **支付 live 验证**：每个 live 商户渠道走通付款、通知、查单和退款对账，见[支付接入](payments.md)；通知地址可从公网经入口访问。
- [ ] **发布第一个集群**：节点能力覆盖快照要求，所有节点的 `applied_revision` 都追上最新修订，并经入口完成一次 HTTP、SSE、WebSocket 调用。
- [ ] **打开需要的 `provider_features`**：逐个部署确认要开启的供应商托管功能（已存储 response、文件、向量库等）和对应的节点能力，知晓[保留与删除策略](relay-design.md#上游供应商功能开关)。
- [ ] **自动发布**：确认 `auto_publish` 开关符合预期；自动发布状态为 ok，没有 `last_failure`。
- [ ] 部署账号 `deployer` 只用于节点登记，密码已轮换。

## 本机冒烟

[`deploy/smoke/`](../deploy/smoke) 在一台 Lima 虚拟机上从零部署整套系统。虚拟机内安装 k3s，k3s 参数与生产相同。

```bash
limactl create --name nexus-k3s deploy/smoke/lima-k3s.yaml && limactl start nexus-k3s
```

```bash
limactl shell nexus-k3s -- bash -lc 'cd ~/path/to/Nexus && for c in control-plane relay web; do nerdctl build -f deploy/images/$c.Dockerfile -t nexus/$c:smoke .; done && bash deploy/smoke/setup-vm.sh'
```

```bash
VMHOME=$(limactl shell nexus-k3s sh -c 'echo $HOME'); KUBECTL="limactl shell nexus-k3s env KUBECONFIG=$VMHOME/.kube/config kubectl" HELM="limactl shell nexus-k3s env KUBECONFIG=$VMHOME/.kube/config helm" uv run --no-project python deploy/smoke/smoke.py --ingress 192.168.64.3 --drills
```

- `setup-vm.sh` 负责准备集群：
  - 在集群外起一个 R2 替身（versitygw，S3 兼容网关）。它和 R2 一样使用 region `auto`，并建好 `nexus-backups` 和 `nexus-app` 两个 bucket；chart 和 k3s 的配置项与 R2 一一对应，只有 endpoint 指向替身（`smoke.py` 安装应用时设置 `objectStorage.endpointURL`，并用同一对密钥创建 `nexus-app-r2`）；
  - 安装 k3s（`cluster-init`、`secrets-encryption`、快照上传到替身的 `nexus-backups/etcd/smoke/`）、Helm、cert-manager、CloudNativePG 和 Barman Cloud 插件；
  - 应用 Traefik 设置；
  - 建一个私有 CA 的 ClusterIssuer 代替 ACME；
  - 把镜像导入 k3s。
- `smoke.py` 只用标准库，用随机值创建 Secret 代替 External Secrets，部署一个使用私有 CA 的 TLS 上游替身（[`upstream_stub.py`](../deploy/smoke/upstream_stub.py)，提供 Responses 的 JSON、SSE 和 WebSocket），然后执行：
  1. 安装两个 chart，检查迁移和策略导入钩子；
  2. 创建管理员、企业（入账 100 USD）、上游、部署、价格、路由和 Key；
  3. 建集群，扩到两个 Relay 节点，发布；
  4. 经 Traefik 发起 HTTP、SSE、WebSocket 调用（另有 65 秒的长 SSE 流和长 WebSocket 轮次），并核对账本：每次调用对应一个已结算请求，余额 = 入账 − 扣费 = 最后一条流水的余额，占用为 0。

  加上 `--drills` 时还会执行：
  - 演练 A：修改 API 的 ConfigMap，只有 API 滚动重启，并读到新值；
  - 演练 B：持续发送请求和 12 秒的长流，同时修改 Relay 的配置触发滚动更新，然后检查没有请求失败、两个节点都被替换、账本平衡。
- 冒烟用的 `values-app.yaml` 导入两个策略组：`identity.auth_policy`（`initial`）和 `reporting.export_policy`（`managed`），脚本检查导入结果中后者已锁定。
- 从零部署要拉取约 1 GB 镜像并构建 Rust，在笔记本上耗时较长。环境已经部署过时，用 `--reuse` 接着使用上次的企业、模型和集群：脚本重新应用 chart、把 Relay 滚动到当前镜像，然后只做调用、账本核对和演练；Relay 已经是当前镜像时，加 `--skip-roll` 跳过滚动。`--fresh` 会先删除上次的 release 和命名空间。
- 冒烟发现并修复了一个问题：Relay 关闭 keep-alive 后，hyper 在 101 响应上也追加 `close`，Traefik 因此拒绝 WebSocket 升级（返回 500）。现在 Relay 只在非 101 响应上加 `Connection: close`。

## 已知限制与后续

- **升级时的就绪窗口**：API 的就绪检查要求 schema 修订完全一致，schema 变化时会有不到一分钟的 API 不可用（见[升级](#升级)）。如果就绪检查能接受“当前修订或已知的下一个兼容修订”，这个窗口就可以消除。
- **Redis 证书续期需要重启**：Redis 不会重新加载 TLS 文件，续期后要安排重启（会触发 60 秒限流恢复窗口）。
