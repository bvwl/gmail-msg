# 部署与配置说明

生产部署提供 `compose.prod.yaml`、后端 `Dockerfile` 和前端 Nginx 镜像。目标部署包含独立 Vue 3 前端、FastAPI REST API、每邮箱 IMAP Worker、清理任务和一个 PostgreSQL 数据库。

## 1. 拓扑

    Browser -> HTTPS reverse proxy -> frontend (Nginx)
                                      │ REST + Cookie
                                      ▼
                                FastAPI REST API -> PostgreSQL (Compose backend network)
                                      │
                                      └─ per-mailbox IMAP Worker -> direct or SOCKS5 -> Gmail IMAP TLS

- PostgreSQL 不暴露公网，只允许 FastAPI、Worker 和清理任务访问。
- Compose 只把前端发布到本机回环端口；公网 HTTPS 由宿主机反向代理或云负载均衡终止。
- IMAP 连接必须使用 TLS（通常 993 端口）；证书校验开启。
- Vue 3 独立构建和部署；FastAPI 只提供 REST API，不直接承载前端页面。
- 反向代理为 Vue 3 配置独立来源，为 FastAPI 配置独立 API 地址和 HTTPS；FastAPI CORS 只允许该前端来源。
- 邮箱 SOCKS5 代理只用于对应 IMAP Worker 的出站连接；FastAPI、Vue 3、管理员登录和 PostgreSQL 不使用邮箱代理。
- 不部署 Redis、Pub/Sub、Google Cloud Scheduler、业务 Webhook 或额外队列。

## 2. 环境变量样例

.env.example 只包含占位符。至少需要：

- APP_ENV、LOG_LEVEL、PUBLIC_BASE_URL。
- DATABASE_URL、连接池大小、连接超时和健康检查超时。
- ADMIN_USERNAME、ADMIN_PASSWORD、ADMIN_SESSION_COOKIE、ADMIN_SESSION_TTL_SECONDS。
- DOCS_USERNAME、DOCS_PASSWORD（启用并保护 `/docs`、`/redoc`、`/openapi.json`）。
- EXTERNAL_API_KEY（外部接口调用时使用 `X-API-Key` 或 Bearer Token）。
- IMAP_IDLE_TIMEOUT_SECONDS、IMAP_RECONNECT_BASE_SECONDS、IMAP_RECONNECT_MAX_SECONDS、IMAP_FETCH_BATCH_SIZE。
- IMAP_DEFAULT_HOST、IMAP_DEFAULT_PORT=993（单邮箱配置仍需通过 Secret 引用提供用户名和 App Password）。
- CORS_ORIGINS、API_BASE_URL、CORS_ALLOW_CREDENTIALS=false/true（按部署拓扑显式配置，禁止任意来源）。
- IMAP_PROXY_DEFAULT_MODE=direct；邮箱级 proxy_mode、proxy_host、proxy_port、proxy_username、proxy_password_secret_ref、proxy_dns_mode。
- MESSAGE_RETENTION_DAYS=30、RETENTION_INTERVAL_SECONDS、RETENTION_BATCH_SIZE。
- VERIFICATION_CODE_LOOKBACK_DAYS=30、SEARCH_MAX_PAGE_SIZE。

App Password 只能通过环境变量或 Secret Manager 注入。配置文件、Git、镜像层、日志和前端代码不得包含真实值。

前后端分离环境至少定义三套地址关系：开发环境的 Vue 开发服务器来源与 FastAPI 本地 API 地址；测试环境的独立前端来源、隔离 API 地址和测试数据库/IMAP 地址；生产环境的 HTTPS 前端来源、HTTPS API 地址和仅内网可达的 PostgreSQL。CORS 白名单必须按环境分别配置，不能把开发来源带入生产。

仓库中的现有本地 .env 可能包含旧阶段变量。实现代码不会读取这些旧变量；部署前应人工按当前配置迁移并移除旧变量。

## 3. 进程与健康检查

FastAPI 启动时验证 PostgreSQL 连接和必要配置；健康检查不得回显连接串、用户名密码或邮件内容。Worker 启动后为每个启用邮箱建立唯一租约，租约失效或进程停止后由新的 Worker 接管。

清理任务可作为独立进程或受控计划任务运行，但必须连接同一个 PostgreSQL。任务完成后在 retention_runs 写入可审计状态。

## 4. 备份与恢复

PostgreSQL 备份必须包含 schema、邮件正文、标签关系、Session 撤销状态和同步游标，并按组织的保留策略加密保存。恢复演练要验证 UID 游标和 UIDVALIDITY 未被错误重置；恢复后 Worker 应执行补查而非跳过未处理 UID。

备份、日志和监控系统不得把邮件正文、原始 MIME、附件或 App Password 写入可检索字段。

## 5. 运行限制

IMAP IDLE 是连接保持机制，不保证服务器事件永不丢失；Worker 必须设置 IDLE 超时并周期性 UID 补查。Gmail 文件夹名称可能因账户语言和服务器实现变化，部署时先通过 LIST/SPECIAL-USE 检查并在邮箱配置中保存实际名称。

本轮已在本机隔离服务完成迁移、API 和浏览器联调；真实邮箱、生产 PostgreSQL、生产 SOCKS5、TLS 证书链、反向代理和生产浏览器部署效果仍需单独验收。

## 6. Compose 生产启动

1. 复制 `.env.production.example` 为 `.env.production`，填写随机数据库密码、管理员密码、Docs 密码、API Key 和 Fernet 密钥；不要把该文件提交到 Git。
   Fernet 密钥可用 `conda run -n zy314 python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成。
2. 将 `CORS_ORIGINS` 设置为实际 HTTPS 前端来源，并在宿主机反向代理中配置 HTTPS、HSTS、证书和限流。
3. 执行 `docker compose --env-file .env.production -f compose.prod.yaml up -d --build`。
4. 验证 `docker compose --env-file .env.production -f compose.prod.yaml ps`，确认 `migrate` 已成功完成、`api` 健康、`frontend` 正常运行。
5. PostgreSQL 没有宿主机端口映射；备份使用 `docker compose exec postgres pg_dump` 或受控备份任务，并加密保存。

