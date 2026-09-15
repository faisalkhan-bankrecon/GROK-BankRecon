"""Supabase JWT authentication for FastAPI — supports both legacy HS256
and the newer asymmetric ES256/RS256 signing keys."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from jwt import PyJWKClient

_bearer = HTTPBearer(auto_error=False)

_SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
_JWKS_URL = f"{_SUPABASE_URL}/auth/v1/.well-known/jwks.json" if _SUPABASE_URL else None
_jwks_client: Optional[PyJWKClient] = PyJWKClient(_JWKS_URL) if _JWKS_URL else None


@dataclass
class AuthUser:
    id: str
    email: Optional[str] = None


def _jwt_secret() -> str:
    secret = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
    if not secret:
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: SUPABASE_JWT_SECRET is not set",
        )
    return secret


def verify_token(token: str) -> AuthUser:
    try:
        header = jwt.get_unverified_header(token)
        alg = header.get("alg", "HS256")

        if alg == "HS256":
            payload = jwt.decode(
                token,
                _jwt_secret(),
                algorithms=["HS256"],
                audience="authenticated",
            )
        elif alg in ("ES256", "RS256"):
            if _jwks_client is None:
                raise HTTPException(
                    status_code=500,
                    detail="Server misconfigured: SUPABASE_URL is not set (needed for JWKS)",
                )
            signing_key = _jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=[alg],
                audience="authenticated",
            )
        else:
            raise HTTPException(
                status_code=401,
                detail=f"Unsupported token algorithm: {alg}",
            )
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {e}",
        ) from e

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing subject")
    email = payload.get("email")
    return AuthUser(id=str(user_id), email=email)


async def require_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> AuthUser:
    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return verify_token(creds.credentials)


async def optional_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[AuthUser]:
    if creds is None or not creds.credentials:
        return None
    try:
        return verify_token(creds.credentials)
    except HTTPException:
        return None
