import os
import secrets

from fastapi import Security, HTTPException, status
from fastapi.security import APIKeyHeader
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

# API key auth for privileged pack write operations (create / update / delete).
# Callers send the secret in the `X-API-Key` request header. The expected value
# is read from the PACKS_API_KEY environment variable at request time, so the key
# can be rotated by updating the env without touching code, and a missing config
# fails closed (every protected call is refused) rather than silently open.
API_KEY_HEADER_NAME = "X-API-Key"
_api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


def require_api_key(provided_key: str | None = Security(_api_key_header)) -> None:
    """FastAPI dependency that rejects the request unless a valid API key is
    supplied in the `X-API-Key` header.

    Fails closed: if PACKS_API_KEY isn't configured on the server, every protected
    request is refused (503) instead of being left open. The comparison is
    constant-time to avoid leaking the key through response timing."""
    expected_key = os.getenv("PACKS_API_KEY")
    if not expected_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Server API key is not configured",
        )
    if not provided_key or not secrets.compare_digest(provided_key, expected_key):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or missing API key",
        )