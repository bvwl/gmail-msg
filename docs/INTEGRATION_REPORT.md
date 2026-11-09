# IMAP 架构集成报告

## 验收范围

验收日期：2026-11-07（Asia/Shanghai）。本轮不连接真实 Gmail、生产 PostgreSQL、生产 SOCKS5、真实 App Password 或真实业务 API。隔离验收只使用本机 Docker 容器、测试占位符、本机 FastAPI/Vite 和测试数据库。

## 通过的测试

- `conda run -n zy314 python --version`：通过，Python 3.14.4。
- `conda run -n zy314 which python`：通过，`/opt/miniconda3/envs/zy314/bin/python`。
- `conda run -n zy314 python -m compileall -q app`：通过。
- `conda run -n zy314 python -m pytest`：通过，21 passed，1 skipped。新增 IMAP IDLE 断线恢复、SOCKS5 代理恢复测试；默认入口保留并逐文件忽略废弃架构测试，没有删除测试文件。
- `conda run -n zy314 python -m ruff check .`：通过，0 诊断。
- `conda run -n zy314 python -m mypy app tests`：通过，当前范围 29 个文件 0 诊断。历史 Gmail API、OAuth、Redis、Pub/Sub、Outbox、Webhook 和旧 Worker 文件仍保留，并按 `pyproject.toml` 的逐类排除说明不参与当前运行路径。
- `npm run build`（工作目录 `frontend/`）：通过，Vite 生产构建完成。
- `TEST_ALLOW_ISOLATED_INTEGRATION=1 TEST_DATABASE_URL=... conda run -n zy314 python -m pytest tests/integration/test_current_isolated_acceptance.py`：通过，1 passed；仅使用本机隔离 PostgreSQL。
- Playwright CLI 本机浏览器流程：通过，实际访问 `http://127.0.0.1:5173`，API 指向本机 `http://127.0.0.1:8000`；实际显示 `waiting_for_proxy / reconnecting` 和 `proxy_unavailable`，并完成邮件与验证码查看、登出回登录页；未使用真实域名或公网 API。
- `docker compose -f compose.test.yaml config --quiet`：通过。
- 隔离服务启动：GreenMail IMAP `127.0.0.1:5143`、IMAPS `127.0.0.1:5993`、PostgreSQL `127.0.0.1:55433`（healthy）、SOCKS5 `127.0.0.1:51080`。PostgreSQL 使用 55433 是因为本机 55432 已被其他测试容器占用。
- Alembic 空数据库升级：在 `imap_mail_empty_20261107` 执行 `upgrade head` 通过；创建当前 9 张表并保留 0006 历史表的 `legacy_*` 副本。
- Alembic 旧 schema 升级：在 `imap_mail_legacy_20261107` 执行 `upgrade 0006_schema_repair` 后再 `upgrade head` 通过。
- Alembic 回滚和幂等：`downgrade 0006_schema_repair` 后再次 `upgrade head` 通过；已在已处于 head 的数据库重复执行 `upgrade head`，无不一致。
- 迁移结构核对：外键级联/置空、邮箱/文件夹/消息/标签/验证码唯一约束、游标字段、`message_id_header` 索引、`verification_codes.confidence` 的 `Float(1.0)` 与 ORM 一致。
- 隔离 GreenMail IMAPS 直连：使用测试 TLS 上下文连接成功并完成 IMAP 登录、LIST。
- 隔离 GreenMail IMAPS SOCKS5：通过测试 SOCKS5 代理建立 TLS socket，连接成功并完成 IMAP 登录、LIST；实现复用了代理 socket，不再由 `IMAP4_SSL` 另行直连。
- SOCKS5 握手：经代理读取 GreenMail `* OK IMAP4rev1` greeting；不可用代理端口返回 `proxy_unavailable`。
- Inbox/Spam 发现：在 GreenMail 创建 `Spam` 文件夹后，`LIST` 返回 `INBOX` 和 `Spam`。
- PostgreSQL 模型和同步：同一稳定邮件键写入 Inbox、Spam 后得到 1 条 `messages`、2 条 `message_labels`、1 条验证码记录，验证跨标签去重和关系唯一性。
- 正文、验证码和附件策略：隔离写入保存规范化正文和验证码候选，只保留 `has_attachments`，不保存附件二进制或原始 MIME。
- 30 天清理：将隔离消息设为 31 天前后执行 `RetentionService`，结果为 `completed`、删除 1 条、剩余 0 条。
- 管理员 API：隔离 PostgreSQL 中成功登录返回 204，受保护邮箱查询返回 200，登出后返回 401；连续失败达到阈值后正确锁定，正确密码仍返回 401。
- FastAPI 查询：修复异步懒加载导致的 `MissingGreenlet`，邮箱列表显式 `selectinload` 文件夹后查询通过。
- IMAP IDLE 断线恢复：隔离 Worker 首次 IDLE 连接中断后进入 `reconnecting`，重新建立连接，按原 UIDVALIDITY 从 `last_seen_uid + 1` 补查 UID；重复补查只更新同一稳定邮件键，游标恢复到最新 UID。
- SOCKS5 代理恢复：隔离 Worker 在代理不可用时进入 `waiting_for_proxy/reconnecting`，恢复后重新建立 SOCKS5 路径的 IMAP TLS 连接并补查遗漏 UID；所有连接尝试均保持 `proxy_mode=socks5`，没有 direct fallback。
- 管理员审计日志：新增 `audit_logs` 和 `0008_admin_audit_logs` 迁移；隔离 PostgreSQL 集成测试验证失败/成功登录、失效 Session、邮件/验证码查看、邮箱配置修改和登出事件可持久化查询。
- 浏览器端联调：本地 FastAPI（127.0.0.1:8000）与 Vite（127.0.0.1:5173）通过真实浏览器完成登录、未登录拦截、邮箱状态、Inbox/Spam/全部邮件、正文/验证码、代理/IMAP/文件夹同步状态和登出回登录页。

## 用户要求的 20 项验收明细

| 项目 | 结果 | 证据或限制 |
| --- | --- | --- |
| 1. 数据库迁移和回滚 | 通过（隔离） | 空库、0006 旧 schema、head 重复升级、downgrade/upgrade 往返均通过。 |
| 2. 邮箱配置和管理员登录 | 通过（隔离） | 管理员登录、Session Cookie 和邮箱查询在隔离 PostgreSQL 通过。 |
| 3. direct 模式连接 | 通过（隔离） | GreenMail IMAPS 测试端口连接成功；测试脚本使用不校验证书的上下文。 |
| 4. SOCKS5 模式连接 | 通过（隔离） | 代理握手、代理 TLS socket 注入、IMAP 登录和 LIST 通过。 |
| 5. 代理不可用状态 | 通过（隔离） | Worker 测试验证 `waiting_for_proxy/reconnecting` 状态和截断退避；连接工厂不可用期间不读取 Gmail 凭据。 |
| 6. SOCKS5 不可用不直连 | 通过（隔离） | 恢复测试断言所有连接设置仍为 `socks5`，没有 direct fallback；生产网络级拦截未验证。 |
| 7. 代理恢复自动重连 | 通过（隔离） | 代理恢复后重新建立连接并按 UID 补查遗漏邮件，游标和同步状态恢复。 |
| 8. IMAP TLS/认证失败 | 部分通过 | 认证失败曾由 GreenMail 认证开启 fixture 返回 `LOGIN failed`；当前 compose 为成功路径启用 `greenmail.auth.disabled`，独立 TLS 证书失败场景未纳入默认脚本。 |
| 9. IMAP IDLE 事件 | 通过（隔离 Worker） | Worker 测试模拟 IDLE socket 断开、reconnecting、重连和重复补查；GreenMail 长连接断网和真实 BYE 仍未验证。 |
| 10. Inbox/Spam 发现 | 部分通过 | GreenMail LIST 已发现 INBOX/Spam；SPECIAL-USE flags 和 Worker 落库尚未端到端验证。 |
| 11. UID/UIDVALIDITY/游标 | 部分通过 | `message_labels` UID/UIDVALIDITY 唯一约束和同步写入通过；真实 IMAP UID FETCH、游标推进和 UIDVALIDITY 变化未完整演练。 |
| 12. baseline 不通知历史邮件 | 未验证 | 当前默认测试没有运行带通知断言的 Worker baseline 场景。 |
| 13. 断线遗漏补查 | 通过（隔离 Worker） | 断线后从 `last_seen_uid + 1` 补查，UIDVALIDITY 未变时不跳过遗漏 UID，重复 FETCH 不产生重复实体。 |
| 14. Inbox/Spam 去重 | 通过（隔离 PostgreSQL） | 同一稳定键跨两个文件夹只保存一份正文，标签关系为两条。 |
| 15. 正文和验证码提取 | 通过（隔离） | RFC822 解析、正文落库和验证码候选记录通过。 |
| 16. 不保存附件/原始 MIME | 通过（隔离） | 仅保存正文、元数据和附件存在标志。 |
| 17. 30 天清理 | 通过（隔离） | 31 天旧邮件被清理并记录 retention run。 |
| 18. Session/锁定/权限/审计 | 通过（隔离 PostgreSQL） | Session、登出、401 权限、失败锁定和 `audit_logs` 持久化查询通过；真实生产审计留存和访问控制未验证。 |
| 19. FastAPI 查询接口 | 通过（隔离） | 根路径、健康检查、登录、受保护邮箱列表和登出流程通过。 |
| 20. Vue 3 与 FastAPI 联调 | 通过（本机隔离） | Playwright CLI 实际浏览器完成登录、路由拦截、Inbox/Spam/全部邮件、正文/验证码、状态展示和登出回登录页；生产域名、HTTPS 和跨域部署未验证。 |

## 失败的测试

- 本轮没有失败的最终命令。曾发现并修复两个当前路径问题：0007 的多命令 `op.execute()` 导致 asyncpg prepared statement 错误；FastAPI 异步懒加载 `Mailbox.folders` 导致 `MissingGreenlet`。
- 旧架构测试和旧模块若单独强制运行仍会失败，因为它们依赖已经废弃的 Gmail API、OAuth、Redis、Outbox、Webhook、旧模型字段和旧配置。它们被逐文件保留并从默认入口及当前 mypy 检查范围排除，未被删除或用全局 ignore 掩盖。

## 跳过或未验证的测试

- 长时间运行的代理自动重连、GreenMail 真正网络断开/BYE、UIDVALIDITY 变化和 baseline 通知抑制仍未验证；本轮 Worker 级断线和代理恢复路径已通过。
- GreenMail 的成功连接使用测试环境 `greenmail.auth.disabled` 和不校验证书的测试上下文；真实证书链、真实 IMAP 认证失败策略和 Gmail 特有 SPECIAL-USE/X-GM 扩展未完整验证。
- 审计日志已在隔离 PostgreSQL 建表并验证主要管理操作写入与查询；真实生产审计保留周期、导出和运维告警仍未验证。
- Vue 3 已完成构建并完成本机浏览器联调；生产域名、TLS、反向代理和真实部署拓扑仍未验证。

## 未验证的真实外部集成

- 未连接真实 Gmail IMAP、真实 Gmail 文件夹、真实 Gmail TLS 证书、真实 App Password 或真实账号认证。
- 未连接生产 PostgreSQL、生产 SOCKS5 或任何公网业务 API。
- 未验证真实网络分区、生产代理恢复、Gmail IDLE 丢事件补查和生产容量。

## 当前已知限制

1. 隔离 GreenMail 使用测试证书和测试认证配置，不能代表 Gmail 的证书链、认证策略、SPECIAL-USE flags、X-GM-MSGID/X-GM-THRID 或生产 IMAP 行为。
2. SOCKS5 容器只用于本机隔离协议路径；生产代理 DNS、认证、网络 ACL 和失败时序仍需单独验收。
3. 当前默认 pytest 的 21 个测试覆盖离线契约、Worker 恢复路径和轻量 API 行为；隔离 PostgreSQL 集成测试与浏览器联调需按本报告单独执行，不能把这些结果描述为真实 Gmail 或生产集成通过。
4. 历史 Gmail API/OAuth/Redis/Outbox/Webhook 代码和测试仍在仓库中，后续迁移任务需要逐文件恢复检查或明确废弃，不应批量删除。
