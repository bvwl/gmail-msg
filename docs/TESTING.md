# 测试设计

当前文档定义实现阶段的测试计划。所有测试必须使用隔离 PostgreSQL 和专用 IMAP 测试服务器或本地协议仿真；不得连接真实邮箱、生产数据库或真实 App Password。测试输出不得包含邮件正文、原始 MIME、附件、密码、Session 或验证码原文。

## 1. 测试边界

- **单元测试**：解析、验证码提取、游标计算、退避、Cookie 属性和清理截止时间。
- **协议集成测试**：隔离 IMAP 服务器验证 TLS、LIST、SELECT、UID FETCH、IDLE、NOOP、EXPUNGE、断线和重连。
- **数据库集成测试**：真实隔离 PostgreSQL 验证唯一约束、事务回滚、全文搜索、Session 和 30 天清理。
- **API 契约测试**：FastAPI 路由、认证、分页、错误码和正文访问控制。
- **Vue 组件/E2E 测试**：登录、列表、搜索、分页、详情、HTML 清理、验证码快速查看和状态展示；当前隔离验收使用本机 FastAPI + Vite + PostgreSQL，通过真实浏览器执行，生产域名和 HTTPS 仍未验证。
- **前后端分离测试**：独立构建 Vue 3，使用配置的 API 基础地址和 CORS 白名单；验证前端不能直连 PostgreSQL、IMAP 或 SOCKS5。

## 2. IMAP 场景

至少覆盖：

1. 首次 baseline 不导入历史邮件为新邮件。
2. Inbox 新邮件通过 IDLE 事件后被 UID FETCH 并落库。
3. Spam 和其他监控文件夹同步，标签关系正确。
4. 同一 Gmail Message ID 同时出现在 Inbox 与 Spam，正文只保存一份。
5. IDLE 期间网络断开，恢复后按 last_seen_uid 补查遗漏 UID。
6. Worker 在数据库提交前崩溃，重启后重试且不重复正文。
7. Worker 在数据库提交后进程退出，重放只更新关系/游标，不重复实体。
8. UIDVALIDITY 变化被识别并暂停，不错误复用旧游标。
9. EXPUNGE 只移除文件夹关系，邮件实体保留到 30 天清理。
10. IMAP 认证失败、TLS 证书失败、超时和服务器 BYE 使用有限退避并进入正确状态。
11. IDLE 超时后 NOOP/重入流程不会丢失 UID。
12. 大批量 UID 分页处理不会开启长事务或跳过未处理 UID。

## 3. 代理与连接场景

至少覆盖：

1. socks5 代理可用，代理检查成功后建立 IMAP TLS 并进入 idle。
2. socks5 代理不可用，邮箱进入 waiting_for_proxy 或 proxy_unavailable，不发生 Gmail 直连。
3. 代理认证失败，不向 Gmail 发送用户名、密码或 App Password，按退避重试代理检查。
4. 代理恢复后重新建立 IMAP TLS，并根据 UID/UIDVALIDITY 补查遗漏邮件。
5. socks5 模式明确禁止直连回退；删除或屏蔽系统代理不能改变结论。
6. direct 模式不使用代理环境变量，直连失败只重试 direct。
7. IMAP TLS 建立失败，状态为 connecting/reconnecting 或 error，proxy_mode 不被自动修改。
8. 多邮箱并行时一个邮箱代理失败不阻塞其他邮箱的代理检查和同步。

## 4. 数据与保留期场景

- 邮件唯一键冲突、Message-ID 缺失和回退键冲突。
- 同一标签重复投递、标签移入/移出和多文件夹关系。
- text/plain、HTML、中文标题、空正文和编码错误。
- 含附件邮件只保存 has_attachments=true，不保存附件或原始 MIME。
- 验证码单个、多候选、过期、误报和无法提取。
- 恰好 30 天、超过 30 天、未来时间和清理任务重复执行。
- 清理中断、部分批次提交和 PostgreSQL 断连后的重试。

## 5. API 与安全场景

- 未登录访问所有管理接口返回 401。
- 登录成功 Cookie 具备 HttpOnly、Secure、SameSite 和过期时间；登出后 Session 立即失效。
- 错误密码不泄露用户名存在性；登录尝试限流。
- 邮箱配置不返回 App Password；正文、验证码和 IMAP 原始响应不出现在错误响应。
- 搜索参数化、防 SQL 注入、分页上限、CSRF、HTML 清理和 SSRF 边界。
- 日志扫描不得命中正文、密码、Cookie、Session、附件、原始 MIME 或验证码值。
- CORS 只允许配置的前端来源；带 Cookie 的跨域请求不得匹配通配符来源。
- 生产 Compose 配置必须通过 `docker compose --env-file .env.production -f compose.prod.yaml config --quiet`。
- 邮件 HTML allowlist、IMAP 文件夹控制字符、IMAP 主机 allowlist、正文截断和生产配置 fail-closed 必须有回归测试。
- 未登录前端访问管理路由被拦截，API 返回 401 后前端回到登录页。
- 页面展示 proxy_mode、proxy_status、错误原因、最近同步时间、UID 和 UIDVALIDITY。

## 6. 性能与恢复

使用隔离测试数据测量单邮箱持续 IDLE、多邮箱并行、短时批量 UID、搜索分页和 30 天清理。记录 PostgreSQL 连接数、事务时长、批次大小、IDLE 重连次数和补查延迟；不把本地结果描述为真实 Gmail 生产容量。

恢复演练包括 PostgreSQL 重启、Worker 进程退出、网络分区、单邮箱认证失败和清理任务重启。验收重点是游标不越过未提交 UID、重复写入最终收敛、正文只保留一份、失败状态可见且可恢复。

## 7. 测试命令与证据规则

实现阶段所有 Python/pytest 命令使用 conda run -n zy314 ...，开始前确认 Python 3.14 和 zy314 路径。测试报告必须分别标记：离线单元、隔离 PostgreSQL/IMAP、真实 IMAP 测试；没有真实凭据时不得声称真实邮箱通过。

## 8. 旧测试迁移清单

当前默认入口只收集 IMAP、PostgreSQL 和管理员 API 的测试。旧测试文件没有删除，已在 `tests/conftest.py` 中逐文件列入收集忽略清单，防止废弃架构在默认命令中继续失败：

- Gmail API、OAuth、Gmail Watch、Pub/Sub：`test_gmail_client.py`、`test_oauth.py`、`test_gmail_watch.py`、`test_pubsub.py`、`test_gmail_sync.py`、`test_gmail_mime.py`、`test_gmail_quota.py`。
- Redis、Outbox、旧 Worker 和旧通知：`test_redis_streams.py`、`test_resources.py`、`test_worker.py`、`test_notifications.py` 以及 `tests/integration/` 下对应文件。
- 旧配置和旧健康检查契约：`test_config.py`、`test_health_and_lifespan.py`、`test_transactions.py`。

替代覆盖集中在 `tests/test_current_architecture.py` 和现有 `test_imap_architecture.py`、`test_main.py`、`test_entrypoint.py`、`test_project_setup.py`、`test_state_machine.py`。每个旧文件仍保留，后续若补齐隔离 PostgreSQL/IMAP 契约，应逐文件移除对应忽略项并恢复执行。

隔离服务使用 `compose.test.yaml`：PostgreSQL、GreenMail IMAP/IMAPS 和 SOCKS5 均只绑定本机测试端口。GreenMail 使用测试占位符和测试证书；当前 compose 为了验证连接流程启用 `greenmail.auth.disabled`，因此真实 IMAP 认证失败仍需单独的认证开启 fixture。端口被占用时通过 `TEST_POSTGRES_PORT`、`TEST_IMAP_PORT`、`TEST_IMAPS_PORT` 和 `TEST_SOCKS5_PORT` 指向新的本机端口；禁止把这些变量指向生产服务。

完整 `mypy app tests` 会逐文件排除仍保留的历史 Gmail API、OAuth、Redis、Pub/Sub、Outbox、Webhook 和旧 Worker 模块及其对应测试；排除列表位于 `pyproject.toml`，与本文件的旧测试清单逐项对应。当前 IMAP、PostgreSQL、FastAPI、管理员认证、同步、清理和当前测试路径必须保持零诊断，历史文件不删除，后续迁移时逐文件恢复检查。

