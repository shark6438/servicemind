from cryptography.fernet import Fernet, InvalidToken

from core import settings


class CredentialCipher:
    def __init__(self) -> None:
        if settings.SERVICEMIND_CREDENTIAL_KEY is None:
            raise RuntimeError("SERVICEMIND_CREDENTIAL_KEY is not configured")
        self._fernet = Fernet(
            settings.SERVICEMIND_CREDENTIAL_KEY.get_secret_value().encode("ascii")
        )

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("Unable to decrypt integration credentials") from exc
