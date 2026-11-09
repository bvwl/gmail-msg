from __future__ import annotations

import main


def test_entrypoint_has_fixed_local_server_configuration() -> None:
    assert main.APP_MODULE == "app.main:app"
    assert main.HOST == "127.0.0.1"
    assert main.PORT == 8000
    assert main.RELOAD is True
