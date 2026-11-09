# AI 编程代理规则

## 当前阶段

当前项目处于文档设计阶段。除非用户明确授权，否则只允许修改 Markdown、配置说明和测试设计文档，不得编写业务代码、数据库迁移、Dockerfile 或部署脚本。

## 语言和沟通

- 与用户使用中文沟通。
- 文档正文使用中文；代码、环境变量名、HTTP 字段名和 IMAP/SQL 官方名称保持官方英文。
- 区分官方保证、本项目设计和尚未验证。

## 安全规则

- 不得在代码、测试、日志、提交信息或文档样例中写入真实密码、App Password、Session Cookie、client secret 或其他密钥。
- 只使用占位符，例如 MAILBOX、SECRET_REF、ADMIN_PASSWORD_HASH。
- 不记录完整 Authorization 头、Cookie、IMAP 原始响应、原始 MIME、完整邮件正文或附件。
- App Password 只能通过环境变量或 Secret Manager 提供。
- 所有公网管理接口必须使用 HTTPS，并设置安全 Cookie 属性。
- 管理员密码使用 Argon2id 或 bcrypt 哈希；邮件、验证码、同步和清理接口都要求管理员登录。
- 对外部 URL 做白名单和 SSRF 防护；HTML 邮件展示前必须清理脚本和危险 URL。

## IMAP 可靠性规则

- 每个邮箱使用一个 IMAP IDLE Worker，至少监控 Inbox、Spam 和配置的其他 Gmail 文件夹。
- IDLE 只作为变化触发机制；断线、超时或进程重启后必须根据 UID、UIDVALIDITY 和 PostgreSQL 游标补查遗漏邮件。
- Gmail 文件夹名称通过 LIST/SPECIAL-USE 发现，不硬编码本地化名称。
- 首次接入默认 baseline，不把全部历史邮件错误地当成新邮件。
- 同一封邮件可能出现在多个标签中；正文只保存一份，通过标签关系区分位置。
- 附件和原始 MIME 不保存；邮件元数据和完整正文保存 30 天，之后自动清理。
- 同一邮箱同时只允许一个 Worker 推进文件夹游标；游标只有在邮件和标签关系事务提交后才能推进。
- IMAP 认证错误停止无限重试；网络超时、BYE、连接断开使用截断指数退避并记录脱敏错误。

## PostgreSQL 规则

- PostgreSQL 是唯一持久化状态源，保存邮箱、UID、UIDVALIDITY、游标、邮件正文、标签、同步状态、管理员 Session、验证码和清理记录。
- 不使用 Redis、消息队列或其他外部缓存；验证码缓存、管理员 Session 和同步状态使用 PostgreSQL 或进程内内存。
- 为稳定邮件键、邮箱文件夹 UID、Session 哈希和标签关系建立唯一约束。
- UID 游标只能在当前批次完整处理并持久化后推进；事务失败时保持旧游标。
- Schema 变更必须有迁移和回滚说明（实现阶段执行）。
- 时间统一使用 UTC。

## FastAPI、Vue 3 和测试规则（实现阶段）

- Python 3.14、FastAPI、SQLAlchemy Async、Pydantic v2；本项目固定使用 Conda 环境 zy314。
- 本项目所有 Python、pytest、Alembic、服务启动、编译和脚本命令必须使用 Conda 环境 zy314，统一写成 conda run -n zy314 python ... 或 conda run -n zy314 python -m ...；禁止使用系统、base、auto 或其他环境的解释器。
- 开始工作或运行验证前，先执行 conda run -n zy314 python --version 和 conda run -n zy314 which python，确认 Python 3.14 和 zy314 路径。
- 领域服务不依赖 FastAPI 请求对象；Vue 3 不在 Local Storage 保存凭据或邮件正文。
- 结构性 Python 修改后立即执行 python -m compileall -q app。
- 测试必须使用隔离 PostgreSQL 和隔离 IMAP 测试服务，不得连接真实生产邮箱或真实 App Password。
- 测试报告区分离线单元、隔离基础设施和真实 IMAP 验证，不把模拟结果写成真实邮箱通过。

## 变更范围和文件删除

- 遵守用户提供的全局文件删除安全规则。
- 禁止批量删除、递归删除、通配符删除或清空目录。
- 删除文件时只能指定一个明确的绝对路径，并先说明原因。
- 未经用户明确授权，不修改或删除与当前任务无关的文件。
- 当前文档阶段只新增或修改项目文档、配置说明和测试设计文档。

## 提交前检查

- README.md、docs/ 和本文件不存在互相矛盾的接口、状态、字段或部署步骤。
- 官方事实带来源链接和核对日期；本项目设计和未验证项明确标记。
- 文档中没有真实凭据、危险删除命令、邮件正文、附件或原始 MIME 样例。
- 文档、配置样例和测试设计不再引用 Gmail API、Watch、Google Cloud Pub/Sub、Redis、OAuth 或业务 Webhook 作为当前方案。
