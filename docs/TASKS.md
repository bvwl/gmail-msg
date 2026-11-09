# 实现任务与验收条件

实现已开始。以下勾选项表示代码已落地；真实 IMAP、SOCKS5 和生产 PostgreSQL 仍需隔离环境验收。

## 1. 配置与基础设施

- [x] 移除旧 Gmail API、OAuth、Watch、Pub/Sub、Redis 和业务 Webhook 配置。
- [x] 增加单 PostgreSQL、管理员 Session、IMAP IDLE、保留期和验证码配置。
- [x] 增加每邮箱 proxy_mode、proxy_host、proxy_port、proxy_username、proxy_password_secret_ref、proxy_dns_mode 和 CORS/前端地址配置。
- [ ] 明确 Secret Manager 或环境变量的 App Password 注入方式。
- [ ] 验收：配置校验拒绝缺失数据库、Cookie 安全配置和 IMAP 必需项；样例不含真实凭据。

## 2. PostgreSQL 模型

- [x] 设计并实现 admin_users、admin_sessions、mailboxes、mailbox_folders、messages、message_labels、verification_codes、retention_runs。
- [x] 为稳定邮件键、文件夹 UID、UIDVALIDITY、Session 哈希和时间字段建立约束与索引。
- [x] 完成 0007 迁移的可执行拆分、幂等升级和 downgrade/upgrade 往返；空库和 0006 旧 schema 隔离验收通过。
- [x] 隔离验收：同一邮件跨 Inbox/Spam 只有一条正文，标签关系和验证码候选可幂等写入。
- [ ] 继续补充同一 UID 重放和游标事务回滚的长流程集成样例。

## 3. IMAP IDLE Worker

- [ ] 实现 TLS 登录、LIST/SPECIAL-USE 文件夹发现、SELECT、UID FETCH、UID SEARCH、IDLE、NOOP 和退出流程。
- [ ] 每邮箱唯一 Worker；记录邮箱和文件夹心跳、连接状态、UIDVALIDITY、UIDNEXT 和 last_seen_uid。
- [ ] 首次 baseline、断线重连、IDLE 超时、UID 补查、EXPUNGE 和 UIDVALIDITY 变化处理。
- [ ] 实现 direct/SOCKS5 独立代理检查、代理状态机、禁止模式回退和按邮箱隔离退避。
- [ ] 验收：断线期间新增邮件在恢复后被补查；同一邮件多标签只保存一次正文；认证失败不会无限重试。

## 4. 邮件解析与保留

- [x] 只保存规范化元数据、完整 text/plain 和清理后的 HTML 正文。
- [x] 记录 has_attachments 布尔值，不保存附件和原始 MIME。
- [x] 提取验证码候选并关联邮件；解析失败不阻塞其他 UID 的可恢复处理。
- [x] 实现 30 天批量清理、运行记录和失败重试。
- [ ] 验收：清理边界按 UTC 正确；删除后 API 不可查询正文和验证码；日志和异常不含正文。

## 5. FastAPI 与 Vue 3

- [x] 管理员登录/登出、Cookie Session、邮箱列表和连接状态。
- [x] Inbox、Spam、全部邮件查询，搜索、分页、正文详情和验证码查询。
- [x] 同步状态、错误状态和 30 天清理状态。
- [x] Vue 3 登录、邮箱管理、列表、详情、验证码快速查看和状态页。
- [x] Vue 3 独立构建和部署，只通过 FastAPI REST API 访问数据；实现 CORS 白名单、路由守卫和 Cookie Session。
- [ ] 页面展示代理模式、代理状态、错误原因、最近同步时间、UID、UIDVALIDITY、Inbox、Spam、全部邮件、正文和验证码。
- [ ] 验收：未登录所有管理接口返回 401；Cookie 具备 HttpOnly/Secure/SameSite；前端不保存凭据。

## 6. 测试与发布前检查

- [x] 使用隔离 IMAP/IMAPS、GreenMail、SOCKS5 和 PostgreSQL，不使用真实邮箱凭据；迁移、模型、直连和代理连接已完成一轮隔离验收。
- [x] 完成 Worker 级 IMAP IDLE 断线恢复、SOCKS5 代理恢复和 UID 补查隔离测试；补充审计日志集成测试并完成本机 FastAPI + Vite 浏览器联调。GreenMail 长时间断网、UIDVALIDITY 变化和生产环境仍待验证。
- [x] 执行 SOCKS5 可用/不可用、禁止直连回退、direct 和 IMAPS 测试连接；代理恢复、认证开启 fixture 和 TLS 失败仍待补充。
- [ ] 检查 README、docs、配置样例和测试设计没有旧 Gmail API/Redis/Pub/Sub 描述。
- [x] 在 `docs/INTEGRATION_REPORT.md` 区分“离线测试”“隔离基础设施测试”和“真实外部集成未验证”的边界。

