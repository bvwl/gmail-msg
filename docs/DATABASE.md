# PostgreSQL 数据设计

PostgreSQL 是唯一持久化数据库。当前实现已包含对应 SQLAlchemy 模型和 0007、0008 迁移；0008 增加管理员 `audit_logs`。所有时间字段使用带时区的 UTC 时间。

## 1. 表与关系

### admin_users

- id UUID 主键。
- username 唯一、非空。
- password_hash 非空，使用 Argon2id 或 bcrypt 生成。
- is_active、created_at、last_login_at。

### admin_sessions

- id UUID 主键。
- session_hash 唯一；只保存 Session ID 的哈希，不保存 Cookie 明文。
- admin_user_id 外键。
- created_at、expires_at、revoked_at、last_seen_at。
- 索引：session_hash、expires_at。

### mailboxes

- id UUID 主键。
- address 唯一；imap_host、imap_port、username。
- app_password_secret_ref：Secret Manager 或环境变量引用，不是密码。
- proxy_mode（direct 或 socks5）。
- proxy_host、proxy_port、proxy_username、proxy_password_secret_ref、proxy_dns_mode；密码字段只保存 Secret 引用。
- proxy_status（direct、proxy_checking、waiting_for_proxy、proxy_unavailable、connecting、idle、reconnecting、auth_failed、error）。
- connection_state、last_heartbeat_at、last_error_code、last_error_at。
- created_at、updated_at。

### mailbox_folders

- id UUID 主键。
- mailbox_id 外键。
- imap_name、folder_role（inbox、spam、other）、monitor_enabled。
- uidvalidity、uidnext、last_seen_uid、baseline_uid。
- sync_state、last_success_at、last_error_code、last_error_at、worker_heartbeat_at。
- 唯一约束：(mailbox_id, imap_name)。

### messages

邮件实体只保存一份正文：

- id UUID 主键。
- mailbox_id 外键。
- stable_message_key（优先 X-GM-MSGID，必要时使用受控回退键）；唯一约束为 (mailbox_id, stable_message_key)。
- message_id_header 可空且加索引，用于搜索和审计。
- thread_key 可空。
- from_address、to_addresses、cc_addresses、subject。
- internal_date、received_at、stored_at。
- text_body、html_body_sanitized；只保存规范化正文。
- has_attachments 布尔值；不保存附件名、大小、二进制或原始 MIME。
- body_hash、parser_state、parser_error_code。
- 全文搜索索引覆盖主题、地址和正文。

### message_labels

- message_id 外键。
- mailbox_folder_id 外键。
- folder_uid：该邮件在该 Gmail 文件夹中的 IMAP UID。
- uidvalidity_snapshot：写入该关系时对应的 UIDVALIDITY。
- first_seen_at、last_seen_at、is_present。
- 主键：(message_id, mailbox_folder_id)；唯一约束：(mailbox_folder_id, folder_uid, uidvalidity_snapshot)。
- 删除或移出文件夹只更新关系状态；不复制邮件正文。

### verification_codes

- id UUID 主键。
- message_id 外键。
- code_value_encrypted 或受限明文列（实现阶段必须根据威胁模型决定，默认加密）。
- code_type、extracted_at、expires_at、confidence。
- 同一邮件可有多个候选验证码，但必须可追溯到邮件 ID。

### retention_runs

- id UUID 主键。
- started_at、finished_at、cutoff_at。
- scanned_count、deleted_count、failed_count、status、error_code。

## 2. 关键约束

- (mailbox_id, messages.stable_message_key) 唯一，保证同一邮箱跨标签只存一份正文。
- proxy_mode=direct 时代理字段必须为空，proxy_mode=socks5 时 proxy_host、proxy_port、proxy_dns_mode 和代理 Secret 引用必须通过校验。
- proxy_mode 不得由错误恢复逻辑自动修改；代理不可用只能更新 proxy_status 和错误字段。
- message_labels 以复合主键和唯一 UID 约束保证幂等。
- mailbox_folders.last_seen_uid 只能在该事务内所有待处理 UID 成功入库后推进。
- uidvalidity 发生变化时不得自动复用旧游标；必须先标记状态并进入重建流程。
- 所有邮件查询必须通过管理员授权后的服务层访问。
- 30 天清理以 stored_at < now() - interval '30 days' 为核心条件，执行时间使用 UTC。

## 3. 事务边界

每批 UID 处理在一个短事务中完成：读取当前游标、插入/更新 messages、写入 message_labels、写入验证码候选、更新 parser 状态、推进 last_seen_uid。事务提交前 Worker 不报告成功。

管理员查询使用只读事务或普通短事务，不持有同步锁。清理任务按主键分页，单批提交，避免长事务；删除邮件时由外键级联删除标签和验证码关系。

## 4. PostgreSQL 内存使用

验证码短期查询缓存、管理员 Session 和同步状态都可以由 PostgreSQL 提供；如实现阶段使用进程内内存优化，必须接受进程重启丢失缓存，并以 PostgreSQL 为最终状态源。禁止引入 Redis。

