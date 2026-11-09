"""当前 IMAP 架构的默认测试入口。

这些文件保留在仓库中，作为迁移线索和历史证据，但其测试对象属于已废弃的
Gmail API、OAuth、Pub/Sub、Redis 或业务 Webhook 架构。逐文件列出，避免使用
通配符把未确认的测试静默排除；替代覆盖位于当前架构测试文件中。
"""

collect_ignore = [
    "test_config.py",
    "test_gmail_client.py",
    "test_gmail_mime.py",
    "test_gmail_quota.py",
    "test_gmail_sync.py",
    "test_gmail_watch.py",
    "test_health_and_lifespan.py",
    "test_notifications.py",
    "test_oauth.py",
    "test_pubsub.py",
    "test_redis_streams.py",
    "test_resources.py",
    "test_transactions.py",
    "test_worker.py",
    "integration/test_local_pipeline.py",
    "integration/test_outbox_fencing.py",
    "integration/test_outbox_http_real.py",
    "integration/test_performance.py",
    "integration/test_postgres_advisory_lock.py",
    "integration/test_postgres_fault_recovery.py",
    "integration/test_postgres_schema_and_outbox.py",
    "integration/test_redis_streams_real.py",
    "integration/test_scheduler_recovery.py",
    "integration/test_worker_process_crash.py",
]
