from django.contrib import admin as dj_admin
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.inspections.models import DefectPhoto, Inspection, Measurement

from .helpers import InspectionTestCase


class AdminTests(InspectionTestCase):
    def test_inspection_records_are_read_only_even_for_superusers(self):
        root = make_user("root", Role.PLANT_MANAGER, is_staff=True, is_superuser=True)
        self.client.force_login(root)
        request = type("R", (), {"user": root})()
        for model in (Inspection, Measurement, DefectPhoto):
            ma = dj_admin.site._registry[model]
            self.assertFalse(ma.has_add_permission(request), model)
            self.assertFalse(ma.has_change_permission(request), model)
            self.assertFalse(ma.has_delete_permission(request), model)
            self.assertEqual(self.client.get(reverse(f"admin:inspections_{model._meta.model_name}_add")).status_code, 403)


class RoleMatrixTests(InspectionTestCase):
    def test_role_matrix(self):
        conduct = "inspections.conduct_inspection"
        view = "inspections.view_inspection"
        expect = {
            "qa": {conduct: True, view: True},
            "qa2": {conduct: True},
            "supervisor": {conduct: False, view: True},
            "qa_head": {conduct: False, view: True},
            "manager": {conduct: False, view: True},
            "plant_manager": {conduct: False, view: True},
            "lead": {conduct: False, view: True},
            "engineer": {conduct: False, view: True},
            "operator": {conduct: False, view: False},
            "dock": {conduct: False, view: False},
        }
        for attr, perms in expect.items():
            for perm, allowed in perms.items():
                self.assertEqual(getattr(self, attr).has_perm(perm), allowed, f"{attr} {perm}")
