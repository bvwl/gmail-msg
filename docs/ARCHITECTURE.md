# 系统架构设计

## 1. 设计结论

系统采用 **每邮箱一个 IMAP IDLE Worker + 可选 SOCKS5 出站代理 + PostgreSQL 唯一状态源 + FastAPI REST API + 独立 Vue 3 前端** 的架构。

- IMAP IDLE 用于发现文件夹变化；邮件内容通过 UID FETCH 获取。
- 每个邮箱至少监控 Inbox、Spam 和配置的其他 Gmail 文件夹。Gmail 的 All Mail、Sent 等文件夹只有在配置启用时才同步。
- PostgreSQL 保存邮箱、文件夹、UIDVALIDITY、UID 游标、邮件实体、标签关系、同步状态、验证码、管理员 Session 和清理记录。
- 每个邮箱独立保存 proxy_mode、代理地址、Secret 引用、DNS 模式和代理状态；代理只作用于该邮箱 IMAP Worker 的出站连接。
- Redis、消息队列、缓存服务、Gmail API、Watch、Pub/Sub、OAuth 和业务 Webhook 不在当前系统中。
- 邮件正文和元数据保留 30 天；附件与原始 MIME 不保存。清理任务按提交时间和数据库时间统一使用 UTC。

系统提供至少一次同步语义。IMAP 断线或进程崩溃后允许重复 FETCH，但数据库唯一约束和标签关系幂等写入保证最终只有一份邮件正文。

## 2. 组件

### FastAPI REST API

负责管理员登录/登出、Cookie Session、邮箱配置和连接状态、邮件列表/搜索/分页、正文查看、验证码查询、同步状态和清理状态。所有管理路由都要求有效管理员 Session；健康检查只暴露不含邮件内容的进程与数据库状态。FastAPI 设置 CORS 白名单，只允许配置的 Vue 3 来源访问。

FastAPI、Vue 3、管理员登录和 PostgreSQL 使用服务器自己的网络配置，不通过邮箱代理。

### 每邮箱代理连接层

每个邮箱独立执行代理检查和 IMAP 连接，不因一个邮箱代理失败而阻塞其他邮箱。proxy_mode 只有 direct 和 socks5：

- direct：只允许直连 IMAP，不读取系统代理，不尝试 SOCKS5。
- socks5：先检查 SOCKS5 代理可用性，再通过代理建立 IMAP TLS；代理不可用时禁止直连回退，也不得发送 Gmail 用户名、密码或 App Password。
- 代理检查失败进入 WAITING_FOR_PROXY 或 PROXY_UNAVAILABLE，使用截断指数退避和随机抖动重试；恢复后才进入 connecting。

代理检查、IMAP TLS 握手和 IMAP 认证是分开的状态。代理恢复前只连接代理端点，不连接 Gmail IMAP。

### 每邮箱 IMAP IDLE Worker

每个启用邮箱只有一个活动 Worker。Worker 为每个目标文件夹维护独立连接或串行选择文件夹，执行以下循环：

1. 使用 TLS 连接 IMAP 服务器并登录。
2. SELECT 目标文件夹，读取 UIDVALIDITY、UIDNEXT 和当前状态。
3. 从 PostgreSQL 读取该文件夹同步游标。
4. 先用 UID SEARCH/FETCH 补齐游标之后已存在的邮件，再进入 IDLE。
5. 收到 EXISTS、EXPUNGE、邮件标记或 IDLE 超时后退出 IDLE，重新读取 UID 范围和 FLAGS/labels。
6. 以事务写入邮件实体、正文和标签关系，提交后更新游标。
7. 连接异常时记录脱敏错误，按截断指数退避重连。

Gmail IMAP 特有文件夹名称必须通过 LIST 与 SPECIAL-USE 属性发现，不能硬编码本地化名称。Spam 通常映射为 Gmail 的垃圾邮件文件夹；实际名称以服务器返回的 folder name 为准。

### 清理任务

清理任务定期扫描 stored_at 早于当前 UTC 时间减 30 天的邮件，按批次删除正文、邮件实体和标签关系，并写入 retention_runs。删除前后不输出正文、主题或验证码。清理失败保留错误状态，下一轮继续。

### 独立 Vue 3 前端

Vue 3 作为独立应用开发、构建和部署，只通过 FastAPI REST API 获取数据，不能直接连接 PostgreSQL、IMAP 或 SOCKS5。前端未登录时由路由守卫阻止管理页面访问；不在 Local Storage、URL 或构建产物中保存 Session、Gmail App Password、代理密码或其他 Secret。页面包括登录、邮箱管理、Inbox、Spam、全部邮件、搜索/分页、邮件详情、验证码快速查看、同步状态和清理状态。

## 3. 数据流与游标

### 首次连接

1. 管理员创建邮箱记录并指定 IMAP 主机、端口、用户名、Secret 引用、proxy_mode、代理参数和监控文件夹。
2. direct 邮箱执行直连检查；socks5 邮箱先执行独立 SOCKS5 可用性检查。
3. 代理可用后，Worker 通过选定网络路径建立 IMAP TLS，并读取每个文件夹的 UIDVALIDITY、UIDNEXT。
4. baseline 模式将当前最大 UID 保存为基线，不把历史邮件标记为新邮件；如果产品选择导入历史，则按明确的导入上限执行。
5. Worker 进入 IDLE，并在下一次变化后从游标继续同步。

### 变化处理

1. IDLE 返回事件或周期性保活触发一次增量检查。
2. Worker 读取 last_seen_uid + 1 到 UIDNEXT - 1 的 UID，并使用 UID FETCH 获取 RFC 822 headers、text/plain 或 text/html 正文和 Gmail IMAP 扩展字段。
3. 解析结果只保留规范化元数据、完整正文和标签；附件 part 只记录“存在附件”的布尔信息，不保存附件名、二进制或原始 MIME。
4. 以稳定邮件键插入 messages；同一封邮件在多个文件夹出现时复用该行，并向 message_labels 添加 Inbox、Spam 等关系。
5. 所有 UID 处理成功、邮件写入和标签关系写入完成后，在同一 PostgreSQL 事务中推进文件夹游标。

### 断线补查

断线恢复时必须先退出旧 IDLE、重新 SELECT 并重新读取 UIDVALIDITY。若 UIDVALIDITY 未变化，则补查游标之后的 UID；若 UIDVALIDITY 变化，暂停该文件夹并标记 uidvalidity_changed，由管理员确认后执行受控重建，避免误把旧 UID 视为新邮件。IMAP 的 EXPUNGE 只影响文件夹关系；已保存的邮件实体继续保留到 30 天清理。

## 4. 邮件去重与标签

稳定邮件键优先使用 Gmail IMAP 的 X-GM-MSGID；若服务器未提供，则使用规范化 Message-ID，并在缺失时结合发件人、内部日期、主题和正文摘要形成受控回退键。回退键必须记录来源并允许人工处理冲突。

messages 只保存一份正文。message_labels 记录 (message_id, mailbox_folder_id)，因此同一邮件可同时显示在 Inbox、Spam、All Mail 或其他监控文件夹。查询 Inbox/Spam 时通过标签关系过滤，不复制正文。

## 5. 状态和并发

邮箱代理/连接状态至少包含 disabled、direct、proxy_checking、waiting_for_proxy、connecting、idle、reconnecting、auth_failed、error、stopped。proxy_unavailable、proxy_auth_failed、imap_tls_failed 和 imap_auth_failed 作为脱敏错误分类保存。文件夹同步状态至少包含 uidvalidity、uidnext、last_seen_uid、last_success_at、last_error_code、last_error_at 和 worker_heartbeat_at。

同一邮箱的 Worker 通过 PostgreSQL advisory lock 或数据库租约互斥；锁只保护同步状态写入，不能替代事务。FastAPI 只读同步状态，不直接推进游标。

## 6. 故障处理

- IDLE 超时：发送 NOOP 或退出并重新进入 IDLE，不丢弃游标。
- socks5 代理不可用：保持 waiting_for_proxy 或 proxy_unavailable，按退避重试；绝不直连。
- socks5 代理认证失败：停止向 Gmail 发送认证信息，等待代理 Secret 修复。
- direct 连接失败：只重试 direct，不读取系统代理，也不切换 socks5。
- IMAP TLS 握手失败：记录 imap_tls_failed，按当前网络模式重连，不改变 proxy_mode。
- 网络断开：记录分类错误，退避重连，恢复后执行 UID 补查。
- 认证失败：停止重试，状态置为 auth_error，等待管理员更新 Secret。
- 单封邮件解析失败：记录该 UID 的可重试错误，不能推进越过未处理 UID；异常信息不得包含正文。
- PostgreSQL 暂时不可用：不确认同步成功，不推进游标；数据库恢复后重新补查。
- 进程崩溃：事务回滚，下一次 Worker 根据旧游标重试，唯一约束收敛重复写入。
- 清理任务失败：保留 failed 状态和脱敏错误代码，下一轮继续，不删除部分批次之外的数据。

## 7. 可靠性边界

本设计保证数据库内的唯一约束、事务游标和标签关系的一致性；不宣称 IMAP 服务器、网络或第三方邮箱永不丢失事件。IDLE 是低延迟触发机制，UID 补查是可靠性保障。外部 IMAP 行为、Gmail 标签映射和容量指标必须在实现阶段用隔离测试账号验证。

## 8. 参考资料（核对日期：2026-11-06）

- IMAP4rev1：RFC 3501，https://www.rfc-editor.org/info/rfc3501/
- IMAP IDLE：RFC 2177，https://www.rfc-editor.org/info/rfc2177/
- Gmail IMAP 扩展（X-GM-MSGID、X-GM-LABELS、Special-Use 说明）：https://developers.google.com/workspace/gmail/imap/imap-extensions
- Gmail IMAP/SSL 连接说明：https://developers.google.com/workspace/gmail/imap/imap-smtp

