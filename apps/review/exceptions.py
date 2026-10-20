from apps.core.exceptions import BusinessRuleViolation


class ReviewError(BusinessRuleViolation):
    """Base for review / MRB rule violations."""


class NotAuthorized(ReviewError):
    """The user may not take this decision (PRD 6.1, 12)."""


class DecisionBlocked(ReviewError):
    """The inspection is not in a state where this decision is allowed."""


class CtqConcessionBlocked(DecisionBlocked):
    """PRD 4.3: a Critical-to-Quality failure can never be released on concession."""


class InvalidDisposition(ReviewError):
    """The MRB disposition is missing required details (PRD 6.3)."""


class SignoffError(ReviewError):
    """The MRB sign-off is not allowed (wrong role, duplicate...)."""


class RecordLocked(ReviewError):
    """Decisions, concessions and MRB records are permanent."""
