from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from core import settings


class CredentialCipher:
    def __init__(self) -> None:
        if settings.SERVICEMIND_CREDENTIAL_KEY is None:
            raise RuntimeError("SERVICEMIND_CREDENTIAL_KEY is not configured")
        self._fernet = MultiFernet(
            [
                Fernet(key.get_secret_value().encode("ascii"))
                for key in [
                    settings.SERVICEMIND_CREDENTIAL_KEY,
                    *settings.SERVICEMIND_CREDENTIAL_PREVIOUS_KEYS,
                ]
            ]
        )

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc:
            raise RuntimeError("Unable to decrypt integration credentials") from exc

    def rotate(self, value: str) -> str:
        try:
            return self._fernet.rotate(value.encode("ascii")).decode("ascii")
        except (InvalidToken, UnicodeError) as exc:
            raise RuntimeError("Unable to rotate integration credentials") from exc
