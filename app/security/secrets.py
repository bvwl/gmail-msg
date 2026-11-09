"""邮箱凭据的应用层加密。"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class MailboxCredentialCipher:
    """用服务级 Fernet 密钥加密邮箱密码，数据库只保存密文。"""

    def __init__(self, key: str) -> None:
        if not key.strip():
            raise ValueError("mailbox_credentials_fernet_key_not_configured")
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("mailbox_credentials_fernet_key_invalid") from exc

    def encrypt(self, value: str) -> str:
        if not value:
            raise ValueError("mailbox_credential_empty")
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise ValueError("mailbox_credential_decrypt_failed") from exc
