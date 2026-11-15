from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.main import LoginRequest, MailboxPatch, MailboxRequest, create_app


@pytest.mark.parametrize("model", [MailboxRequest, MailboxPatch])
@pytest.mark.parametrize(
    "value",
    [
        "abcd efgh ijkl mnop",
        " abcd  efgh ijkl mnop ",
        "abcd\tefgh\nijkl mnop",
        "abcd\u00a0efgh ijkl mnop",
        SecretStr("abcd efgh ijkl mnop"),
    ],
)
def test_app_password_normalizes_whitespace(model, value):
    kwargs = (
        {"address": "test@example.invalid", "username": "test@example.invalid"}
        if model is MailboxRequest
        else {}
    )
    payload = model(
        app_password=value, proxy_password="proxy password keep spaces", **kwargs
    )
    assert payload.app_password.get_secret_value() == "abcdefghijklmnop"
    assert payload.proxy_password.get_secret_value() == "proxy password keep spaces"


@pytest.mark.parametrize("model", [MailboxRequest, MailboxPatch])
@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_app_password_rejects_blank(model, value):
    kwargs = (
        {"address": "test@example.invalid", "username": "test@example.invalid"}
        if model is MailboxRequest
        else {}
    )
    with pytest.raises(ValidationError, match="APP_PASSWORD_REQUIRED"):
        model(app_password=value, **kwargs)


def test_omitted_patch_and_admin_password_are_unchanged():
    assert MailboxPatch().app_password is None
    assert MailboxPatch().model_dump(exclude_unset=True) == {}
    assert LoginRequest(username="admin", password="keep spaces").password == "keep spaces"


def test_application_registers_body_compatible_routes():
    app = create_app()
    for path, method in [
        ("/api/admin/login", "POST"),
        ("/api/admin/logout", "POST"),
        ("/api/mailboxes/{mailbox_id}", "DELETE"),
    ]:
        route = next(
            route for route in app.routes
            if getattr(route, "path", None) == path
            and method in getattr(route, "methods", set())
        )
        assert route.status_code == 200
