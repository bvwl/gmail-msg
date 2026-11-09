# 安全设计

## 1. 管理员认证

- 管理员密码使用 Argon2id 或 bcrypt 哈希，禁止明文和可逆加密。
- 本地管理员账号可由 `.env` 的 `ADMIN_USERNAME` 与 `ADMIN_PASSWORD` 初始化或同步；密码只用于启动时生成或更新 Argon2id 哈希，前端仍通过登录接口获取 HttpOnly Session Cookie。
- 登录成功后创建随机 Session ID，只把其哈希写入 PostgreSQL。
- Cookie 必须设置 HttpOnly、Secure、SameSite=Strict（若跨站部署有明确理由才评估 Lax），并设置过期时间。
- 所有邮箱、邮件、验证码、同步和清理接口都要求有效管理员 Session。
- 登录失败采用统一响应，避免暴露用户名存在性；可按 IP 和用户名组合限流。
- 登出撤销 PostgreSQL Session；Session 过期由查询和清理任务回收。

FastAPI `/docs`、`/redoc` 和 `/openapi.json` 使用独立的 `DOCS_USERNAME`、`DOCS_PASSWORD` Basic Auth。外部接口使用 `EXTERNAL_API_KEY` 时必须通过 `X-API-Key` 或 `Authorization: Bearer` 传递，服务端使用常量时间比较，不记录 Key。

## 2. IMAP 凭据

App Password 只能通过环境变量或 Secret Manager 提供。数据库只保存 Secret 引用或经过应用层加密的凭据（实现阶段需确定密钥托管方案），不在 API 响应、Vue、日志、异常堆栈或测试输出中出现。

IMAP 连接必须启用 TLS 和证书校验；认证失败应停止无限重试并把邮箱置为 auth_error，等待管理员修复 Secret。

### SOCKS5 代理边界

- 代理配置按邮箱隔离，FastAPI、Vue 3、管理员登录和 PostgreSQL 永远不经过邮箱代理。
- proxy_mode=socks5 时先检查代理可用性；代理不可用、代理认证失败或 DNS 模式错误时，不得尝试直连 Gmail IMAP。
- 代理恢复前不得向 Gmail 发送用户名、密码或 App Password；代理检查请求只连接代理端点。
- proxy_mode=direct 时忽略系统代理变量，只允许直连；直连失败不得自动切换 SOCKS5。
- 日志只记录 proxy_mode、proxy_status 和脱敏错误分类，不记录代理地址中的凭据、完整 SOCKS5 握手或密码。
- IMAP 目标主机使用 allowlist；请求拒绝私网、回环、链路本地和保留地址，代理主机可通过 `PROXY_ALLOWED_HOSTS` 进一步限制。

## 3. 邮件数据保护

- 不保存附件二进制、附件名称、原始 MIME 或未清理的 HTML。
- 正文只写入 PostgreSQL，日志、异常堆栈、指标标签和测试输出不得包含正文、主题、完整地址或验证码原文。
- API 列表默认返回摘要，正文通过单独的已认证接口读取。
- HTML 展示前只保留严格标签和属性 allowlist；不保留图片、样式、SVG、脚本、表单或未知属性。
- 30 天后自动删除邮件实体、正文、标签关系和验证码，并保留不含正文的清理统计。
- PostgreSQL、备份和磁盘访问按最小权限控制并启用组织要求的加密。

## 4. Web 与接口安全

- 生产环境所有管理页面和 API 使用 HTTPS。
- FastAPI 只允许配置的 Vue 3 来源通过 CORS 访问；禁止使用通配符来源并携带 Cookie。
- 生产环境对带 Session Cookie 的非安全方法强制校验 Origin/Referer；没有匹配来源的请求返回 403。
- 邮件搜索使用参数化查询和 PostgreSQL 全文搜索，禁止拼接 SQL。
- 搜索词长度有限制，并转义 `%`、`_` 和反斜杠，避免通配符资源滥用。
- 分页、搜索词、日期范围和正文返回大小设上限，防止资源耗尽。
- RFC822 正文按 `MESSAGE_BODY_MAX_CHARS` 截断，搜索词按 `MAX_QUERY_LENGTH` 限制。
- 错误响应只返回稳定错误码和 request_id，不返回 IMAP 原始响应或数据库连接信息。
- 前端不在 Local Storage、URL 或埋点中保存 Session、App Password、正文或验证码。
- Vue 3 不直接连接 PostgreSQL、IMAP 或 SOCKS5；所有数据访问必须经过已认证的 FastAPI REST API。

## 5. 日志与审计

日志只记录邮箱脱敏标识、邮箱/文件夹 ID、UID 范围、状态、耗时、错误分类和 request_id。禁止记录完整 Authorization 头、Cookie、密码、Session ID、IMAP 命令响应、邮件正文、原始 MIME 和验证码值。`audit_logs` 持久化管理员登录、登出、Session 失效、邮件/验证码查看和邮箱配置修改，只保存操作类型、资源标识、成功标志及脱敏元数据。

## 6. 数据库权限

应用账号只拥有所需 schema 权限；迁移账号与运行账号分离。PostgreSQL 不允许公网访问，管理员查询和 Worker 写入均使用最小权限连接。不要使用 Redis 或进程外缓存保存认证、正文或同步状态。

## 7. 未验证事项

当前阶段没有使用真实邮箱凭据。隔离环境已验证 Argon2id、Cookie Session、API 审计和浏览器联调；真实 Gmail TLS/文件夹/IDLE 行为、生产代理、生产数据库、生产 Cookie 部署和 30 天清理性能仍未验证。

