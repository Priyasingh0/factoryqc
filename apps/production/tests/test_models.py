from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.production.models import JobTraveler, RoutingOperation
from apps.production import services
from apps.production.exceptions import RecordLockedError

from .helpers import ProductionTestCase, make_ready_part, make_work_order, op_at, released_traveler
from apps.checklists.tests.helpers import make_part


class ModelRuleTests(ProductionTestCase):
    def test_duplicate_op_number_rejected(self):
        part = make_part("P-DUP")
        RoutingOperation.objects.create(part=part, op_number=10, name="A")
        with self.assertRaises(IntegrityError), transaction.atomic():
            RoutingOperation.objects.create(part=part, op_number=10, name="B")

    def test_final_pdi_cannot_have_hold_or_first_piece(self):
        part = make_part("P-PDI")
        for extra in ({"hold_minutes": 5}, {"requires_first_piece": True}, {"inspection_interval_minutes": 30}):
            step = RoutingOperation(part=part, op_number=99, name="PDI", is_final_pdi=True, **extra)
            with self.assertRaises(ValidationError):
                step.full_clean()

    def test_only_one_original_traveler_per_work_order(self):
        traveler, _ = released_traveler(self)
        with self.assertRaises(IntegrityError), transaction.atomic():
            JobTraveler.objects.create(work_order=traveler.work_order, quantity=1)

    def test_rework_traveler_allowed_alongside_original(self):
        traveler, _ = released_traveler(self)
        rework = JobTraveler.objects.create(
            work_order=traveler.work_order, quantity=1, is_rework=True, parent_traveler=traveler)
        self.assertTrue(rework.pk)

    def test_quantity_must_be_positive(self):
        wo = make_work_order(self.lead, make_part("P-Q"), quantity=0)
        with self.assertRaises(ValidationError):
            wo.full_clean()

    def test_barcode_is_signed_and_unique_per_traveler(self):
        traveler, _ = released_traveler(self)
        self.assertNotEqual(traveler.barcode, str(traveler.pk))  # signed, not a bare id
        from apps.core.exceptions import InvalidQRToken
        with self.assertRaises(InvalidQRToken):  # any edit to the signed text is rejected
            services.resolve_traveler_from_barcode(traveler.barcode[:-2] + "xx")
        self.assertEqual(services.resolve_traveler_from_barcode(" " + traveler.barcode + " "), traveler)

    def test_no_change_or_delete_permissions_exist_for_shopfloor_models(self):
        from django.contrib.auth.models import Permission
        codes = set(Permission.objects.filter(content_type__app_label="production").values_list("codename", flat=True))
        for model in ("jobtraveler", "traveleroperation", "productionrun", "partserial"):
            self.assertIn(f"view_{model}", codes)
            for verb in ("add", "change", "delete"):
                self.assertNotIn(f"{verb}_{model}", codes)


class RolePermissionTests(ProductionTestCase):
    def test_role_matrix(self):
        expect = {
            "lead": {"production.release_workorder": True, "production.operate_traveleroperation": False,
                     "production.clear_first_piece": False},
            "operator": {"production.operate_traveleroperation": True, "production.release_workorder": False,
                         "production.clear_first_piece": False, "production.log_periodic_check": False},
            "qa": {"production.clear_first_piece": True, "production.log_periodic_check": True,
                   "production.operate_traveleroperation": False, "production.release_workorder": False},
            "plant_manager": {"production.release_workorder": True, "production.clear_first_piece": False},
            "manager": {"production.view_workorder": True, "production.release_workorder": False},
            "engineer": {"production.view_jobtraveler": True, "production.add_workorder": False},
            "dock": {"production.view_workorder": False, "production.view_jobtraveler": False},
        }
        for attr, perms in expect.items():
            user = getattr(self, attr)
            for perm, allowed in perms.items():
                self.assertEqual(user.has_perm(perm), allowed, f"{attr} {perm}")
