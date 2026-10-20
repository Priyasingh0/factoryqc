class BusinessRuleViolation(Exception):
    """Raised by services when a PRD rule blocks an action.

    Views catch this and show the message to the user.
    """


class InvalidQRToken(BusinessRuleViolation):
    """A scanned QR/barcode token is forged, damaged or of the wrong kind."""


class ImmutableRecordError(Exception):
    """Raised on any attempt to modify or delete an append-only record."""
