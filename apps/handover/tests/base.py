from datetime import timedelta

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.core.models import AuditLog
from apps.core.notifications import Notification  # noqa: F401  (re-exported for the tests)
from apps.handover import services
from apps.handover.models import MachineHealth, ToolWear
from apps.plant.services import record_pm_check
from apps.plant.tests.helpers import at
from apps.production import services as production
from apps.production.tests.helpers import (
    NOW, ProductionTestCase, op_at, released_traveler, sign_off,
)

HANDOVER_AT = NOW + timedelta(hours=3)        # 13:00, end of the morning shift
EVENING = at(2026, 3, 10, 15, 0)              # incoming shift
NEXT_MORNING = at(2026, 3, 11, 7, 0)
LONG = "Spindle vibration after the last tool change, please watch it."


class HandoverTestCase(ProductionTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.sup = make_user("sup1", Role.SHIFT_SUPERVISOR)
        cls.sup2 = make_user("sup2", Role.SHIFT_SUPERVISOR)
        cls.maint = make_user("maint", Role.MAINTENANCE_SUPERVISOR)
        cls.warehouse = make_user("wh", Role.WAREHOUSE_CLERK)

    # ---------------------------------------------------------------- scenarios
    def running_job(self, parts=2, quantity=5):
        """A job running on a machine since the morning, `parts` already counted."""
        traveler, machine = released_traveler(self, quantity=quantity)
        op = op_at(traveler, 10)
        production.start_operation(op, self.operator, machine, NOW)
        op.refresh_from_db()
        sign_off(op.run, self.qa, NOW)
        if parts:
            production.record_parts(op, self.operator, parts, NOW)
        return traveler, machine, op

    def ready_for_evening(self, machine):
        record_pm_check(machine, self.operator, completed=True, safety_zero_point_ok=True, now=EVENING)

    def hand_over(self, machine, user=None, now=HANDOVER_AT, **kw):
        args = dict(scrap_count=0, tool_wear=ToolWear.OK, machine_health=MachineHealth.GOOD,
                    job_status_notes="Op 10 running, two parts counted, no issues.")
        args.update(kw)
        return services.create_handover(machine, user or self.operator, now=now, **args)

    def signed(self, machine, **kw):
        handover = self.hand_over(machine, **kw)
        return services.sign_handover(handover, self.sup, now=HANDOVER_AT)

    def acknowledged(self, machine, **kw):
        handover = self.signed(machine, **kw)
        return services.acknowledge_handover(handover, self.operator2, now=EVENING)

    def audit(self, **filters):
        return AuditLog.objects.filter(**filters)
