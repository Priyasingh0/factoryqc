from apps.core.exceptions import BusinessRuleViolation


class HandoverError(BusinessRuleViolation):
    """Base for shift-handover rule violations (PRD 12.2)."""


class NotAuthorized(HandoverError):
    """The user's role may not do this handover step."""


class HandoverIncomplete(HandoverError):
    """The handover form is missing something the PRD makes mandatory."""


class InvalidTransition(HandoverError):
    """The handover is not in a state where this step is allowed."""


class HandoverPending(HandoverError):
    """PRD 12.2: work on the machine is locked until the incoming worker acknowledges the handover."""


class RecordLocked(HandoverError):
    """Handover records are permanent."""
