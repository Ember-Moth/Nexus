# 本机开发与 M1 验证

M1 已实现 Python 工程、五个业务边界与依赖组装、存储作用域、迁移、API/Worker 生命周期和初始管理员引导。本机开发直接使用原生 PostgreSQL 和 Redis，不依赖 Docker，也不配置系统服务管理器。

identity 已提供身份、项目凭据和模型策略，catalog 提供模型与上游管理。跨模块依赖由 bootstrap.py 连接公开能力，不共享仓储。进程入口分别位于 api.py 和 worker.py。改动业务用例、HTTP 路由和数据库操作时，按[模块内分层](architecture.md#python-工程组织)定位对应文件。

## 环境要求

- Python 3.14.7 与 uv，Python 版本记录在 `.python-version`，依赖由 `uv.lock` 锁定。
- PostgreSQL 16 或更高版本，需要 `initdb`、`pg_ctl` 和服务端二进制；工具不在 PATH 时，脚本会尝试通过 `pg_config --bindir` 查找。
- Redis 服务端及对应的本机执行权限。
- Linux 或 macOS；API 和 Worker 显式使用 uvloop。

本次本机验证环境为 Python 3.14.7、PostgreSQL 17.11、Redis 8.10.3。脚本使用已有二进制，不安装数据库软件。

## 初始化与启动

在仓库根目录执行：

```sh
uv sync --locked
uv run python scripts/dev_services.py start
uv run nexus --env-file .env.migrate migrate
uv run nexus --env-file .env.api check
```

`start` 在仓库的 `.local/native` 中创建专用数据库实例，选择可用的本地端口，只监听 `127.0.0.1`。已有实例再次执行 start 会复用本项目数据，不接管本机其他 PostgreSQL 或 Redis 服务。

首次启动会生成随机凭据和 32 字节加密根密钥，并创建以下文件。私密文件权限为 0600，均已被 Git 忽略：

| 文件 | 用途 |
| --- | --- |
| `.env.api` | API 与初始管理员引导使用的运行角色 |
| `.env.worker` | Worker 使用的独立运行角色 |
| `.env.migrate` | 迁移角色，仅用于迁移命令 |
| `.local/native/services.json` | 本机实例管理信息和本地管理凭据 |

脚本不覆盖已有的进程配置文件。数据库目录与这些配置文件需要保持配套；调整现有配置后，可使用 check 命令检查连接和迁移状态。

分别在两个终端运行：

```sh
uv run nexus --env-file .env.api api
```

```sh
uv run nexus --env-file .env.worker worker
```

API 默认监听 `127.0.0.1:8000`，可通过进程环境中的 `NEXUS_HOST`、`NEXUS_PORT` 修改。`.env` 文件必须由 `--env-file` 显式指定；进程环境变量优先于该文件。

`GET /health/live` 检查进程存活；`GET /health/ready` 检查 PostgreSQL 登录身份、迁移版本、Redis 和五个模块的存储权限，依赖异常时返回 503，响应不包含连接凭据或数据库错误详情。

Worker 执行依赖监督、带 TTL 的心跳、可靠开户、到期预留核对和支付任务。支付任务带租约与 fencing_token，多进程可接管；远端 HTTP 调用不持有数据库事务。M4 用量结算与配置分发由独立消费循环执行。独立消费循环的节点心跳分别报告 settlement 和 publication_outbox。每个并发 Worker 需使用不同的 `NEXUS_WORKER_ID`。

```sh
uv run nexus --env-file .env.worker worker-health
```

API 与 Worker 接收 SIGINT/SIGTERM 后关闭连接资源；Worker 只删除属于当前进程实例的心跳，避免旧进程清除替代进程的状态。

## 初始管理员

完成迁移后执行：

```sh
uv run nexus --env-file .env.api bootstrap-admin --login admin
```

命令交互读取并确认至少 16 字符的密码，使用 Argon2id 保存哈希，不输出密码或哈希。自动化可以使用 `--password-stdin` 从安全输入流传入，不提供命令行明文密码参数。

引导只创建第一个 `platform_admin`，不会隐式授予财务角色。相同登录名重复执行返回原用户且不重置密码；已有其他管理员时拒绝再次引导。并发引导通过数据库锁、唯一约束和 identity 内事务保证仅创建一次，并记录一条审计。

M2 的接口流程见[登录与企业成员管理](identity-api.md)、[项目与应用凭据](project-credentials.md)和[模型与上游目录](catalog-api.md)。初始管理员仍通过 CLI 引导，不开放公开注册入口。

## 存储权限与事务

| 身份 | 权限 |
| --- | --- |
| `nexus_local_admin` | 仅本机脚本管理其专用实例，不提供给运行进程 |
| `nexus_migrator` | 创建模块 schema、表和权限，独立于运行角色 |
| `nexus_api`、`nexus_worker` | 非超级用户、无建库建角色权限、NOINHERIT；可切换到显式授予的模块角色 |
| `nexus_identity` 等五个 NOLOGIN 角色 | 仅使用所属 schema，由迁移逐表授予权限 |

模块的私有持久化层通过固定作用域事务执行 `SET LOCAL ROLE`，每次并发操作拥有独立 Session。事务结束即提交或回滚并恢复连接身份；模块对外门面不暴露 Session 或全局 Store。

运行登录身份可以读取 `nexus_meta.alembic_version`，不能修改它或执行迁移。`identity.audit_events` 只允许相应模块读取和追加。新函数默认不向 PUBLIC 授予 EXECUTE，gateway_v1 只向专用网关组角色授权。

M1 迁移创建五个模块 schema、迁移元数据、预留的 gateway_v1 schema 和管理员引导存储。M2 的 0002 增加身份管理，0003 增加项目凭据，0004 增加目录、加密凭据、部署映射与模型策略。M3 的 0005 增加账户与账本，0006 增加定价、预算、预留和尝试记录；M3P 的 0007 增加支付配置、订单、任务、通知、查单与收款证据，复用原资金账本；0008 增加配置发布、受限 SQL 接口和 Token 用量收件箱。网关角色、运行参数和模拟器见[通信协议](plane-protocol.md)。

Worker 已消费组织开户事件并标记到期预留。API 和 Worker 的 NEXUS_BILLING_CURRENCY 默认为 USD，首次账务事务将其固定在数据库；已有数据库不能通过改环境变量换币种。参见[定价与预付费账户](billing-api.md)。

支付模拟器与固定回调、返回地址的配置见[支付网关与自动入账](payments.md)。模拟器只监听本机，生产进程拒绝 mock 支付配置。存在支付订单时 0007 的降级会拒绝执行，避免丢失在途或迟到收款的跟踪信息；升级保留已有 M3 人工入账。

## 检查与集成验证

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest -m 'not integration' -q
uv run python scripts/run_integration.py -q
uv build
```

集成脚本新建专用临时目录、随机本地端口和 `nexus_test_` 数据库，运行迁移与测试，最后停止进程并删除临时数据。迁移和测试子进程会移除外部 NEXUS_* 环境变量，只使用测试生成的配置，防止已导出的开发或生产凭据覆盖测试连接。只运行普通 pytest 时，集成测试会明确跳过。

部分受限执行环境禁止 PostgreSQL 创建共享内存。这种情况下需要给该集成脚本相应执行权限，而不是改用 SQLite 或跳过数据库权限验证。

CI 位于 `.github/workflows/check.yml`，运行同一组静态、架构、原生数据库集成检查和构建步骤。CI 工作流尚需在远端运行后才有对应的执行结果。

## 停止本机依赖

先退出本项目的 API 和 Worker，再执行：

```sh
uv run python scripts/dev_services.py stop
```

stop 只控制本项目记录的 PostgreSQL 数据目录和经过 PID 核对的 Redis 实例，保留开发数据供下次启动。临时测试实例则由测试脚本自行清理。

## 生产配置边界

生产环境通过受控配置设施分别注入 API、Worker 和迁移所需的凭据，不将本机管理凭据或迁移凭据传给运行进程。运行配置缺少有效的根密钥或连接凭据时拒绝启动；生产 PostgreSQL 要求 `sslmode=verify-full`，Redis 使用带认证的 `rediss` 连接。

本机脚本是开发与测试工具，不承担生产数据库部署、备份或高可用配置。生产运维与真实业务负载的验收仍按[实施计划](implementation-plan.md)推进。
