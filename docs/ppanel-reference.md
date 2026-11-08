# PPanel 设计参考

Nexus 参考 PPanel 的模块化单体思想和支付网关职责划分。参考对象是模块边界、公开接口、数据所有权和事务原则，不是 PPanel 的框架目录或完整分层模板。Python、FastAPI、uvloop、PostgreSQL 技术栈与企业 Token 计费范围保持已定稿的选择。

本次参考本地 `/Users/tschen/ppanel/backend`，读取时为干净的 master，提交 `764e937c658a62c0b562a11c7c0619261b01c882`，日期为 2026-11-08。以下结论基于设计说明和对应源码；没有运行 PPanel 服务或真实支付测试，也没有修改该仓库。

## 模块设计依据

| PPanel 来源 | 当前结构或规则 | Nexus 采用方式 |
| --- | --- | --- |
| [模块化单体 ADR](/Users/tschen/ppanel/backend/docs/design/adr-001-modular-monolith.md) | 模块门面、模块自有契约、唯一数据属主、作用域事务 | 用数据所有权和事务划分五个模块，支付与钱包同属 billing |
| [包结构说明](/Users/tschen/ppanel/backend/docs/design/package-layout.md) | 组装根、业务模块、接入层、基础设施分别承担职责 | bootstrap.py 组装依赖，业务包按需提供 API，进程入口不承担账务流程 |
| [billing 依赖与构造](/Users/tschen/ppanel/backend/internal/module/billing/billing.go:310) | Deps 注入所需能力，内部构造 checkout 等服务 | 按真实用例传入依赖，需要可替换边界时才声明 Protocol |
| [应用组装](/Users/tschen/ppanel/backend/internal/app/billing.go:24) | app 向模块注入门面适配器、仓储和队列能力 | 依赖组装集中在 bootstrap.py，不向业务代码传整个应用对象 |
| [billing 作用域事务](/Users/tschen/ppanel/backend/internal/module/billing/billing.go:731) | Store 只暴露 billing 事务、钱包与 inbox 能力 | BillingUnitOfWork 只暴露本域仓储，支付和余额在同域原子提交 |
| [导入边界检查](/Users/tschen/ppanel/backend/internal/arch/arch_test.go:179) | 限制模块依赖 app、完整 Store、其他模块 contract 等 | Python 通过 AST/CI 检查边界，不能仅依赖目录命名 |
| [仓储所有权检查](/Users/tschen/ppanel/backend/internal/arch/ownership_test.go:48) | 不能调用其他模块仓储或开启对方事务 | 维护表与模块属主清单，HTTP/任务只进入公开门面 |

PPanel 文档包含历史迁移说明。本次采用现行边界规则和实际源码中的结构，不将历史迁移步骤或旧 ServiceContext 当作 Nexus 的实现模板。

## 支付设计依据

| PPanel 来源 | 当前行为 | Nexus 采用方式 |
| --- | --- | --- |
| [Gateway 接口](/Users/tschen/ppanel/backend/internal/module/billing/internal/gateway/gateway.go:159) | StartPayment、Reconcile、ParseCallback、CheckOrder、ConfirmPayment 统一业务调用方式 | 一个通用支付流程，协议差异封装在绑定渠道配置的网关对象内 |
| [Registry](/Users/tschen/ppanel/backend/internal/module/billing/internal/gateway/registry.go:53) | 按平台集中注册工厂、配置规范化和 CallbackStyle | 注册表拥有类型化配置验证、能力与通知样式，业务流程不散布厂商分支 |
| [回调流程](/Users/tschen/ppanel/backend/internal/module/billing/internal/callbacks/service.go:94) | 认证通知、绑定订单、核对期望金额币种、远端确认，再结算 | 共享不可变 PaymentExpectation 和确认流程，保留 Nexus 的可靠通知收件箱 |
| [通知 HTTP handler](/Users/tschen/ppanel/backend/internal/module/billing/transport/http/notify/payment_notify_handler.go:40) | 按回调样式处理原始 body、表单重复参数和协议应答 | FastAPI 薄 handler 保留原始证据，错误类型与渠道响应规则明确 |
| [支付会话存储](/Users/tschen/ppanel/backend/internal/module/billing/internal/repo/payment_session.go:13) | 远程创建前持久化开始标记，用条件更新绑定交易和可复用入口 | 下单任务先提交创建意图与代次，未知结果先查原订单，成功入口可重用 |
| [共同结算原语](/Users/tschen/ppanel/backend/internal/module/billing/internal/settle/settle.go:46) | 回调和到期对账复用 VerifiedPayment，校验交易号与状态转换 | 回调、主动查单、人工补单共用 billing 的确认与原子入账服务 |
| [付款状态更新](/Users/tschen/ppanel/backend/internal/module/billing/internal/repo/order.go:406) | 条件更新状态和交易号，同时增加状态版本与事件 | 用 CAS、唯一约束和账务事务处理竞争，不覆盖另一笔交易 |
| [订单事件 outbox](/Users/tschen/ppanel/backend/internal/module/billing/internal/orderevents/service.go:1) | 事件随状态提交，提交后发布 Redis 通知，失败可重试 | 资金事实先在 PostgreSQL 提交，通知及报表投影不阻塞资金正确性 |

## Nexus 的具体取舍

- 顶层模块为 identity、catalog、billing、publication、reporting。支付、定价、计量、金额预算和钱包是 billing 的内部子域，保持资金事务完整。
- GatewayRegistry 只集中选择协议和验证配置；支付适配器不能修改钱包，不能决定未经账务校验的充值金额。
- PPanel 的回调服务在通知流程中完成远端确认和结算。Nexus 采用验签后可靠入箱并应答，再由 Worker 查单和入账，降低回调响应对外部网络的依赖。
- PPanel 的 VerifiedPayment 标记已付后驱动后续激活，源码也处理入队失败后的恢复。Nexus 的单纯钱包充值把入账状态、余额和流水放在同一 billing 事务内，提交后事件用于通知与报表，不复制套餐激活流程。
- PPanel 当前未匹配收款日志有 best-effort 路径。Nexus 已接收的支付证据与核对事项按可靠收件箱处理，不用普通进程日志代替资金来源记录。
- 保留 Nexus 的高精度 Decimal/NUMERIC Token 账务。支付协议需要的最小货币单位仅在适配器边界精确转换，不将整个钱包降为整数分。
- 采用 Python 包的公开导出与 CI 依赖检查，不复制 Go internal 目录、统一 Facade/Deps 模板或多层空转发；Go 框架、GORM 和 asynq 不作为 Nexus 依赖。
- PostgreSQL 两平面直连接口是 Nexus 的额外边界，使用模块拥有的公开 SQL 只读契约组合授权，只写单一业务模块，不照搬任意全局 Store。
- 具体支付渠道仍未选择。PPanel 已有的供应商、优惠券、赠金、佣金、提现、套餐库存与访客门店功能不自动进入 Nexus 范围。

2026-11-08 根据项目要求澄清：M1 将进程入口整理为 api.py、worker.py、bootstrap.py 等直接的 Python 模块，去除仅为存储探测设置的 Facade/Service/Repository 转发链；业务和数据库边界继续保留。

正式实现规则见[系统架构](architecture.md)、[数据模型](data-model.md)和[支付网关与自动入账](payments.md)。这些本地来源链接用于追溯参考，不成为 Nexus 的运行或构建依赖。
