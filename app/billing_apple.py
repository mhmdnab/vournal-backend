"""Apple In-App Purchase for vournal Plus (build 5).

The app tags each purchase with the user's appAccountToken, sends the StoreKit 2
signed transaction (JWS) to POST /billing/apple/transactions, and finishes the
transaction only after this accepts it. Apple then keeps us in step through App
Store Server Notifications V2 (POST /billing/apple/notifications): renewals,
grace periods, expiry, refunds and revocations.

Every signed payload is verified against Apple's root certificate (app/certs)
with Apple's own library. Production is tried first, then Sandbox — App Review
buys in the sandbox against the production backend. The Xcode environment (local
StoreKit testing, unsigned) is accepted only when APPLE_IAP_ALLOW_XCODE=1, which
only the local dev runner sets.

All entitlement writes go through app.entitlements.grant_plus / revoke_plus.
"""

from __future__ import annotations

import logging
import os
import pathlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import (
    SignedDataVerifier,
    VerificationException,
    VerificationStatus,
)
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import _user_out, get_current_user
from app.db import db
from app.entitlements import account_token_for, grant_plus, revoke_plus
from app.schemas import UserOut

log = logging.getLogger("vournal.billing.apple")

PRODUCT_ID = os.environ.get("APPLE_PLUS_PRODUCT_ID", "app.vournal.mobile.plus.monthly")
BUNDLE_ID = os.environ.get("APPLE_BUNDLE_ID", "app.vournal.mobile")
APP_APPLE_ID = int(os.environ.get("APPLE_APP_APPLE_ID", "6818284333"))
_CERTS = pathlib.Path(__file__).parent / "certs"

# Notification types that (re)grant access until the transaction's expiry, and
# the ones that end Apple's grant now.
_GRANTS = {"SUBSCRIBED", "DID_RENEW", "OFFER_REDEEMED", "RENEWAL_EXTENDED", "REFUND_REVERSED"}
_REVOKES = {"EXPIRED", "GRACE_PERIOD_EXPIRED", "REFUND", "REVOKE"}


class InvalidTransaction(Exception):
    """A signed payload that failed verification in every allowed environment."""


@dataclass
class AppleNotification:
    type: str
    subtype: Optional[str]
    transaction: Any  # JWSTransactionDecodedPayload, or None (e.g. TEST)
    grace_expires_ms: Optional[int]


def environments() -> list[Environment]:
    allowed = [Environment.PRODUCTION, Environment.SANDBOX]
    if os.environ.get("APPLE_IAP_ALLOW_XCODE") == "1":
        # First: an Xcode-signed transaction fails Production on its signature
        # (not its environment), which would stop the search. Real App Store
        # data fails the Xcode check on environment and falls through.
        allowed.insert(0, Environment.XCODE)
    return allowed


def verifier_for(environment: Environment) -> SignedDataVerifier:
    roots = [path.read_bytes() for path in sorted(_CERTS.glob("*.cer"))]
    return SignedDataVerifier(
        roots,
        True,  # online revocation checks of the signing chain
        environment,
        BUNDLE_ID,
        APP_APPLE_ID if environment == Environment.PRODUCTION else None,
    )


def _first_valid(decode) -> Any:
    """Try each allowed environment; only a wrong-environment failure moves on."""
    last: Exception | None = None
    for environment in environments():
        try:
            return decode(verifier_for(environment))
        except VerificationException as exc:
            last = exc
            if exc.status != VerificationStatus.INVALID_ENVIRONMENT:
                break
        except Exception as exc:  # malformed input from the library's parser
            last = exc
            break
    raise InvalidTransaction(str(last))


def decode_transaction(jws: str):
    return _first_valid(lambda v: v.verify_and_decode_signed_transaction(jws))


def decode_notification(signed_payload: str) -> AppleNotification:
    def decode(verifier: SignedDataVerifier) -> AppleNotification:
        body = verifier.verify_and_decode_notification(signed_payload)
        data = body.data
        transaction = grace = None
        if data is not None and data.signedTransactionInfo:
            transaction = verifier.verify_and_decode_signed_transaction(
                data.signedTransactionInfo
            )
        if data is not None and data.signedRenewalInfo:
            renewal = verifier.verify_and_decode_renewal_info(data.signedRenewalInfo)
            grace = renewal.gracePeriodExpiresDate
        return AppleNotification(
            type=body.rawNotificationType or "",
            subtype=body.rawSubtype,
            transaction=transaction,
            grace_expires_ms=grace,
        )

    return _first_valid(decode)


def _when(ms: Optional[int]) -> datetime:
    return datetime.fromtimestamp((ms or 0) / 1000, tz=timezone.utc)


async def _user_for(transaction) -> Any:
    """The account a transaction belongs to: its appAccountToken first, then the
    original transaction id we stored at purchase."""
    token = getattr(transaction, "appAccountToken", None)
    if token:
        user = await db.user.find_unique(where={"appAccountToken": token})
        if user is not None:
            return user
    original = getattr(transaction, "originalTransactionId", None)
    if original:
        return await db.user.find_unique(where={"appleOriginalTransactionId": original})
    return None


router = APIRouter(prefix="/billing/apple", tags=["billing"])


class AccountTokenOut(BaseModel):
    appAccountToken: str


class SignedTransactionIn(BaseModel):
    signedTransaction: str


class SignedNotificationIn(BaseModel):
    signedPayload: str


@router.get("/account-token", response_model=AccountTokenOut)
async def account_token(current=Depends(get_current_user)):
    return AccountTokenOut(appAccountToken=await account_token_for(db, current.id))


@router.post("/transactions", response_model=UserOut)
async def submit_transaction(body: SignedTransactionIn, current=Depends(get_current_user)):
    try:
        transaction = decode_transaction(body.signedTransaction)
    except InvalidTransaction:
        raise HTTPException(400, {"code": "INVALID_TRANSACTION"})
    if transaction.productId != PRODUCT_ID:
        raise HTTPException(422, {"code": "WRONG_PRODUCT"})

    owner = await _user_for(transaction)
    token = getattr(transaction, "appAccountToken", None)
    if (owner is not None and owner.id != current.id) or (token and owner is None):
        # Bought by another vournal account (shared Apple ID), or a token we never issued.
        log.warning("Apple transaction for another account rejected (user %s)", current.id)
        raise HTTPException(409, {"code": "TRANSACTION_BELONGS_TO_ANOTHER_ACCOUNT"})

    if getattr(transaction, "revocationDate", None):
        await revoke_plus(db, current.id, "apple")
    else:
        await grant_plus(
            db,
            current.id,
            _when(transaction.expiresDate),
            "apple",
            transaction.originalTransactionId,
        )
    log.info("Apple purchase verified for user %s", current.id)
    return _user_out(await db.user.find_unique(where={"id": current.id}))


@router.post("/notifications")
async def notifications(body: SignedNotificationIn):
    try:
        note = decode_notification(body.signedPayload)
    except InvalidTransaction:
        raise HTTPException(400, {"code": "INVALID_NOTIFICATION"})

    user = await _user_for(note.transaction) if note.transaction is not None else None
    if user is None:
        log.info("Apple notification %s for no known user", note.type)
        return {"ok": True}

    if note.type in _GRANTS and not getattr(note.transaction, "revocationDate", None):
        await grant_plus(
            db,
            user.id,
            _when(note.transaction.expiresDate),
            "apple",
            note.transaction.originalTransactionId,
        )
    elif note.type == "DID_FAIL_TO_RENEW" and note.subtype == "GRACE_PERIOD" and note.grace_expires_ms:
        await grant_plus(
            db, user.id, _when(note.grace_expires_ms), "apple",
            note.transaction.originalTransactionId,
        )
    elif note.type in _REVOKES:
        await revoke_plus(db, user.id, "apple")
    log.info("Apple notification %s handled for user %s", note.type, user.id)
    return {"ok": True}
