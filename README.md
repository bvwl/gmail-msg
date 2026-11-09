# IMAP 邮件监听与管理系统

本项目已进入实现阶段，当前架构为 **IMAP + PostgreSQL + FastAPI + Vue 3**。此前基于 Gmail API、Gmail Watch、Google Cloud Pub/Sub、Redis 和业务 Webhook 的方案全部废弃，旧验收记录不再代表当前设计。

系统为每个邮箱运行一个 IMAP IDLE Worker，持续监听收件箱、垃圾邮件和配置要求的其他 Gmail 文件夹。每个邮箱可选择 direct 或 SOCKS5 代理，代理只用于该邮箱 Worker 的 IMAP 出站连接。IMAP 推送只负责发现变化；Worker 断线重连后根据 PostgreSQL 中的 UID、UIDVALIDITY 和同步游标补查遗漏邮件。邮件正文和元数据保存 30 天，附件和原始 MIME 永不保存。邮件实体只保存一份，Inbox、Spam 等 Gmail 标签通过关系表表达。

## 目标范围

- 支持多个 Gmail 邮箱，每个邮箱独立配置 IMAP 连接和监控文件夹。
- 使用 PostgreSQL 作为唯一持久化基础设施，保存邮箱配置、IMAP 状态、邮件、标签、验证码、管理员 Session 和清理记录。
- FastAPI 提供管理员登录、邮箱与连接状态、邮件查询、正文查看、验证码查询、同步状态和清理状态接口。
- Vue 3 作为独立应用，通过 FastAPI REST API 提供登录、邮箱管理、收件箱与垃圾邮件列表、搜索分页、详情、验证码快速查看、代理状态和同步错误状态页面。
- 所有邮件内容和管理接口均需管理员登录；管理员密码使用 Argon2id 或 bcrypt 哈希，Session 使用 HttpOnly、Secure、SameSite Cookie。

## 核心架构

```text
Gmail IMAP folders
        │ IMAP IDLE + UID FETCH/SEARCH
        ▼
per-mailbox IMAP IDLE Worker
        │ PostgreSQL transaction
        ▼
PostgreSQL（唯一状态源）
        ▲
        │ FastAPI REST API + CORS + Cookie Session
        ▼
Vue 3 administrator UI
```

IMAP IDLE 断开、超时、认证失败或服务器重启时，Worker 记录错误并按退避重连。恢复后按保存的 UID 游标、UIDVALIDITY 和 UID SEARCH/FETCH 补查，只有完整处理并提交事务后才推进游标。同一邮箱同时只允许一个 Worker 写入该邮箱的同步状态。

## 文档索引

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：组件、IDLE 生命周期、断线补查、幂等和保留策略。
- [docs/DATABASE.md](docs/DATABASE.md)：PostgreSQL 表、字段、约束、索引和事务边界。
- [docs/API.md](docs/API.md)：FastAPI 管理接口、认证 Cookie、分页和错误模型。
- [docs/SECURITY.md](docs/SECURITY.md)：管理员认证、IMAP 凭据、日志和数据保护。
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)：单 PostgreSQL 部署拓扑、环境变量和运维要求。
- [docs/TASKS.md](docs/TASKS.md)：实现拆分、依赖关系和验收条件。
- [docs/TESTING.md](docs/TESTING.md)：单元、IMAP 集成、故障恢复、安全和保留期测试设计。
- [docs/INTEGRATION_REPORT.md](docs/INTEGRATION_REPORT.md)：隔离验收结果、浏览器联调证据和真实外部集成未验证项。

## 推荐运行单元

生产环境使用同一代码版本启动：

1. FastAPI 进程：管理员登录、邮箱管理、邮件查询和健康检查。
2. 每邮箱一个 IMAP IDLE Worker：连接、IDLE、断线重连、UID 补查和入库。
3. PostgreSQL 清理任务：定期删除超过 30 天的邮件、正文和标签关系，并记录清理结果。

Worker 可以与 FastAPI 进程分离，也可以在同一主机以独立进程运行。Vue 3 单独开发、构建和部署，只能访问 FastAPI REST API；不能直连 PostgreSQL、IMAP 或 SOCKS5。所有后端进程只连接同一个 PostgreSQL 数据库；不使用 Redis 或其他队列、缓存、消息总线。

## 配置原则

所有环境差异通过环境变量或 Secret Manager 注入。App Password 只能通过环境变量或 Secret Manager 提供，不写入 Git、镜像、日志或测试样例。邮件正文、原始 MIME、附件、密码、Session Cookie 和验证码原文不得写入日志。

Vue 3 与 FastAPI 分开开发、构建和部署；每个环境分别配置前端来源、API 基础地址和 FastAPI CORS 白名单。前端只能通过 REST API 访问数据，不能直连 PostgreSQL、IMAP 或 SOCKS5。

管理员登录账号可通过 `.env` 的 `ADMIN_USERNAME`、`ADMIN_PASSWORD` 初始化或同步；FastAPI `/docs`、`/redoc`、`/openapi.json` 使用独立的 `DOCS_USERNAME`、`DOCS_PASSWORD` Basic Auth。外部程序使用 `EXTERNAL_API_KEY`，通过 `X-API-Key` 或 `Authorization: Bearer` 调用管理 API。密码和 API Key 不放入前端 `VITE_*` 变量，因为这类变量会被打包并暴露给浏览器。

默认首次同步模式为 `baseline`：首次连接只记录当前 UID 上限，不把历史邮件全部标记为新邮件。之后的同步由 UID 游标驱动；UIDVALIDITY 变化时暂停该文件夹并进入受控重建流程，避免把旧 UID 当成新 UID。

## 本地环境与验证边界

项目 Python 环境约定为 Conda `zy314`，实现和验证命令统一使用任何实现阶段的 Python、pytest、Alembic 或服务命令都必须使用 `conda run -n zy314 ...`，并先确认 Python 3.14 与解释器路径。

当前报告已记录本机隔离 PostgreSQL、Worker 恢复路径和 Vue + FastAPI 浏览器联调结果；仍不宣称真实 Gmail、生产 PostgreSQL、生产 SOCKS5、生产 TLS 或公网业务 API 已验证。

业务运行依赖已切换到 IMAP、PostgreSQL、FastAPI 和 Vue 3；本地 .env 中的 Secret 仍需按部署环境注入。

## 生产 Compose

生产环境使用 `compose.prod.yaml`：PostgreSQL、迁移、FastAPI、IMAP Worker、清理任务和前端 Nginx 由同一 Compose 项目管理。PostgreSQL 只加入内部网络，不映射宿主机端口；前端只绑定宿主机回环端口，公网 HTTPS 需要由宿主机反向代理或云负载均衡提供。

```bash
cp .env.production.example .env.production
docker compose --env-file .env.production -f compose.prod.yaml up -d --build
```

生产配置必须使用 `CORS_ORIGINS`，不能使用通配符；管理员 Cookie 强制 `Secure` 和 `SameSite=Strict`。详细步骤见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 旧方案处理

Gmail API、OAuth、Watch、Google Cloud Pub/Sub、Redis Streams、业务 Webhook、Outbox、附件存储和 90 天保留期均属于废弃设计。它们不得再出现在实现计划、配置样例、测试场景或运行说明中。
