# 支付网关与自动入账

Nexus 为预付费账户提供两种资金入口：财务人工登记，以及支付网关确认成功后的自动入账。两条路径共用资金账本、币种规则、幂等约束和对账机制。

已接入 Stripe Checkout、彩虹易支付 V1/V2 和 Antom 托管收银台。开发环境也可启用本机 mock。渠道参数通过管理 API 配置，API 和 Worker 共用加密后的凭据版本。

## 运行与接口

先执行 `uv run nexus --env-file .env.migrate migrate`，升级到当前迁移版本，再分别启动 API 与 Worker。降级涉及支付订单、跨币种报价或核对决定时，先检查目标迁移的历史保留条件。

API 与 Worker 使用相同的以下配置：

| 配置 | 用途 |
| --- | --- |
| `NEXUS_PAYMENT_PUBLIC_BASE_URL` | 固定通知站点，例如 `https://control.example.com`；生成渠道通知地址，Antom 另带订单路径 |
| `NEXUS_PAYMENT_RETURN_URL` | 固定前端返回地址，只用于订单展示和查询；客户端不能覆盖 |
| `NEXUS_MOCK_PAYMENT_URL` | 仅 development/test 可用的模拟器地址，只接受 `http://127.0.0.1:{port}` |

限额保存在账务策略（`billing.settings`）中，平台管理员通过 `GET/PUT /api/v1/settings/billing` 的 `payments` 分组或管理后台“平台 → 设置 → 计费”修改，所有进程约 2 秒内生效：

| 字段 | 用途 |
| --- | --- |
| `notifications_per_channel_per_minute` | 每个渠道每分钟通知上限，默认 1000，范围 1–100000 |
| `notifications_per_ip_per_minute` | 每个来源每分钟通知上限，默认 120，范围 1–10000 |
| `requests_per_merchant_per_minute` | 每商户环境固定 60 秒窗口的远端请求上限，默认 120，范围 1–10000；Worker 在每个处理事务中读取 |

生产进程要求通知与返回地址为 HTTPS，拒绝 mock 传输和 test 渠道。支付配置沿用已有根密钥配置，但使用独立于上游模型凭据的密钥派生和记录绑定；仍在查询或验签窗口内使用的旧根密钥必须保留。

以下路径相对于 `/api/v1`，管理调用使用 Bearer 会话。列表提供 limit（1–100，默认 50）和 cursor，注册类型列表由已安装适配器决定。

| 接口 | 权限和行为 |
| --- | --- |
| `GET /payment-gateway-types` | 平台管理员查看已安装类型与配置 schema；未注册类型不能启用 |
| `GET/POST /payment-channels` | 平台管理员管理渠道；创建要求 Idempotency-Key |
| `GET/PATCH /payment-channels/{id}` | PATCH 要求 If-Match 与 Idempotency-Key，可改名、限额、状态或替换完整配置 |
| `GET /payment-channels/{id}/versions` | 查询配置版本、能力、根密钥版本和验签有效期，不返回密钥或密文 |
| `POST /payment-channels/{id}/versions/{version_id}/revoke` | 平台管理员撤销材料，要求 If-Match；撤销当前配置前必须先停用渠道 |
| `GET /organizations/{org}/payment-methods` | 组织 owner、org_finance 或平台财务查看可用渠道、币种、精度与限额 |
| `GET/POST /organizations/{org}/payment-orders` | 创建要求上述充值权限、active 组织及 Idempotency-Key；返回 202 与订单，不等待远端下单 |
| `GET /organizations/{org}/payment-orders/{id}` | 组织账务权限读取订单；只读数据库，不触发远端查单 |
| `POST /organizations/{org}/payment-orders/{id}/reconcile` | 平台财务请求后台查单，返回 202；不接受客户端指定成功状态或入账金额 |
| `GET /organizations/{org}/payment-orders/{id}/observations` | 平台财务读取最小查单证据，排除支付入口参数 |
| `GET /organizations/{org}/payment-orders/{id}/receipts` | 平台财务读取真实收款、交易标识及证据关联，包括额外收款 |
| `GET /organizations/{org}/payment-cases` | 平台财务读取支付异常、风险金额和证据关联 |

企业开发者没有充值和企业钱包权限。平台管理员维护渠道，不隐式获得人工补单权限；管理员以企业 owner 身份操作自己的企业时，仍按该组织角色判断。平台财务权限沿用[财务授权流程](billing-api.md)。

渠道 PATCH 中的 configuration 是完整替换，必须重新提供该类型需要的凭据。省略 configuration 时保留原材料。轮换默认保留旧验签材料 3600 秒，可通过 verification_grace_seconds 指定 0–86400 秒；最多同时存在八个有效验签版本。普通改名和密钥轮换都不能改变渠道商户、环境或收款命名空间。

订单创建只接收 channel_id、amount 和 currency，金额必须是十进制字符串。渠道限额与金额精度都要通过；例如 mock 使用 USD 两位小数，`1.001` 被拒绝，不会被改成 `1.00`。组织消费折扣不改变充值额度。

重试创建、渠道变更等命令时保留原 Idempotency-Key、请求体和 If-Match。重复调用返回同一资源；同键不同参数返回 409。缺少前提头返回 428，版本冲突返回 412。支付入口仅在未入账、未进入核对且期限有效时返回。

## 托管收银台配置

为 API 和 Worker 配置同一组数据库、加密密钥、`NEXUS_PAYMENT_PUBLIC_BASE_URL` 与 `NEXUS_PAYMENT_RETURN_URL`。平台管理员调用 `GET /api/v1/payment-gateway-types` 可取得配置 schema，再调用 `POST /api/v1/payment-channels` 创建渠道。渠道外层填写 name、adapter_type、stable_merchant_id、environment、currency、min_amount、max_amount 与 configuration。三个适配器当前的充值币种均为 USD，金额字段传十进制字符串。

| adapter_type | stable_merchant_id | configuration 必填 | 可选 |
| --- | --- | --- | --- |
| stripe | Stripe 账户 ID，与 API 密钥对应的 acct_ 账户一致 | account_id、secret_key、webhook_secret | 无 |
| epay，version=v1 | 彩虹易支付商户 PID | version、base_url、pid、environment、payment_type、cny_per_usd、merchant_key | 无 |
| epay，version=v2 | 彩虹易支付商户 PID | version、base_url、pid、environment、payment_type、cny_per_usd、private_key、platform_public_key | 无 |
| antom | Antom Dashboard 的 Client ID | client_id、region、environment、private_key、platform_public_key | key_version 默认 1，checkout_hosts 默认仅含 checkout.antom.com |

**Stripe** 使用单次银行卡 Checkout Session。API 密钥需能读取账户、创建和查询 Checkout Session、读取关联的 PaymentIntent、Charge 和争议。到 Stripe Dashboard 为当前模式建立快照事件端点，填写 `/webhooks/payments/{channel_id}` 的完整 HTTPS URL，订阅 checkout.session.completed、checkout.session.expired、charge.refunded 与 charge.dispute.* 相关事件，并将此端点的签名密钥写入 webhook_secret。Nexus 用原始消息体验签，依据服务端查询中的订单元数据、币种、已捕获的 Charge 和实收金额过账。收据以 Charge ID 去重。创建结果丢失时，Worker 在原订单窗口内查找 Session，并受原始幂等键和期限约束。当前使用标准 checkout.stripe.com 域名。[Checkout Session](https://docs.stripe.com/api/checkout/sessions/create) · [Webhook 验签](https://docs.stripe.com/webhooks/signature)

**EPay** 使用签名页面跳转。base_url 填部署方提供的 HTTPS 网关根地址，payment_type 填该商户开通的方式，例如 alipay 或 wxpay。V1 使用 submit.php、MD5 和经过 HTTPS 认证的 api.php 订单查询；V2 使用 api/pay/submit、RSA-SHA256 和签名的 api/pay/query。RSA 密钥支持 PEM 或 Base64 DER，至少 2048 位。支付回调允许 GET 或表单 POST，成功确认返回纯文本 success。网关必须能查回 PID、支付方式、外部交易号、商户订单号、原始金额和状态。收款命名空间包含网关地址、PID 与环境。不同网关实例可以使用相同的 PID。[V1 协议](https://www.jiangcen.cn/doc/v1_legacy_api.html) · [V2 查单字段](https://pay.lanfucai.com/doc/pay_query.html)

`cny_per_usd` 表示每 1 USD 对应的 CNY，使用十进制字符串。创建订单时按渠道配置计算 CNY 实付额并四舍五入到分，同时锁定钱包 USD 入账额。在返回值中，amount、credit_amount、currency 使用钱包口径，collection_amount、collection_currency 使用支付口径，exchange_rate 为订单汇率。例如充值 10.00 USD、汇率 7.25 时须实收 72.50 CNY，随后把渠道汇率改为 8.00 只影响新单。收据保留人民币原值；部分退款按原始实付额占比折算为美元核对金额。已入账订单的退款通过引用原流水的调整处理。

**Antom** 使用 AMS 的 Hosted Checkout Page。region 可取 na、us、asia、eu，按商户在 Dashboard 获配的端点选择。test 和 live 分别使用沙箱和生产路径。使用商户私钥对请求签名，并以 Antom 公钥验证原始响应和通知。checkout_hosts 可填写商户沙箱或生产收银台实际返回的主机名。Nexus 在下单时发送带订单 ID 的 paymentNotifyUrl，代理转发应保留完整路径，以便没有 paymentRequestId 的请款通知也能关联订单。查询以支付请求号或 paymentId 调用 inquiryPayment。银行卡须有全额成功 CAPTURE；已映射的单阶段钱包可按服务端最终支付结果入账。当前单阶段映射包括 ALIPAY_CN、ALIPAY_HK、GCASH、DANA、TNG 和 TRUEMONEY。退款、争议与无法绑定的通知均需核对，平台级退款通知若只有退款 ID 而无原支付引用，不能用于自动归属。[Hosted Checkout](https://docs.antom.com/ac/cashierpay/HOSTEDCKP) · [签名规则](https://docs.antom.com/ac/ams/digital_signature) · [查单与请款](https://docs.antom.com/ac/cashierpay/capture)

本地协议测试使用真实 PostgreSQL、Redis、订单处理器与替代厂商网络的 HTTP 测试传输，覆盖渠道配置、签名、查单、重复通知、汇率轮换及资金流水。实际商户接入时需用对应凭据验证沙箱下单、托管页付款、通知投递和退款对账。

## 订单状态

支付结果、支付入口状态和入账状态分别保存，避免一个 `paid` 字段混淆外部收款与内部记账。

| 字段 | 状态 | 说明 |
| --- | --- | --- |
| `checkout_state` | creating、ready、closed、unknown | 控制支付入口展示；不代表资金结果 |
| `payment_state` | pending、processing、succeeded、failed、cancelled、partially_reversed、reversed、unknown | 已验证的外部支付状态，保留每次观察事实 |
| `credit_state` | uncredited、credited | 原始充值是否已入账；调整不能把它改回 uncredited 再充值 |
| `review_required` | true / false | 金额冲突、未知结果、退款或其他异常需核对 |

通知到达顺序不能决定资金结果，旧 pending 或 failed 通知不能覆盖已确认成功。原先本地超时、过期或查询失败，随后得到可信成功事实时，可以入账；若发现真实撤销或退款，则走调整与核对，不将其当成重复成功通知丢弃。

## 本机模拟支付

模拟器只验证协议与控制平面流程，不包含真实可付款的收银页面。其外部支付状态保存在模拟器进程内；Nexus 的订单、任务、证据和账本仍持久化在 PostgreSQL。

1. 设置仅用于本机测试的 `NEXUS_MOCK_PROVIDER_API_KEY`（至少 32 个字符），运行：

   ```sh
   uv run python scripts/mock_payment_provider.py --port 59090 --merchant mock-merchant
   ```

2. 为 API 和 Worker 设置 `NEXUS_MOCK_PAYMENT_URL=http://127.0.0.1:59090` 及固定通知、返回地址，然后重启进程。
3. 管理员创建 adapter_type 为 mock、stable_merchant_id 为 mock-merchant、environment 为 test 的渠道，明确 USD 限额。configuration 中 api_key 与模拟器环境变量一致，另提供至少 32 字符的 webhook_secret；notification_mode 可选 raw_json（默认）或 form。
4. 企业 owner 或财务创建订单，Worker 生成受控的模拟支付入口。使用模拟器的 `POST /control/paid`，携带模拟 api_key 的 Bearer 认证和 `{"merchant_order_ref":"订单返回的引用"}`，模拟实际收款。
5. 模拟器不主动发送通知；Worker 的查单补偿可以取得收款结果，也可由平台财务调用订单 reconcile 立即排队。之后查看订单的 credit_state、流水关联及账户余额。

mock 通知使用 `X-Mock-Signature: t={Unix秒},v1={小写十六进制HMAC}`。签名输入为时间戳的十进制字节、一个 `.` 和原始 body；form 的 GET 通知使用原始 query 字节。算法为 HMAC-SHA256，默认容忍 300 秒时差，配置范围为 60–900 秒。重复签名头、重复 JSON 字段或重复表单/query 参数都会拒绝；不会先把多值参数折叠为字典再验签。

raw_json 只接受 POST；form 接受 GET/POST。POST 不允许附带未签入 body 的 query。正常接收与已验证的无关通知返回 200（raw_json 为 ok，form 为 success）；非法通知返回 400，暂时失败返回 503，限流返回 429。模拟协议报文上限为 64 KiB，HTTP 入口另有 1 MiB 硬上限。通知入口不沿用管理 API 的业务 JSON 响应格式。

已发布的 mock 只提供 redirect，不支持远端关单；接口明确返回不支持，不模拟关单成功。通用 CheckoutResult 也支持 qr_code，适配器必须显式声明该能力及受控内容前缀。真实渠道的协议、应答格式、金额精度和关闭能力见各渠道配置。

## 任务与运行维护

创建请求只保存不可变期望和任务。Worker 以短事务认领任务，默认租约 30 秒；厂商和 mock 的 HTTP 传输均限制调用时间与响应体大小。网络期间不持有数据库连接或账户锁，多 Worker 通过 SKIP LOCKED、递增 fencing_token 和订单创建代次协作。

首次创建意图保存原始幂等标识、幂等方式及到期时间。接管者看到已开始的操作会先查询原订单；“未找到”结果同样必须匹配真实商户和环境，不能用本地期望代替缺失的上游身份。只有原保证和当前能力均允许、渠道仍启用且订单仍有效时，才重发同一请求。修改配置或后续 SDK 能力不会延长旧调用的保证。过期、缺乏保证或持续未知的结果进入核对，不换订单号重新创建。

商户出站请求限额由 PostgreSQL 中的窗口计数协调，多个渠道、密钥版本和 Worker 共用稳定收款命名空间，不按进程各自放大。限额不足只延期任务，不增加远端失败次数，也不丢弃创建意图。查单和安全重试需要分属不同窗口时，已验证查询证据会持久化；重试前仍复核订单版本、创建代次及原始幂等期限。

正常未付款订单每 30 秒查询，已入账或过期订单每 300 秒继续检查资金变化。临时故障采用退避，单轮最多八次尝试，随后保留核对事项；新的有效通知或财务补单可以再次唤醒查询。通知中不同外部支付对象的提示拥有独立任务，不覆盖其他收款线索。

通知先验签，再在同一数据库事务中保存最小事实并唤醒查单。任务完成后更新受控处理状态；原始内容、摘要和验证版本不变，已处理通知不会因后续查询故障退回待处理。查询结果与所观察的订单版本、创建代次和任务租约核对，迟到结果只保留证据、唤醒复查，不覆盖较新的支付或入账结果。

人工登记与网关确认共用同一个过账函数。成功事务同时提交订单、实际收款关联、funding_records、唯一资金流水、余额、审计、任务状态及 billing.funding_posted 事件。网关过账使用 payment-worker 系统身份；财务补单只是请求服务端核实，不伪造某位人员确认收款。

退款、争议、重复实际收款及绑定冲突会保存证据和核对事项。已入账订单的已验证反向资金通知会立即限制账户；查询补偿也会检查没有通知的后续变化。系统不自动发起对外退款、不自动扣成负余额，也不自动解除已有冻结。平台财务可以引用原入账流水执行受余额与预留约束的调整，事项关闭与解除限制命令见[查询与运营](operations.md)。

## 通用适配器契约

新增支付渠道时，在 billing 的装配入口注册配置模型和适配器工厂，协议实现放入 `billing/providers/`。管理 API 通过 Registry 暴露厂商配置 schema，Worker 通过同一注册表打开相应版本的网关。[注册入口](../src/nexus/modules/billing/__init__.py) · [协议与能力声明](../src/nexus/modules/billing/application/payments/registry.py)

通用接口表达业务含义，不假设不同支付服务具有相同 URL、签名算法、回调格式或状态码。适配器不得直接修改组织余额。

| 操作 | 输入 | 输出与约束 |
| --- | --- | --- |
| `capabilities` | 渠道配置 | 币种与金额精度、查单与关单能力、幂等方式与期限、托管入口类型及允许的目的地 |
| `create_payment` | 不变的商户订单号、金额、币种、服务端回调地址、固定返回地址、幂等标识 | 可选的外部支付引用、托管地址或支付码、入口期限与支付状态 |
| `query_payment` | 商户订单号或已确认的外部支付对象引用 | 经服务端认证获得的支付状态、交易号、金额、币种、商户和环境 |
| `parse_notification` | 原始方法、路径、查询字节、多值表单、请求头、请求体、渠道验签材料 | 在同一入口完成认证和归一化，只返回 VerifiedNotice；不暴露可绕过验签的业务解析入口 |
| `check_binding` | 不可变 PaymentExpectation、VerifiedNotice 或查单事实 | 补充厂商特有的订单元数据核对；共同流程始终校验组织、订单、金额、币种与商户 |
| `build_notification_response` | 已持久化接收、有效但忽略、拒绝或暂时失败 | 符合该渠道协议的 HTTP 状态、正文和 Content-Type |
| `close_payment` | 商户订单引用 | 可选能力；不支持时不模拟已关单 |

核心契约为 PaymentExpectation、CheckoutResult、NotificationEnvelope、VerifiedNotice、PaymentObservation 和 NotificationDisposition。它们是 billing 自有的不可变值对象，不接收整个 ORM Order 或 FastAPI Request。PaymentExpectation 固定订单、账户、渠道商户、环境、金额和币种；CheckoutResult 首期只允许受控的 redirect 或 qr_code 类型，并声明有效期。

NotificationStyle 声明允许的 HTTP 方法、原始 body 或表单模式、是否要求参数唯一、报文上限以及应答风格。表单或 query 需保留多值信息直到检查完成，不能先转换成单值字典再丢掉重复签名字段。handler 按样式收集原始请求，再调用 billing 门面，不直接选择 SDK。

内部错误区分 `invalid_notification`、`pending`、`unconfirmed`、`retryable_failure` 和 `manual_review`。未确认表示缺少足够证据，不能当作确定未支付；暂时失败可重试，非法通知不触发入账。对外状态和正文遵守渠道协议，禁止直接回显包含上游密钥或内部堆栈的异常文本。

自动入账渠道必须支持可信的服务端查单，并能匹配原商户订单、实际收款商户、交易金额和币种。安全下单重试需要网关原生幂等，或唯一商户订单号与可靠查询；若能力不足，结果未知时进入核对，不盲目重新创建支付。

网关下单幂等的保留期由适配器明确，不假定永久有效。例如 Stripe 的接口幂等有其自己的重放与清理规则，不能代替 Nexus 的长期业务唯一约束。[Stripe 幂等请求说明](https://docs.stripe.com/api/idempotent_requests)
