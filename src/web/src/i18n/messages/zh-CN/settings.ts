// Platform settings: business policies stored in each module's policy table.
const settings = {
  title: "设置",
  description:
    "平台业务策略按模块保存在数据库里，修改带版本和审计。API 与 Worker 在约 2 秒内全部生效；网关集群的策略随下一次发布下发到节点。",
  version: "版本 {version}",
  save: "保存",
  saved: "已保存",
  reset: "撤销修改",
  invalid: "请填写 {min} – {max} 之间的数值",
  invalidCurrency: "请填写 3 位大写字母的币种代码",
  invalidText: "请填写 1 – {max} 个字符",
  managed: {
    badge: "由策略文件管理",
    title: "由策略文件管理，只读",
    body: "该组由 nexus settings import 锁定（文件摘要 {digest}…，导入于 {at}）。修改请提交策略文件；文件里把该组声明为 initial 即解除锁定。",
  },
  tabs: {
    site: "站点",
    identity: "身份",
    billing: "计费",
    reporting: "报表",
    quotas: "资源配额",
    clusters: "网关集群",
  },
  site: {
    title: "站点信息",
    description: "站点名称、控制台的公开地址和联系邮箱。站点名称用于控制台和身份邮件，充值完成后付款人回到控制台地址下的充值页。",
    completed: "初始化已于 {at} 完成。需要重新初始化时，在主机上执行 nexus setup reset。",
    pending: "尚未完成初始化。",
    fields: {
      site_name: "站点名称",
      console_url: "控制台地址",
      contact_email: "联系邮箱",
    },
    hints: {
      site_name: "最多 100 个字符",
      console_url: "除本机地址外必须是 HTTPS；不填则无法发起在线充值",
    },
  },
  identity: {
    title: "登录、邮件与会话",
    description: "登录尝试和身份邮件的限额、会话与密码设置凭据的有效期、保留登录名。",
    signIn: "登录尝试",
    mail: "身份邮件",
    sessions: "有效期",
    other: "其他",
    fields: {
      sign_in: {
        window_seconds: "统计窗口（秒）",
        per_account: "每个账号",
        per_ip: "每个来源地址",
      },
      mail: {
        window_seconds: "统计窗口（秒）",
        per_recipient: "同一收件人（每个来源网络）",
        per_recipient_total: "同一收件人（全部来源）",
        per_account: "每个账号",
        per_ip: "每个来源地址",
        global_limit: "全平台",
        queue_ttl_seconds: "未发出邮件的保留时间（秒）",
      },
      sessions: {
        session_ttl_seconds: "登录会话（秒）",
        password_setup_ttl_seconds: "密码设置凭据（秒）",
      },
      reserved_logins: "保留登录名",
      platform_email_recovery: "平台角色可通过邮件找回",
    },
    hints: {
      sign_in: {
        per_ip: "IPv6 按 /64 计。",
      },
      mail: {
        per_recipient: "不能大于全部来源的上限。",
        queue_ttl_seconds: "令牌在发出后 15 分钟内有效。",
      },
      reserved_logins: "每行一个，比较前统一为小写的 NFKC 形式；自助注册不能使用这些登录名。",
      platform_email_recovery: "关闭时，持有平台角色的用户只能由其他管理员重新签发凭据。",
    },
    switches: "注册、邮箱验证和 Turnstile 开关在“用户”页维护，和这里共用同一个版本。",
  },
  billing: {
    title: "计费",
    description: "向量存储预付和到期策略、支付渠道的调用与通知限额。",
    currencyGroup: "币种",
    currency: "账务币种",
    currencyHint: "出现第一个账户、价格、收款账户或支付渠道之后不能修改。",
    storage: "向量存储",
    payments: "支付",
    lapse: { delete: "删除", suspend: "暂停" },
    fields: {
      vector_storage: {
        prepaid_days: "预付天数",
        lapse_policy: "续费失败且预付到期后",
      },
      payments: {
        requests_per_merchant_per_minute: "每个商户号每分钟调用",
        notifications_per_channel_per_minute: "每个渠道每分钟通知",
        notifications_per_ip_per_minute: "每个来源地址每分钟通知",
      },
    },
  },
  reporting: {
    title: "导出",
    description: "企业导出任务的频率、保留时间和行数上限，下一次导出立即使用。",
    fields: {
      exports: {
        per_org_minute: "每个企业每分钟导出",
        ttl_seconds: "导出文件保留时间（秒）",
        max_rows: "单次导出最多行数",
      },
    },
  },
  quotas: {
    title: "资源数量默认值",
    description:
      "每个 Key 和每个企业在一个网关集群内最多持有的文件、会话、容器、批处理（各自计数）和向量库。企业的覆盖值在该企业的“限额”页设置（平台层）。",
    capability: "不等于内置默认值时，发布要求节点声明能力 relay.cluster-policy.v1。",
    fields: {
      resources_per_key: "每个 Key 的资源数",
      resources_per_organization: "每个企业的资源数",
      vector_stores_per_key: "每个 Key 的向量库数",
      vector_stores_per_organization: "每个企业的向量库数",
    },
  },
  clusters: {
    defaultsTitle: "集群策略默认值",
    defaultsDescription:
      "所有集群默认使用这组值，包括之后新建的集群。Relay 策略随快照下发，同一集群的所有节点使用同一组值；自动发布的时间窗由 Worker 使用。",
    title: "单个集群的覆盖值",
    description: "留空表示跟随默认值；填写后只对这个集群生效。",
    follows: "跟随默认值（{value}）",
    select: "选择集群",
    empty: "还没有网关集群",
    relay: "Relay",
    autoPublish: "自动发布",
    capability: "Relay 策略不等于内置默认值时，发布要求节点声明能力 relay.cluster-policy.v1。",
    fields: {
      relay: {
        max_attempts: "每个请求最多尝试次数",
        failover_budget_ms: "故障转移时间预算（毫秒）",
        purge_grace_seconds: "Key 停用后清理上游副本的宽限期（秒）",
        stream_lifetime_seconds: "恢复的响应流最长时间（秒）",
      },
      auto_publish: {
        quiet_seconds: "静默时间（秒）",
        max_delay_seconds: "最长延迟（秒）",
      },
    },
    hints: {
      relay: {
        max_attempts: "1 表示不做故障转移。",
      },
      auto_publish: {
        max_delay_seconds: "不能短于静默时间。",
      },
    },
  },
};

export default settings;
