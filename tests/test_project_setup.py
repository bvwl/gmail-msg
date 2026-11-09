from __future__ import annotations

import sys

import app


def test_supported_python_version() -> None:
    assert sys.version_info[:2] == (3, 14)


def test_package_version() -> None:
    assert app.__version__ == "0.1.0"
