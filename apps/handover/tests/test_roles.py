from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user

from .base import HandoverTestCase

CREATE, SIGN, ACK = ("handover.create_shifthandover", "handover.sign_shifthandover",
                     "handover.acknowledge_shifthandover")
VIEW = "handover.view_shifthandover"


class RolePermissionTests(HandoverTestCase):
    def can(self, role, perm):
        return make_user(f"r_{role}_{perm.split('.')[1]}", role).has_perm(perm)

    def test_who_can_complete_countersign_and_acknowledge(self):
        expected = {
            Role.MACHINE_OPERATOR: (True, False, True),
            Role.SHIFT_SUPERVISOR: (True, True, True),
        }
        for role in Role:
            want = expected.get(role, (False, False, False))
            got = (self.can(role, CREATE), self.can(role, SIGN), self.can(role, ACK))
            self.assertEqual(got, want, role)

    def test_who_can_read_the_logbook(self):
        readers = {Role.PLANT_MANAGER, Role.QUALITY_MANAGER, Role.QUALITY_HEAD, Role.SHIFT_SUPERVISOR,
                   Role.QA_INSPECTOR, Role.PRODUCTION_LEAD, Role.MACHINE_OPERATOR, Role.MAINTENANCE_SUPERVISOR}
        for role in Role:
            self.assertEqual(self.can(role, VIEW), role in readers, role)

    def test_nobody_gets_add_change_or_delete_permissions(self):
        from django.contrib.auth.models import Permission

        extra = Permission.objects.filter(content_type__app_label="handover").exclude(
            codename__startswith="view_").exclude(codename__in=["create_shifthandover", "sign_shifthandover",
                                                                "acknowledge_shifthandover"])
        self.assertFalse(extra.exists(), list(extra))
