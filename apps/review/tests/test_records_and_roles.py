from django.contrib import admin as dj_admin
from django.contrib.auth.models import Permission
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.inspections.exceptions import InspectionLocked
from apps.inspections.models import InspectionStatus
from apps.review import services
from apps.review.exceptions import RecordLocked
from apps.review.models import (
    Concession, MRBDisposition, MRBSignoff, QuarantineRecord, ReasonCode, SupervisorDecision,
)

from .helpers import LATER, LONG_TEXT, ReviewTestCase, pdi_ctq_frozen, pdi_noctq_fail, pdi_pass


class ImmutabilityTests(ReviewTestCase):
    def test_decisions_and_concessions_are_permanent(self):
        insp, _ = pdi_noctq_fail(self)
        services.issue_concession(insp, self.auth_sup, ReasonCode.COSMETIC, LONG_TEXT, LATER)
        decision, concession = SupervisorDecision.objects.get(), Concession.objects.get()
        for obj in (decision, concession):
            with self.assertRaises(RecordLocked):
                obj.save()
            with self.assertRaises(RecordLocked):
                obj.delete()
        for model in (SupervisorDecision, Concession):
            with self.assertRaises(RecordLocked):
                model.objects.all().update(id=1)
            with self.assertRaises(RecordLocked):
                model.objects.all().delete()

    def test_decided_inspection_can_never_move_again(self):
        insp, _ = pdi_pass(self)
        services.approve_inspection(insp, self.supervisor, "", LATER)
        insp.refresh_from_db()
        for status in (InspectionStatus.SUBMITTED, InspectionStatus.CONCESSION, InspectionStatus.QUARANTINED,
                       InspectionStatus.IN_PROGRESS):
            insp.status = status
            with self.assertRaises(InspectionLocked):
                insp.save()

    def test_frozen_inspection_can_only_be_quarantined(self):
        insp, _ = pdi_ctq_frozen(self)
        for status in (InspectionStatus.APPROVED, InspectionStatus.CONCESSION, InspectionStatus.SUBMITTED,
                       InspectionStatus.IN_PROGRESS):
            insp.status = status
            with self.assertRaises(InspectionLocked):
                insp.save()
        insp.status = InspectionStatus.QUARANTINED
        insp.save()

    def test_quarantine_record_only_moves_open_to_closed(self):
        insp, _ = pdi_noctq_fail(self)
        record = services.quarantine_lot(insp, self.supervisor, "Reject the lot, roughness high", LATER)
        record = QuarantineRecord.objects.get(pk=record.pk)
        record.reason = "edited"
        with self.assertRaises(RecordLocked):
            record.save()
        with self.assertRaises(RecordLocked):
            record.delete()
        with self.assertRaises(RecordLocked):
            QuarantineRecord.objects.all().update(status="CLOSED")

    def test_inspection_cannot_skip_the_review_rules(self):
        # A SUBMITTED inspection may only become one of the three decided states.
        insp, _ = pdi_pass(self)
        insp.status = InspectionStatus.FROZEN
        with self.assertRaises(InspectionLocked):
            insp.save()

    def test_no_change_or_delete_permissions_exist(self):
        codes = set(Permission.objects.filter(content_type__app_label="review").values_list("codename", flat=True))
        for model in ("supervisordecision", "concession", "quarantinerecord", "mrbdisposition", "mrbsignoff"):
            self.assertIn(f"view_{model}", codes)
            for verb in ("add", "change", "delete"):
                self.assertNotIn(f"{verb}_{model}", codes)


class AdminAndRoleTests(ReviewTestCase):
    def test_review_records_are_read_only_even_for_superusers(self):
        root = make_user("root", Role.PLANT_MANAGER, is_staff=True, is_superuser=True)
        self.client.force_login(root)
        request = type("R", (), {"user": root})()
        for model in (SupervisorDecision, Concession, QuarantineRecord, MRBDisposition, MRBSignoff):
            ma = dj_admin.site._registry[model]
            self.assertFalse(ma.has_add_permission(request), model)
            self.assertFalse(ma.has_change_permission(request), model)
            self.assertFalse(ma.has_delete_permission(request), model)
            self.assertEqual(self.client.get(reverse(f"admin:review_{model._meta.model_name}_add")).status_code, 403)

    def test_role_matrix(self):
        expect = {
            "supervisor": {"review.approve_inspection": True, "review.quarantine_lot": True,
                           "review.issue_concession": True, "review.sign_disposition": False},
            "manager": {"review.issue_concession": True, "review.approve_inspection": False,
                        "review.quarantine_lot": False, "review.sign_disposition": False},
            "qa_head": {"review.quarantine_lot": True, "review.propose_disposition": True,
                        "review.sign_disposition": True, "review.approve_inspection": False,
                        "review.issue_concession": False},
            "plant_manager": {"review.sign_disposition": True, "review.propose_disposition": True,
                              "review.quarantine_lot": False, "review.approve_inspection": False},
            "lead": {"review.sign_disposition": True, "review.propose_disposition": True,
                     "review.issue_concession": False},
            "qa": {"review.view_supervisordecision": False, "review.approve_inspection": False,
                   "review.sign_disposition": False},
            "operator": {"review.view_supervisordecision": False},
            "dock": {"review.view_quarantinerecord": False},
            "engineer": {"review.approve_inspection": False},
        }
        for attr, perms in expect.items():
            for perm, allowed in perms.items():
                self.assertEqual(getattr(self, attr).has_perm(perm), allowed, f"{attr} {perm}")
