from apps.core.exceptions import BusinessRuleViolation


class DispatchError(BusinessRuleViolation):
    """Base for dispatch rule violations."""


class NotAuthorized(DispatchError):
    """The user's role may not do this dispatch step (PRD 12)."""


class ShippingInterlock(DispatchError):
    """PRD 10.2: the lot is not cleared, so nothing can be issued or printed.

    `problems` lists every unfulfilled requirement, so the clerk sees the exact reason.
    """

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("Shipping interlock: " + "; ".join(self.problems))


class ManifestError(DispatchError):
    """The manifest details are missing or wrong (PO, vehicle, serials)."""


class DocumentError(DispatchError):
    """The gate pass, packing slip, invoice or label cannot be issued in this state."""
