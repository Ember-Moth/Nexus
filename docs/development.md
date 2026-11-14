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

`live` 检查 API 进程是否存活，`ready` 校验数据库角色、迁移版本、模块权限、账务币种与 Redis。API 就绪失败时查看响应中的检查项。每个 Worker 使用独立的 `NEXUS_WORKER_ID`，通过对应配置运行 `worker-health` 检查其心跳和消费者状态。先停止 API 与 Worker，再运行 `uv run python scripts/dev_services.py stop`，本地数据会保留。

## 生产配置

- API、Worker 和迁移进程分别注入各自的凭据。运行时设置 `NEXUS_ENVIRONMENT=production`，PostgreSQL 连接使用 `sslmode=verify-full`，Redis 使用带认证的 `rediss://`。给每个网关节点登记独立的数据库身份。[网关权限与接入](plane-protocol.md)
- API 与 Worker 使用相同的账务币种、Redis 命名空间和应用加密密钥版本。钱包默认使用 USD；先将新旧密钥分发到相关进程，再轮换当前版本，并保留历史密文所需的旧密钥。[凭据轮换](catalog-api.md#凭据加密与轮换)
- 启用充值前配置 HTTPS 通知站点、返回地址和 live 商户渠道。先在相应商户环境验证付款、通知、查单及退款对账，再开放收款。启用收费模型前确认实际售价、成本、折扣、用量维度和可执行的输入输出上界。[支付接入](payments.md) · [定价与预算](billing-api.md)
- 按部署容量确定积压阈值、连接预算、保留期、可接受的数据丢失量（RPO）和恢复时间（RTO）。还原时需同时具备数据库备份、应用密钥和网关集群密钥。按实际环境验证跨机恢复、流量峰值和故障切换。[验收与恢复演练](control-plane-acceptance.md)

## 开发检查

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest -q -m 'not integration'
uv run python scripts/run_integration.py -q
uv build
```

集成脚本使用临时 PostgreSQL、Redis 和随机端口，结束后清理；容量用例显式传入 `--capacity`。维护模块目录和依赖时参考[模块组织](architecture.md#python-工程组织)。
