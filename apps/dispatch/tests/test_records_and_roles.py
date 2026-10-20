from django.contrib import admin as dj_admin
from django.contrib.auth.models import Permission

from apps.accounts.models import Role
from apps.dispatch.models import GatePass, ManifestSerial, ShipmentLabel, ShipmentManifest, ShippingDocument
from apps.dispatch import services
from apps.review.exceptions import RecordLocked

from .helpers import AFTER, DispatchTestCase, approved_lot
from .test_documents import KIND, manifest_for


class ImmutabilityTests(DispatchTestCase):
    def test_manifest_details_are_locked(self):
        m = manifest_for(self, approved_lot(self))
        m = ShipmentManifest.objects.get(pk=m.pk)
        for field, value in (("customer_po", "PO-X"), ("vehicle_number", "XX"), ("batch_size", 99)):
            setattr(m, field, value)
            with self.assertRaises(RecordLocked):
                m.save()
            m.refresh_from_db()

    def test_manifest_status_moves_only_open_to_dispatched(self):
        m = manifest_for(self, approved_lot(self))
        m = ShipmentManifest.objects.get(pk=m.pk)
        m.status = ShipmentManifest.Status.OPEN
        with self.assertRaises(RecordLocked):  # no change at all is not a move either
            m.save()
        services.issue_gate_pass(m, self.clerk, AFTER)
        m = ShipmentManifest.objects.get(pk=m.pk)
        m.status = ShipmentManifest.Status.OPEN
        with self.assertRaises(RecordLocked):
            m.save()

    def test_manifest_cannot_be_deleted_or_bulk_changed(self):
        m = manifest_for(self, approved_lot(self))
        with self.assertRaises(RecordLocked):
            m.delete()
        with self.assertRaises(RecordLocked):
            ShipmentManifest.objects.all().delete()
        with self.assertRaises(RecordLocked):
            ShipmentManifest.objects.all().update(vehicle_number="X")

    def test_issued_documents_are_permanent(self):
        m = manifest_for(self, approved_lot(self))
        gp = services.issue_gate_pass(m, self.clerk, AFTER)
        slip, _ = services.issue_document(m, KIND.PACKING_SLIP, self.clerk, AFTER)
        label = services.issue_label(m, self.clerk, AFTER)
        for obj in (gp, slip, label, m.items.first()):
            with self.assertRaises(RecordLocked):
                obj.save()
            with self.assertRaises(RecordLocked):
                obj.delete()
        for model in (GatePass, ShippingDocument, ShipmentLabel, ManifestSerial):
            with self.assertRaises(RecordLocked):
                model.objects.all().delete()
            with self.assertRaises(RecordLocked):
                model.objects.all().update(id=1)

    def test_admin_is_read_only(self):
        for model in (ShipmentManifest, ManifestSerial, GatePass, ShippingDocument, ShipmentLabel):
            ma = dj_admin.site._registry[model]
            self.assertFalse(ma.has_add_permission(None))
            self.assertFalse(ma.has_change_permission(None))
            self.assertFalse(ma.has_delete_permission(None))


class RolePermissionTests(DispatchTestCase):
    def perms(self, user):
        return {p for p in user.get_all_permissions() if p.startswith("dispatch.")}

    def test_only_the_warehouse_clerk_can_issue(self):
        issue = {"dispatch.create_manifest", "dispatch.issue_gate_pass", "dispatch.issue_shipping_documents"}
        self.assertTrue(issue <= self.perms(self.clerk))
        for user in (self.supervisor, self.manager, self.qa_head, self.plant_manager, self.lead, self.qa, self.operator, self.dock):
            self.assertFalse(issue & self.perms(user), user.role)

    def test_who_can_see_the_dispatch_board(self):
        seeing = (self.clerk, self.supervisor, self.manager, self.qa_head, self.plant_manager, self.lead)
        for user in seeing:
            self.assertIn("dispatch.view_shipmentmanifest", self.perms(user), user.role)
        for user in (self.qa, self.operator, self.dock, self.engineer):
            self.assertNotIn("dispatch.view_shipmentmanifest", self.perms(user), user.role)

    def test_no_default_add_change_delete_permissions_exist(self):
        codes = set(Permission.objects.filter(content_type__app_label="dispatch").values_list("codename", flat=True))
        self.assertFalse({c for c in codes if c.startswith(("add_", "change_", "delete_"))})
