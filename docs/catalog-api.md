# 模型与上游目录

## 配置顺序

1. 平台管理员创建公开模型，填写协议族、Token 计量维度和输入/输出上限。
2. 创建 HTTPS 上游，选择协议族，写入 API Key 凭据。
3. 建立部署映射，把公开模型关联到上游模型名称与同一上游的有效凭据。
4. 平台管理员为企业建立组织级 platform 模型策略。
5. 企业 owner 根据需要添加 customer 策略；获授权 developer 可以收窄其项目或 Key 范围。
6. 企业成员携带组织、项目或 Key 上下文查询可见模型。

当前协议族元数据支持 openai_compatible、anthropic、gemini 和 aws_bedrock，Token 模型使用 tokens.v1 计量定义。这些是控制平面的目录标签。客户端协议为 Anthropic Messages 和 OpenAI Responses，Relay 另可连接 Amazon Bedrock Runtime；部署能否执行请求还取决于实际端点及已验收的能力。[Relay 协议范围](relay-design.md#协议覆盖与交付条件)

模型限制和能力由管理员填写，尚未通过真实上游调用验证。

## 目录 API

下列路径以 `/api/v1` 为前缀。除客户模型查询外，目录写入、上游、凭据和部署查询都要求 platform_admin；platform_finance 和企业 owner 不因此获得供应侧权限。

| 接口 | 行为 |
| --- | --- |
| `GET/POST /models` | 管理员查询全部配置或创建公开模型；客户查询按上下文过滤 |
| `GET/PATCH /models/{id}` | 查询或修改模型；public_name 不可改，修改需 If-Match |
| `GET/POST /upstreams` | 查询或创建上游 |
| `GET/PATCH /upstreams/{id}` | 修改名称、状态或地址；协议族不可改 |
| `GET/POST /upstreams/{id}/credentials` | 查询凭据元数据或加密保存新凭据 |
| `GET /upstreams/{id}/credentials/{credential_id}` | 查询元数据，不返回密文或明文 |
| `POST /upstreams/{id}/credentials/{credential_id}/revoke` | 撤销凭据，立即影响关联部署的配置状态 |
| `GET/POST /deployments` | 查询或创建模型到上游的映射 |
| `GET/PATCH /deployments/{id}` | 修改上游模型名、凭据、区域、名称或状态；模型与上游归属不可改 |

单资源响应包含 ETag，更新和撤销使用 If-Match。列表使用 limit/cursor，部署可按 model_id、upstream_id 筛选。资源采用停用和归档，归档后不再修改；凭据即使属于已归档上游，仍可撤销。

上游地址只接受 HTTPS，拒绝 URL 中的用户名、密码、查询参数和 fragment。变更地址前必须先停用上游。当前不请求地址、探测 DNS 或验证真实连通性；内网 HTTPS 地址也仅作为配置保存。

部署的 `relay_profile.convert_from` 可显式启用另一种客户端协议。各方向的端点值、节点能力和开关示例见 [Relay 配置](relay-development.md#启用-http-和-sse-协议转换)。

仅提供 Chat Completions 的 OpenAI 兼容上游使用 `relay_profile.endpoint = "openai_chat_completions"`，支持 Responses 客户端的 HTTP JSON 和 SSE。[部署示例与计量约定](relay-development.md#responses-客户端使用-chat-completions-上游)

部署凭据必须属于其上游，协议族必须在公开模型的支持范围内。修改模型协议时，如果仍有不兼容的启用部署，返回冲突；应先停用这些映射。停用或归档部署不要求旧凭据仍有效，避免失效配置无法收起。

模型和部署的 configured 字段根据启停状态、协议匹配和已加载的加密密钥版本计算。它表示配置前提是否满足，不表示已验证密文可解密、厂商在线、价格已生效或具有调用额度。客户目录仅显示有效且 configured 的授权模型。

按次收费的工具配置在模型的 `tool_dimensions` 中。开启 Web Search 需先为 `web_search_requests` 发布销售价和部署成本价，再设置 `relay_profile.web_search_ceiling`。部署已启用搜索时，模型须保留该维度。请求次数上限和标准版、preview 版的选路见 [Relay Web Search](relay-development.md#web-search)。

原生 Responses 部署可通过 `file_search_ceiling` 启用 File Search。模型须同时发布检索次数和存储字节秒两个价格维度。计价单位、上游账户绑定及存储用量接口见 [File Search 和向量存储](relay-development.md#file-search-和向量存储)。

Claude 独立代码执行使用 `code_execution_seconds` 维度和部署的 `execution_reserve_seconds`。OpenAI Code Interpreter 的容器内存档分别定价，通过 `container_tariff` 和 `container_reserve_minutes` 匹配上游计费方式。两者的资源绑定、免执行费组合和计量证据见 [代码执行和生成文件](relay-development.md#代码执行和生成文件)。

## Amazon Bedrock 部署

在公开模型的 `protocols` 中添加 `aws_bedrock`。创建同协议族的上游，填写区域 Runtime 的 HTTPS 基础地址，例如 `https://bedrock-runtime.us-east-1.amazonaws.com`。部署的 `region` 填 AWS 区域，`upstream_model` 填该区域支持的模型 ID、推理配置文件 ID 或受支持的 ARN。两种客户端分别使用已有的 `POST /v1/messages` 和 `POST /v1/responses`。

部署凭据的 `secret` 支持 Bedrock API Key，也支持紧凑 JSON 字符串形式的 AWS 签名凭据，字段为 `access_key_id`、`secret_access_key`，以及临时凭据的 `session_token`。这两种凭据均由 catalog 加密并随集群密文包发给节点；签名凭据使用官方 SigV4 签名器、部署的区域和服务名 `bedrock`。临时凭据到期前按现有凭据轮换流程更换。[AWS 的 Bedrock API Key](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html)

`relay_profile.endpoint` 设为 `bedrock_converse`，`transports` 设为 `["http"]`，`count_rule` 使用 `deployment_ceiling.v1`。按已验证模型填写 `input_ceiling`、`output_ceiling` 和 `bound_reference`。该上游直接服务两个客户端协议，省略 `convert_from`。需要工具或流式输出时，同时开启模型和部署的相应能力。集群节点须登记 `relay.bedrock-converse.v1` 及 `relay.native.v1`；启用流式时另需 `relay.sse.v1`。先升级控制平面到 `0014_bedrock_converse` 和相应 Worker，再发布快照。

Converse 部署可在 `relay_profile.bedrock_guardrail` 中配置 AWS 的 `guardrailIdentifier`、`guardrailVersion` 和 `trace`，并在 `bedrock_model_fields` 配置模型支持的 `additionalModelRequestFields`。使用这两项时节点需声明 `relay.bedrock-settings.v1`。模型专有参数在出站时与客户端显式参数核对；冲突或覆盖模型、消息、输出上限时拒绝。计数端点只转发 AWS CountTokens 支持的字段。 配置 Guardrail 时节点还需登记 `relay.bedrock-guardrail-usage.v1`，按策略计费的维度、上界和回执要求见[Guardrail 用量与费用](relay-development.md#guardrail-用量与费用)。

需要让租户逐请求选择配置时，在部署的 `bedrock_request_options` 列出允许的组合，并为节点登记 `relay.bedrock-request-options.v1`。例如，以下配置允许在这次生成上切换到 Guardrail 版本 2，同时把 `top_k` 改为 7。未覆盖的字段沿用部署默认值。

```json
{
  "bedrock_request_options": [
    {
      "guardrailConfig": {"guardrailIdentifier": "abcd", "guardrailVersion": "2"},
      "additionalModelRequestFields": {"top_k": 7}
    }
  ]
}
```

客户端在 Messages、Responses 或 Chat 请求的顶层 `bedrock` 对象中传入该组合；SDK 可通过 `extra_body` 设置。空对象选择 Converse 的默认配置。Relay 在已授权候选中匹配默认值或一个完整的已发布组合，重试时再次匹配。

## 凭据加密与轮换

凭据仅接受不含空白或控制字符的 ASCII API Key。使用 PyCA cryptography 的 HKDF-SHA256 从根密钥派生专用存储密钥，再以 AES-256-GCM 加密；每次生成随机 12 字节 nonce，并把上游 ID、凭据 ID、密钥版本绑定为附加认证数据。篡改密文或跨记录替换时无法通过解密认证。[AEAD 文档](https://cryptography.io/en/latest/hazmat/primitives/aead/)、[HKDF 文档](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/)

数据库保存密文、密钥版本和元数据，不保存根密钥。任何 HTTP 接口都不提供明文读取，创建响应也不回显提交的 secret。审计只记录元数据；模型、上游、部署和访问策略更新保留可复核的前后状态。

上游凭据轮换采用“创建新凭据 → 更新部署引用 → 撤销旧凭据”。允许新旧凭据同时存在以支持切换，已撤销的凭据不可恢复。查询接口不会因上游停用而丢失历史凭据信息。

| 运行参数 | 含义 |
| --- | --- |
| `NEXUS_ENCRYPTION_KEY` | 当前根密钥，32 字节随机值的 Base64 编码 |
| `NEXUS_ENCRYPTION_KEY_VERSION` | 当前密钥版本，默认 1 |
| `NEXUS_PREVIOUS_ENCRYPTION_KEYS` | 额外密钥版本到 Base64 密钥的 JSON 映射，默认空对象 |

同一版本必须始终对应同一份密钥材料。切换根密钥时先让需要处理数据的实例具备新旧版本，再更新当前版本；保留仍被密文引用的旧版本。当前不会自动重加密旧记录。缺失版本时关联部署显示 configured=false；同版本填错材料只能在解密认证时发现，不能把版本号存在当作解密验证。

## 模型访问策略

接口为 `/organizations/{org}/access-policies` 的 GET/POST，以及 `/{policy_id}` 的 GET/PATCH。创建字段是 layer、scope_type、scope_id、allowed_models。layer 为 platform 或 customer；scope_type 为 organization、project 或 key。组织级 scope_id 可省略，其余必须提供实际资源 ID。

- 必须存在 platform 组织级策略，否则默认拒绝所有模型。
- 有效范围是路径上全部 platform/customer 策略的交集。
- 未设置下级策略表示继承；显式空列表表示拒绝全部。
- platform 策略只允许平台管理员写入；平台项目/Key 范围也不能扩大上级平台范围。
- 企业 owner 可收窄本组织及其项目/Key；developer 仅可收窄获授权项目及其 Key，不能修改组织级范围。
- 现有策略只能修改 allowed_models；暂不提供删除/恢复继承接口。父级收窄会立即限制子级，不要求逐个重写子级记录。
- 只允许引用存在且有效的模型。模型随后被停用或供应配置失效时，仍会从客户目录过滤掉；历史策略引用继续保留。

客户查询 `GET /models` 或 `GET /models/{id}` 必须提供 organization_id，可进一步提供 project_id 或 key_id。所有上下文必须匹配且对当前用户可见；跨企业、未授权项目和不匹配的 Key 返回 404。平台管理员不提供组织上下文时查询完整管理目录，提供组织上下文时也按该企业实际范围预览。

完成模型授权后，还需发布售价、部署成本和路由。节点在调用时按实际余额和预算预留资金，使用授权版本的上游部署。[定价](billing-api.md) · [节点接入与配置发布](plane-protocol.md#节点接入与配置发布)
