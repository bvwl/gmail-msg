# FastAPI 与 Vue 3 接口设计

当前文档定义接口契约；除健康检查外，管理 API 支持管理员 Session Cookie 或配置的外部 API Key。FastAPI 文档页面使用独立 Basic Auth 保护。

FastAPI 只提供 REST API。Vue 3 以独立来源访问 API，服务端按环境配置 CORS 白名单和是否允许凭据；禁止通配符来源与 Cookie 同时使用。浏览器预检请求只允许白名单来源、明确方法和明确请求头。

## 1. 认证

### FastAPI 文档认证

`/docs`、`/redoc` 和 `/openapi.json` 默认关闭；配置 `DOCS_USERNAME` 与 `DOCS_PASSWORD` 后才启用，并要求 HTTP Basic Auth。文档账号与管理员登录账号分开配置。

### 外部 API Key

配置 `EXTERNAL_API_KEY` 后，外部调用可以使用以下任一请求头访问原本需要管理员 Session 的 API：

    X-API-Key: EXTERNAL_API_KEY
    Authorization: Bearer EXTERNAL_API_KEY

API Key 不返回给前端，不写入日志；生产环境必须配置随机长值。未配置 API Key 时，接口只接受管理员 Session Cookie。

### POST /api/admin/login

请求体：

    {"username":"ADMIN_USER","password":"ADMIN_PASSWORD"}

成功返回 204，并设置 Cookie：

- 名称由配置指定，例如 admin_session。
- HttpOnly、Secure、SameSite=Strict，生产环境必须设置 Path=/ 和合理过期时间。
- Cookie 值为随机不可预测的 Session ID；Session 记录存 PostgreSQL，服务重启后仍可按过期时间验证。

失败统一返回 401，不说明用户名是否存在，也不记录密码或 Cookie。

### POST /api/admin/logout

撤销当前 Session，清除 Cookie，成功返回 204。重复登出应保持幂等。

### GET /api/admin/audit-logs

返回最近的管理员审计记录，包括操作类型、资源类型/标识、成功标志、脱敏来源地址、脱敏元数据和 UTC 时间。不得返回 Cookie、Session 值、密码、正文、附件、原始 MIME 或验证码原文。

## 2. 邮箱管理

### GET /api/mailboxes

返回邮箱地址、启用状态、监控文件夹、连接状态、最近心跳、最近成功同步时间和脱敏错误代码。不返回 App Password 或 Secret 值。

### POST /api/mailboxes

请求示例：

    {
      "address":"MAILBOX",
      "imap_host":"imap.gmail.com",
      "imap_port":993,
      "username":"MAILBOX",
      "app_password_secret_ref":"SECRET_REF",
      "proxy_mode":"direct",
      "proxy_host":null,
      "proxy_port":null,
      "proxy_username":null,
      "proxy_password_secret_ref":null,
      "proxy_dns_mode":"remote",
      "folders":["INBOX","[Gmail]/Spam"]
    }

服务端保存 Secret 引用，不保存明文 App Password 或代理密码。proxy_mode=direct 时代理字段必须为空且只允许直连；proxy_mode=socks5 时必须提供 proxy_host、proxy_port、proxy_dns_mode 和代理凭据引用。文件夹名称以 IMAP LIST 返回值校验；未知文件夹返回 400。

### PATCH /api/mailboxes/{mailbox_id}

更新启用状态、监控文件夹、显示名称、IMAP Secret 引用或代理配置。修改后 Worker 在安全边界重载配置；切换代理模式前先停止当前 IMAP 连接，不能复用旧网络路径。

### GET /api/mailboxes/{mailbox_id}/status

返回邮箱级 proxy_mode、proxy_status、connection_state、proxy_dns_mode，以及每个文件夹的 UIDVALIDITY、UIDNEXT、last_seen_uid、IDLE 状态、最后成功时间和错误分类。不得返回代理密码、Secret 引用对应的值或 IMAP 原始响应。

## 3. 邮件查询

### GET /api/mailboxes/{mailbox_id}/messages

查询参数：

- folder=inbox|spam|all|<folder_id>。
- q：主题、发件人、收件人和正文的 PostgreSQL 全文搜索词。
- recipient：按具体收件人地址筛选；Gmail 本地部分的点号差异会按等价地址匹配，例如 `xxx.111@gmail.com` 可以匹配 `xxx111@gmail.com`。
- page、page_size：服务端限制最大页大小。
- from、to：UTC 时间范围。
- has_code=true|false：是否包含提取到验证码的邮件。

返回分页游标或页码、总数（可选近似值）以及每封邮件的 ID、主题、地址、日期、所在标签、正文摘要和是否存在验证码。列表接口不得返回完整正文。

### GET /api/messages/{message_id}

返回完整规范化正文、元数据、所有标签、保存时间和验证码结构化结果。正文只从 PostgreSQL 返回；附件和原始 MIME 字段不存在。

### GET /api/messages/{message_id}/body

仅返回 text/plain 或已规范化的 HTML 正文。若返回 HTML，前端必须使用严格的清理策略后再展示，禁止执行脚本。

## 4. 验证码

### GET /api/mailboxes/{mailbox_id}/codes

按接收时间倒序返回最近验证码：邮件 ID、验证码类型、脱敏后的发件人、提取时间和过期提示。默认只返回最近 30 天数据；验证码原文只在管理员已登录且请求明确包含 include_value=true 时返回，响应不得缓存。

验证码提取规则、误报和多验证码场景在测试设计中定义；提取失败不影响邮件入库。

## 5. 同步与清理状态

### GET /api/sync/status

返回每个邮箱/文件夹的 Worker 状态、IDLE 或补查阶段、游标、最近心跳、最近错误和待补查 UID 数量。不得返回正文、密码或原始 IMAP 响应。

### GET /api/retention/status

返回最近一次清理的开始/结束时间、扫描数量、删除数量、失败数量、下次计划时间和当前最早保留时间。不得返回被清理邮件内容。

### GET /api/config/frontend

返回当前允许的前端来源标识和 API 基础路径，供独立 Vue 3 应用显示诊断信息；不得返回数据库、IMAP 或代理 Secret。

## 6. 错误模型

错误统一使用：

    {"error":{"code":"AUTH_REQUIRED","message":"登录已失效","request_id":"REQUEST_ID"}}

稳定错误码包括 AUTH_REQUIRED、FORBIDDEN、NOT_FOUND、INVALID_INPUT、IMAP_AUTH_ERROR、SYNC_UNAVAILABLE、DATABASE_UNAVAILABLE 和 RATE_LIMITED。消息中不得嵌入密码、Session、邮件正文或完整 IMAP 响应。

## 7. Vue 3 页面契约

- 登录页调用 /api/admin/login，成功后跳转邮箱管理。
- 邮箱管理页展示连接状态、文件夹和最近错误，可启停邮箱和更新 Secret 引用。
- 邮箱管理页同时展示 proxy_mode、proxy_status、当前错误原因、最近同步时间、UID、UIDVALIDITY 和 IMAP 连接阶段。
- Inbox、Spam、全部邮件页使用同一列表组件，支持搜索、分页、标签筛选和验证码快捷入口。
- 详情页展示完整正文、元数据和标签，HTML 正文先清理后渲染。
- 状态页展示 Worker 心跳、UID 游标、断线补查、认证错误和 30 天清理进度。

