from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


class Argon2Passwords:
    def __init__(self) -> None:
        self._hasher = PasswordHasher()
        # A fixed, valid Argon2id hash permits equivalent work for an unknown login.
        self._dummy = (
            "$argon2id$v=19$m=65536,t=3,p=4$MIIRqgvgQbgj220jfp0MPA$"
            "YfwJSVjtjSU0zzV/P3S9nnQ/USre2wvJMjfCIjrTQbg"
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        try:
            valid = self._hasher.verify(password_hash or self._dummy, password)
            return bool(password_hash and valid)
        except VerificationError, InvalidHashError:
            return False
