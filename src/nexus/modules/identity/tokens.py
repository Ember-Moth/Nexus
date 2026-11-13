import hashlib
import re

from .errors import unauthorized


def digest_token(token: str, prefix: str) -> str:
    if not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{43}", token):
        raise unauthorized()
    return hashlib.sha256(token.encode()).hexdigest()
