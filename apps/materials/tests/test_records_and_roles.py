from decimal import Decimal

from django.contrib import admin as dj_admin
from django.contrib.auth.models import Permission

from apps.materials import services
from apps.materials.exceptions import RecordLocked
from apps.materials.models import (
    SNCR, GradeProperty, IntakeStatus, LabResult, MaterialIssue, MTCReading, RawMaterialIntake,
)

from .base import MaterialsTestCase
from .helpers import LATER


class ImmutabilityTests(MaterialsTestCase):
    def test_received_data_cannot_be_edited(self):
        i = RawMaterialIntake.objects.get(pk=self.receive().pk)
        for field, value in (("heat_number", "H-FAKE"), ("quantity", Decimal("1")), ("po_number", "X"),
                             ("supplier_id", self.supplier.pk + 99), ("mtc_sha256", "1" * 64)):
            setattr(i, field, value)
            with self.assertRaises(RecordLocked, msg=field):
                i.save()
            i.refresh_from_db()

    def test_status_may_only_follow_the_allowed_moves(self):
        i = RawMaterialIntake.objects.get(pk=self.receive().pk)
        i.status = IntakeStatus.SCRAPPED
        with self.assertRaises(RecordLocked):
            i.save()
        q = RawMaterialIntake.objects.get(pk=self.quarantined(heat="H-Q").pk)
        for bad in (IntakeStatus.CLEARED, IntakeStatus.RECEIVED, IntakeStatus.IN_LAB_TESTING):
            q.status = bad
            with self.assertRaises(RecordLocked, msg=bad):
                q.save()
        done = self.quarantined(heat="H-R")
        services.resolve_sncr(done.sncr, self.buyer, "RTV", "DN-1", LATER)
        done = RawMaterialIntake.objects.get(pk=done.pk)
        done.status = IntakeStatus.CLEARED
        with self.assertRaises(RecordLocked):
            done.save()

    def test_intakes_cannot_be_deleted_or_bulk_changed(self):
        i = self.receive()
        with self.assertRaises(RecordLocked):
            i.delete()
        with self.assertRaises(RecordLocked):
            RawMaterialIntake.objects.all().delete()
        with self.assertRaises(RecordLocked):
            RawMaterialIntake.objects.all().update(status="CLEARED")

    def test_sncr_core_fields_locked_and_closed_only_once(self):
        i = self.quarantined()
        sncr = SNCR.objects.get(pk=i.sncr.pk)
        sncr.details = "rewritten"
        with self.assertRaises(RecordLocked):
            sncr.save()
        sncr = SNCR.objects.get(pk=i.sncr.pk)
        sncr.status = SNCR.Status.OPEN
        with self.assertRaises(RecordLocked):
            sncr.save()
        with self.assertRaises(RecordLocked):
            sncr.delete()
        with self.assertRaises(RecordLocked):
            SNCR.objects.all().delete()
        services.resolve_sncr(i.sncr, self.buyer, "RTV", "DN-1", LATER)
        closed = SNCR.objects.get(pk=sncr.pk)
        closed.resolution_reference = "DN-2"
        with self.assertRaises(RecordLocked):
            closed.save()

    def test_readings_lab_results_and_issues_are_permanent(self):
        lot = self.in_lab()
        services.record_lab_result(lot, self.eng, passed=True, report_reference="LAB-1", now=LATER)
        reading = lot.readings.first()
        lab = LabResult.objects.get()
        for obj in (reading, lab):
            with self.assertRaises(RecordLocked):
                obj.save()
            with self.assertRaises(RecordLocked):
                obj.delete()
        for model in (MTCReading, LabResult, MaterialIssue):
            with self.assertRaises(RecordLocked):
                model.objects.all().delete()
            with self.assertRaises(RecordLocked):
                model.objects.all().update(id=1)

    def test_reading_must_belong_to_the_lots_grade(self):
        lot = self.receive()
        other = self.lab_grade.properties.first() or GradeProperty.objects.create(
            grade=self.lab_grade, name="Carbon", kind="CHEMICAL", min_value=Decimal("0.1"))
        with self.assertRaises(RecordLocked):
            MTCReading.objects.create(intake=lot, grade_property=other, value=Decimal("0.2"), entered_by=self.eng)

    def test_grade_property_needs_a_limit_and_ordered_limits(self):
        from django.core.exceptions import ValidationError
        for kw in ({}, {"min_value": Decimal("5"), "max_value": Decimal("1")}):
            prop = GradeProperty(grade=self.grade, name="X", kind="CHEMICAL", **kw)
            with self.assertRaises(ValidationError):
                prop.clean()

    def test_admin_views_are_read_only_except_master_data(self):
        for model in (RawMaterialIntake, MTCReading, LabResult, SNCR, MaterialIssue):
            ma = dj_admin.site._registry[model]
            self.assertFalse(ma.has_add_permission(None))
            self.assertFalse(ma.has_change_permission(None))
            self.assertFalse(ma.has_delete_permission(None))


class RolePermissionTests(MaterialsTestCase):
    def perms(self, user):
        return {p.split(".", 1)[1] for p in user.get_all_permissions() if p.startswith("materials.")}

    def test_dock_clerk(self):
        p = self.perms(self.dock)
        self.assertTrue({"create_intake", "reject_intake", "view_rawmaterialintake"} <= p)
        self.assertFalse({"review_mtc", "issue_material", "quarantine_lot", "resolve_sncr", "record_lab_result"} & p)

    def test_quality_roles(self):
        self.assertTrue({"review_mtc", "record_lab_result", "reject_intake"} <= self.perms(self.eng))
        self.assertTrue({"review_mtc", "quarantine_lot"} <= self.perms(self.qm))
        self.assertTrue({"quarantine_lot", "resolve_sncr"} <= self.perms(self.qh))
        self.assertFalse({"create_intake", "issue_material"} & (self.perms(self.eng) | self.perms(self.qm) | self.perms(self.qh)))

    def test_purchasing_and_production(self):
        self.assertIn("resolve_sncr", self.perms(self.buyer))
        self.assertIn("add_supplier", self.perms(self.buyer))
        self.assertFalse({"issue_material", "review_mtc", "create_intake"} & self.perms(self.buyer))
        self.assertIn("issue_material", self.perms(self.lead))
        self.assertFalse(self.perms(self.operator) | self.perms(self.sup))

    def test_no_default_write_permissions_on_records(self):
        codes = set(Permission.objects.filter(content_type__app_label="materials").values_list("codename", flat=True))
        for model in ("rawmaterialintake", "mtcreading", "labresult", "sncr", "materialissue"):
            self.assertFalse({f"{a}_{model}" for a in ("add", "change", "delete")} & codes, model)
