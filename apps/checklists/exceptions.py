from apps.core.exceptions import BusinessRuleViolation


class TemplateLockedError(BusinessRuleViolation):
    """PRD 12 / 4.1: a template can only be edited while it is a Draft."""


class InvalidTransition(BusinessRuleViolation):
    """The requested workflow step is not allowed from the template's current status."""


class TemplateValidationError(BusinessRuleViolation):
    """The template is not complete enough to be sent for approval."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class NotAuthorized(BusinessRuleViolation):
    """The user may not perform this workflow step (PRD 12 permissions)."""


class TemplateNotAvailable(BusinessRuleViolation):
    """No Released template exists for the part / checklist type (PRD 4.1)."""
