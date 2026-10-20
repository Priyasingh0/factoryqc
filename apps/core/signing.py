"""Signed QR/barcode tokens (PRD 13.1 "Tamper-Proof QR Labels").

Station, traveler and shipment QR codes carry a token signed with SECRET_KEY.
A forged or edited code fails verification. This module only creates and
verifies the token text; rendering it as a QR image is a later concern.
"""

from django.core import signing

from .exceptions import InvalidQRToken

QR_SALT = "factoryqc.qr-token.v1"


class QRKind:
    MACHINE = "machine"
    TRAVELER = "traveler"
    SHIPMENT = "shipment"
    MATERIAL = "material"

    ALL = {MACHINE, TRAVELER, SHIPMENT, MATERIAL}


def make_token(kind, object_id):
    if kind not in QRKind.ALL:
        raise ValueError(f"Unknown QR kind: {kind}")
    return signing.dumps({"k": kind, "id": str(object_id)}, salt=QR_SALT, compress=True)


def read_token(token, expected_kind):
    """Return the object id inside a valid token, or raise InvalidQRToken."""
    try:
        payload = signing.loads(token, salt=QR_SALT)
    except signing.BadSignature as exc:
        raise InvalidQRToken("Invalid or tampered QR code.") from exc
    if not isinstance(payload, dict) or payload.get("k") != expected_kind:
        raise InvalidQRToken("This QR code is not valid for this purpose.")
    return payload["id"]
