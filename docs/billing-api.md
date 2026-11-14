# 定价与预付费账户

钱包按企业结算。平台管理员配置售价、部署成本、折扣和预算，平台财务登记收款并过账。在线充值的配置、收款币种和查单流程见[支付网关与自动入账](payments.md)。

## 账户准备

`NEXUS_BILLING_CURRENCY` 默认 `USD`。第一次账务事务（包括账务就绪检查）将币种写入 billing.settings。以后 API 与 Worker 配置必须与持久化币种一致，否则账务操作拒绝、`billing_currency` 就绪检查失败；修改配置不执行换汇。

Worker 每 500 ms 检查一次开户事件和到期预留，有待处理数据时连续消费。组织创建仍先返回 provisioning；billing 幂等开户并发出 account_ready，identity 消费后进入 active。两侧事件均携带显式 schema_version；消费者只接受已支持版本，未知版本保留待处理且不确认、不开户或激活。中途停机可重放，暂停组织只更新恢复目标，归档组织不会被重新激活。到期预留只进入 review，金额仍保持 held。

## 权限与财务操作

所有路径以 `/api/v1` 为前缀，使用管理会话 Bearer 认证。

| 角色 | 账务权限 |
| --- | --- |
| platform_admin | 维护价格、折扣和平台预算；读取账户；管理平台角色 |
| platform_finance | 登记收款、过账、调整、对账；读取平台范围的账户与流水 |
| org_owner | 读取本企业账务，管理本企业预算，不能修改平台预算 |
| org_finance | 读取本企业账务；无直接入账、调整和预算写权限 |
| org_developer | 可在模型权限范围内查询有效售价；无企业钱包和完整账务查询权限 |

平台财务写权限需显式授予。[分配平台角色](identity-api.md#平台角色与企业角色)

财务只维护稳定的线下收款账户。`reference` 是同一真实收款账户的永久标识，限 1–70 个字母、数字或 `_.:-`；不能为同一真实账户创建多个 reference 以绕过收款去重。namespace 固定为 `manual:{reference}`，不能通过改名、停用或修改密钥改变。网关收款使用独立、受校验的 gateway 来源，人工补单须通过支付订单查单，不能在人工登记中指定网关来源或命名空间。

人工入账流程：

1. `POST /receiving-accounts` 创建收款账户，填写 name、reference、currency。
2. `POST /organizations/{org}/funding-records` 创建登记草稿，填写 receiving_account_id、external_reference、amount、currency、received_at 和 evidence。received_at 必须带时区，不能在未来；金额必须为正的十进制字符串。
3. 确认外部真实到账后，携带草稿 ETag 和 Idempotency-Key 调用 `POST /organizations/{org}/funding-records/{id}/post`。余额、唯一 funding_credit、登记状态、幂等和审计在同一事务提交。
4. `GET /organizations/{org}/account` 查看 balance、held_amount 和 available_balance；`GET /organizations/{org}/ledger` 查看流水。

同一 namespace 内的 external_reference 全局唯一，不能给另一组织再次入账。未过账草稿可取消并重新登记；已过账收款不能释放这个唯一标识。收款账户停用后不接受新登记，已有真实收款草稿仍可过账。

## 接口

`{org}`、`{id}` 为 UUID，OpenAPI 中分别名为 organization_id 和资源专属的标识，例如 funding_record_id、adjustment_id、budget_policy_id、price_version_id、discount_version_id。列表使用 limit（1–100，默认 50）与 cursor 分页。

| 接口 | 行为 |
| --- | --- |
| `GET/POST /receiving-accounts` | 财务查询或创建线下收款账户 |
| `PATCH /receiving-accounts/{id}` | 改名或切换 active/disabled，不能改变 reference、币种和 namespace |
| `GET /organizations/{org}/account` | 权威余额、在途预留、可用余额、状态和读取时间 |
| `GET/POST /organizations/{org}/funding-records` | 查询或新建人工登记 |
| `GET/PATCH /organizations/{org}/funding-records/{id}` | 查看或替换草稿可编辑内容 |
| `POST /organizations/{org}/funding-records/{id}/post` | 幂等过账 |
| `POST /organizations/{org}/funding-records/{id}/cancel` | 取消草稿 |
| `GET/POST /sale-price-versions`、`GET/POST /cost-price-versions` | 管理员查询或创建价格草稿，列表可按 scope_id 筛选 |
| `GET/PATCH /sale-price-versions/{id}`、`GET/PATCH /cost-price-versions/{id}` | 查看或替换价格草稿内容 |
| `POST /sale-price-versions/{id}/publish`、`POST /cost-price-versions/{id}/publish` | 发布不可变价格版本 |
| `GET/POST /organizations/{org}/discount-versions` | 查询或创建组织折扣草稿 |
| `PATCH /organizations/{org}/discount-versions/{id}` | 修改未发布的 factor |
| `POST /organizations/{org}/discount-versions/{id}/publish` | 发布不可变折扣版本 |
| `GET /organizations/{org}/pricing?model_id={model}` | 当前基础售价、折扣、展示单价、版本与生效时间；可带 project_id、key_id |
| `GET/POST /organizations/{org}/budget-policies` | 查询或新建固定日/月金额预算 |
| `GET/PATCH /organizations/{org}/budget-policies/{id}` | 查看预算；仅修改 limit_amount 或 active/disabled 状态 |
| `GET /organizations/{org}/budget-policies/{id}/periods` | UTC 期间、限额、spent、reserved 与 over_limit |
| `GET/POST /organizations/{org}/adjustments` | 查询或创建来源调整草稿 |
| `GET /organizations/{org}/adjustments/{id}` | 查看调整 |
| `POST /organizations/{org}/adjustments/{id}/post` | 幂等执行反向资金调整 |
| `GET /organizations/{org}/ledger`、`GET /organizations/{org}/ledger/{id}` | 只读资金流水 |
| `GET /organizations/{org}/requests`、`GET /organizations/{org}/requests/{id}` | 平台和企业按权限查看请求与预留状态，开发者限于获授权项目 |
| `POST /organizations/{org}/account/reconcile` | 财务核对账户、资金流水、预留、预算及单据关系；保存核对审计 |

PATCH、发布、过账与草稿取消要求 `If-Match`。收款账户、入账、调整、价格和折扣的创建，以及发布和过账，还要求 `Idempotency-Key`。请求失败或响应丢失时，使用原键、原请求体及原 If-Match 重试；同键不同参数返回 409，过期版本返回 412，缺少必要头返回 428。

入账和价格草稿的 PATCH 使用完整可编辑内容；归属组织、模型/部署、创建者等身份字段不可改变。所有已发布价格、折扣、已过账登记、调整及流水不可覆盖；数据库也拒绝修改这些历史事实。入账查询会区分 manual/gateway，网关入账的 created_by/posted_by 为 null，system_actor 明确为 payment-worker，不冒用财务人员身份。两类入账共用过账原语并在同一事务发布 billing.funding_posted 事件。

## 定价与精度

售价创建接受 model_id；成本价创建接受 deployment_id。两者都带 currency、rates 和可选 enabled（默认 true）。rates 是按计量维度索引的映射：

```json
{
  "input_uncached_tokens": {"unit_price": "2.000000000000", "unit_quantity": 1000000},
  "output_tokens": {"unit_price": "8.000000000000", "unit_quantity": 1000000}
}
```

维度必须与公开模型完整匹配，不能省略某个缓存维度或以缺失价格代替零。金额和单价最多 12 位小数，拒绝 JSON 浮点数、NaN、Infinity、越界值；价格单位只接受整数 1、1000、1000000。示例价格仅用于说明接口，不是运营报价。

发布请求使用 `{}` 表示在服务器当前时间生效，也可提供带时区的 effective_at。相同作用域的发布起点必须严格递增且不得追溯；下一版本的起点构成上一版本的结束边界。未来版本一经发布也不能修改，计划发布时间需谨慎选择。停用通过发布 enabled=false 的新价格版本完成，历史授权仍使用原版本。

折扣草稿仅接受 factor，范围 `0 < factor <= 1`、最多六位小数。每个无自定义折扣的组织都明确使用不可变的默认版本 `00000000-0000-0000-0000-000000000001`，系数为 1。恢复原价时发布 factor 为 1 的新版本。充值金额不使用折扣。

预留时锁定售价与折扣，开始每次尝试时锁定成本版本。客户费用先用基础价格与折扣计算各维度未舍入金额，再对总和执行一次 ROUND_HALF_UP 到 12 位小数；预留上限向上舍入。pricing 返回的 discounted_unit_price 仅供展示，不能作为再次计价的输入。客户接口不返回上游成本。

## 预算、调整与核对

预算的 layer 为 platform 或 organization，scope_type 为 organization、project 或 key，非组织作用域必须给出 scope_id。所有命中的平台与企业预算共同限制请求，企业自建预算不能覆盖平台上限。scope、周期和归属创建后不可修改。预算策略、期间、请求和占用使用包含组织 ID 的复合外键，预留请求的企业、项目和服务账号也必须与引用的 Key 一致；这些引用约束不授予 billing 读取 identity 私有表的权限。

预算按 UTC 固定日或月计算，使用 spent + reserved 限制消费。中途新增或恢复预算会计入本期间已有消费和在途请求；降低额度不会清空已有占用，超过新限额后拒绝新预留。充值不会增加预算限额。

调整须引用原 funding_credit 或 usage_debit 的 source_entry_id，并填写正数 amount、currency、reason、evidence。原充值对应反向扣减，原消费对应退费；不能引用另一条调整继续叠加反向链。并发调整总额不能超过原金额，充值撤销不得使用已被预留的资金。消费退费同步减少原授权期间的净消费，即使已经跨月也不影响当前期间的预算。

按账户对账返回 consistent、issues 和 as_of，并保存审计。Worker 每小时轮巡核对账户，发现差异时创建核对事项。财务人员可查看证据和处理历史，详见[查询与运营](operations.md)。

网关预留、尝试和用量通过数据库函数提交。退款、超上界和缺失用量的处理见[计量与账务](billing.md)和[异常核对](operations.md#财务核对)。
