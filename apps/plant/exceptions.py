from apps.core.exceptions import BusinessRuleViolation


class MachineNotReady(BusinessRuleViolation):
    """PRD 2.2: the machine's daily PM / safety zero-point checks are not done."""


class GaugeNotUsable(BusinessRuleViolation):
    """PRD 2.3: the gauge is unknown, inactive, or its calibration has expired."""


class PMCheckError(BusinessRuleViolation):
    """A PM check could not be recorded."""
