from apps.core.exceptions import BusinessRuleViolation


class ProductionError(BusinessRuleViolation):
    """Base class for production rule violations."""


class NotAuthorized(ProductionError):
    """The user's role may not perform this production step (PRD 12)."""


class ReleaseError(ProductionError):
    """The work order cannot be released yet."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class OperationBlocked(ProductionError):
    """An operation cannot start, count or finish (sequence, quarantine, state...)."""


class HoldActive(OperationBlocked):
    """PRD 8.2: a mandatory cooling / curing hold has not expired."""


class FirstPieceLocked(OperationBlocked):
    """PRD 9.1: the station is in Setup Mode until QA signs off the first piece."""


class RecordLockedError(ProductionError):
    """Released work orders and routings in use cannot be edited."""
