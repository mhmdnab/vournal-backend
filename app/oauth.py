"""Provider integration contract. No client token is trusted before verification.

Future adapter: authorization code + PKCE + one-time state/nonce -> server-side
issuer/audience/signature/expiry/nonce verification -> identity by (issuer, sub).
Never link accounts by unverified email. Existing-account linking requires a
recent authenticated MFA session. Credentials are deliberately absent here.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.enhancements import require_enabled

router = APIRouter(prefix="/auth/oauth", tags=["auth"])


@dataclass(frozen=True)
class VerifiedIdentity:
    issuer: str
    subject: str
    email: str | None
    email_verified: bool


class AuthorizationCode(BaseModel):
    code: str = Field(min_length=1, max_length=4096)
    codeVerifier: str = Field(min_length=43, max_length=128)
    state: str = Field(min_length=32, max_length=256)


class ProviderVerifier(Protocol):
    async def exchange(
        self, request: AuthorizationCode, *, expected_nonce: str
    ) -> VerifiedIdentity:
        """Verify server-owned redirect URI, single-use state, PKCE and ID token.

        Verify signature with provider JWKS and fixed algorithm/issuer/audience,
        expiry, nonce, and (when required) email_verified before returning.
        Never accept a decoded-but-unverified JWT or trust client email claims.
        """
        ...


@router.get("/providers")
async def providers():
    require_enabled()
    return {
        name: {
            "enabled": False,
            "reason": "Credentials and server verification pending",
        }
        for name in ("apple", "google")
    }


@router.post("/{provider}/exchange")
async def exchange(provider: Literal["apple", "google"], body: AuthorizationCode):
    require_enabled()
    # Explicit closed boundary until a real ProviderVerifier is configured.
    # Provider login will still flow through Vournal's verification/MFA policy.
    raise HTTPException(503, "This sign-in provider is not configured yet.")
