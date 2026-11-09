from __future__ import annotations

import pytest
from app.config import Settings
from pydantic import ValidationError


def test_settings_load_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("HEALTH_CHECK_TIMEOUT_SECONDS", "1.5")

    settings = Settings(_env_file=None)

    assert settings.app_env == "test"
    assert settings.log_level == "DEBUG"
    assert settings.health_check_timeout_seconds == 1.5


def test_development_settings_load_defaults_without_secrets() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_env == "development"
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.redis_url.startswith("redis://")


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_level="verbose")


def test_production_requires_external_secrets() -> None:
    with pytest.raises(ValidationError, match="production configuration missing"):
        Settings(_env_file=None, app_env="production")


def test_production_accepts_explicit_configuration() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        google_client_id="CLIENT_ID",
        google_client_secret="SECRET_REF",
        google_redirect_uri="https://example.test/oauth/google/callback",
        google_refresh_token_key="SECRET_REF",
        oauth_admin_token="SECRET_REF",
        pubsub_project_id="PROJECT_ID",
        pubsub_subscription="projects/PROJECT_ID/subscriptions/SUBSCRIPTION",
        pubsub_oidc_audience="https://example.test/webhooks/pubsub/gmail",
        pubsub_oidc_service_account="service-account@example.iam.gserviceaccount.com",
        business_webhook_url="https://business.example.test/webhook",
        business_webhook_secret="SECRET_REF",
    )

    assert settings.app_env == "production"


def test_stage5_webhook_names_are_loaded_and_business_names_remain_compatible() -> None:
    settings = Settings(
        _env_file=None,
        webhook_url="https://business.example.test/hook",
        webhook_secret="SECRET_REF",
        webhook_max_retries=7,
        webhook_enabled=True,
    )
    assert settings.webhook_url == "https://business.example.test/hook"
    assert settings.webhook_secret is not None
    assert settings.webhook_enabled is True
    assert settings.webhook_max_retries == 7
