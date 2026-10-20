from apps.core.exceptions import BusinessRuleViolation


class InspectionError(BusinessRuleViolation):
    """Base for inspection workflow errors."""


class NotAuthorized(InspectionError):
    """The user's role may not perform this inspection step (PRD 12)."""


class CannotStart(InspectionError):
    """The scans do not allow an inspection to be opened (PRD 5.1)."""


class InspectionLocked(InspectionError):
    """PRD 13.3: submitted inspection data cannot be altered or deleted."""


class InspectionFrozen(InspectionError):
    """PRD 4.3: a CTQ failure froze this checklist; only the Quality Head can act."""


class InvalidMeasurement(InspectionError):
    """The entered reading is not acceptable (wrong type, format or tool)."""


class InvalidPhoto(InspectionError):
    """The uploaded defect photo is not acceptable (PRD 5.4)."""


class SubmitBlocked(InspectionError):
    """The inspection is incomplete (PRD 5.2) or lacks defect photos (PRD 5.4)."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))
