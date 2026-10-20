from apps.core.exceptions import BusinessRuleViolation


class MaterialsError(BusinessRuleViolation):
    """Base for raw-material rule violations."""


class NotAuthorized(MaterialsError):
    """The user's role may not do this materials step (PRD 12)."""


class IntakeRejected(MaterialsError):
    """The intake record cannot be saved (missing MTC, heat mismatch, bad quantity...) (PRD 3.2)."""


class InvalidTransition(MaterialsError):
    """The lot is not in a state where this step is allowed."""


class MaterialBlocked(MaterialsError):
    """PRD 3.3: quarantined, rejected or unreleased stock cannot be issued or consumed."""


class RecordLocked(MaterialsError):
    """Intake core data, readings, SNCRs and issues are permanent."""
