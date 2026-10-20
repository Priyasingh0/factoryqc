from datetime import timedelta
from unittest import mock

from apps.core.models import AuditLog
from apps.core.signing import QRKind, make_token
from apps.dispatch import services
from apps.dispatch.exceptions import DocumentError, ManifestError, NotAuthorized, ShippingInterlock
from apps.dispatch.models import GatePass, ManifestSerial, ShipmentLabel, ShipmentManifest, ShippingDocument

from apps.production.models import JobTraveler

from .helpers import (
    AFTER, DispatchTestCase, approved_lot, conceded_lot, make_red, quarantined_lot, set_hold, set_traveler_status,
)

KIND = ShippingDocument.Kind


def manifest_for(case, lot, **kwargs):
    kwargs.setdefault("vehicle_number", "MH-12-AB-1234")
    return services.create_manifest(lot, case.clerk, now=AFTER, **kwargs)


class ManifestTests(DispatchTestCase):
    def test_creates_manifest_with_all_unshipped_parts(self):
        lot = approved_lot(self)
        m = manifest_for(self, lot)
        self.assertEqual(m.batch_size, 2)
        self.assertEqual(m.items.count(), 2)
        self.assertEqual(m.status, ShipmentManifest.Status.OPEN)
        self.assertEqual(m.created_by, self.clerk)
        row = AuditLog.objects.get(action=AuditLog.Action.CREATE, model_name="dispatch.ShipmentManifest")
        self.assertIn("interlock green", row.remarks)
        self.assertEqual(row.user, self.clerk)

    def test_locked_lot_cannot_get_a_manifest_and_the_attempt_is_audited(self):
        lot = approved_lot(self)
        make_red(lot, 20)
        with self.assertRaises(ShippingInterlock):
            manifest_for(self, lot)
        self.assertFalse(ShipmentManifest.objects.exists())
        self.assertTrue(AuditLog.objects.filter(field_name="shipping_interlock", user=self.clerk).exists())

    def test_quarantined_lot_cannot_get_a_manifest(self):
        lot = quarantined_lot(self)
        with self.assertRaises(ShippingInterlock):
            manifest_for(self, lot)

    def test_only_clerks_may_create(self):
        lot = approved_lot(self)
        for user in (self.supervisor, self.qa, self.operator, self.manager, self.dock):
            with self.assertRaises(NotAuthorized):
                services.create_manifest(lot, user, vehicle_number="V1", customer_po="PO", now=AFTER)
        self.assertFalse(ShipmentManifest.objects.exists())

    def test_vehicle_is_required(self):
        lot = approved_lot(self)
        with self.assertRaises(ManifestError):
            manifest_for(self, lot, vehicle_number="  ")

    def test_po_defaults_from_work_order_and_must_match_it(self):
        lot = approved_lot(self)
        wo = lot.work_order
        self.assertTrue(wo.customer_po)  # the helper work order carries a PO
        m = manifest_for(self, lot, customer_po="")
        self.assertEqual(m.customer_po, wo.customer_po)
        with self.assertRaises(ManifestError):
            manifest_for(self, lot, customer_po="SOMEONE-ELSE")

    def test_partial_shipments_and_a_serial_ships_only_once(self):
        lot = approved_lot(self)
        first, second = list(lot.serials.all())
        m1 = manifest_for(self, lot, serials=[first])
        self.assertEqual(m1.batch_size, 1)
        with self.assertRaises(ManifestError) as ctx:
            manifest_for(self, lot, serials=[first])
        self.assertIn(first.serial_number, str(ctx.exception))
        m2 = manifest_for(self, lot)  # default: everything left
        self.assertEqual([i.serial for i in m2.items.all()], [second])
        with self.assertRaises(ManifestError):
            manifest_for(self, lot)  # nothing left

    def test_unknown_serial_is_rejected(self):
        lot = approved_lot(self)
        with self.assertRaises(ManifestError):
            manifest_for(self, lot, serials=[999999])
        self.assertFalse(ShipmentManifest.objects.exists())


class GatePassTests(DispatchTestCase):
    def test_issue_marks_manifest_dispatched_and_audits(self):
        m = manifest_for(self, approved_lot(self))
        gp = services.issue_gate_pass(m, self.clerk, AFTER)
        m.refresh_from_db()
        self.assertEqual(gp.number, f"GP-{m.pk:06d}")
        self.assertEqual(m.status, ShipmentManifest.Status.DISPATCHED)
        self.assertEqual(m.dispatched_at, AFTER)
        row = AuditLog.objects.get(action=AuditLog.Action.DECISION, model_name="dispatch.ShipmentManifest", object_id=str(m.pk))
        self.assertEqual((row.previous_value, row.new_value), ("OPEN", "DISPATCHED"))
        self.assertIn(gp.number, row.remarks)

    def test_second_gate_pass_is_refused(self):
        m = manifest_for(self, approved_lot(self))
        services.issue_gate_pass(m, self.clerk, AFTER)
        with self.assertRaises(DocumentError):
            services.issue_gate_pass(m, self.clerk, AFTER)
        self.assertEqual(GatePass.objects.count(), 1)

    def test_locked_lot_gets_no_gate_pass(self):
        lot = approved_lot(self)
        m = manifest_for(self, lot)
        set_hold(lot, AFTER + timedelta(hours=3))
        with self.assertRaises(ShippingInterlock):
            services.issue_gate_pass(m, self.clerk, AFTER)
        m.refresh_from_db()
        self.assertEqual(m.status, ShipmentManifest.Status.OPEN)
        self.assertFalse(GatePass.objects.exists())
        self.assertTrue(AuditLog.objects.filter(field_name="shipping_interlock", remarks__contains="gate pass").exists())

    def test_gate_pass_after_the_hold_expires(self):
        lot = approved_lot(self)
        m = manifest_for(self, lot)
        set_hold(lot, AFTER + timedelta(hours=3))
        services.issue_gate_pass(m, self.clerk, AFTER + timedelta(hours=4))
        self.assertTrue(GatePass.objects.exists())

    def test_only_clerks_issue_gate_passes(self):
        m = manifest_for(self, approved_lot(self))
        for user in (self.supervisor, self.manager, self.qa):
            with self.assertRaises(NotAuthorized):
                services.issue_gate_pass(m, user, AFTER)

    def test_no_parts_can_be_added_after_dispatch(self):
        lot = approved_lot(self)
        m = manifest_for(self, lot, serials=[lot.serials.first()])
        services.issue_gate_pass(m, self.clerk, AFTER)
        from apps.review.exceptions import RecordLocked
        with self.assertRaises(RecordLocked):
            ManifestSerial.objects.create(manifest=m, serial=lot.serials.last())


class DocumentTests(DispatchTestCase):
    def test_packing_slip_and_invoice_are_issued_once_each(self):
        m = manifest_for(self, approved_lot(self))
        slip, created = services.issue_document(m, KIND.PACKING_SLIP, self.clerk, AFTER)
        self.assertTrue(created)
        self.assertEqual(slip.number, f"PS-{m.pk:06d}")
        invoice, created = services.issue_document(m, KIND.INVOICE, self.clerk, AFTER)
        self.assertEqual((created, invoice.number), (True, f"INV-{m.pk:06d}"))
        again, created = services.issue_document(m, KIND.PACKING_SLIP, self.clerk, AFTER)
        self.assertEqual((again, created), (slip, False))
        self.assertEqual(ShippingDocument.objects.count(), 2)
        self.assertTrue(AuditLog.objects.filter(field_name="reprint").exists())

    def test_unknown_document_kind_refused(self):
        m = manifest_for(self, approved_lot(self))
        with self.assertRaises(DocumentError):
            services.issue_document(m, "RECEIPT", self.clerk, AFTER)

    def test_packing_slip_and_invoice_blocked_when_locked(self):
        lot = approved_lot(self)
        m = manifest_for(self, lot)
        make_red(lot, 30)
        for kind in (KIND.PACKING_SLIP, KIND.INVOICE):
            with self.assertRaises(ShippingInterlock):
                services.issue_document(m, kind, self.clerk, AFTER)
        self.assertFalse(ShippingDocument.objects.exists())
        self.assertEqual(AuditLog.objects.filter(field_name="shipping_interlock").count(), 2)

    def test_reprint_is_blocked_when_the_lot_becomes_locked(self):
        lot = approved_lot(self)
        m = manifest_for(self, lot)
        services.issue_document(m, KIND.INVOICE, self.clerk, AFTER)
        make_red(lot, 30)
        with self.assertRaises(ShippingInterlock):
            services.issue_document(m, KIND.INVOICE, self.clerk, AFTER)

    def test_only_warehouse_clerks_issue_documents(self):
        m = manifest_for(self, approved_lot(self))
        for user in (self.supervisor, self.manager, self.qa_head, self.qa):
            with self.assertRaises(NotAuthorized):
                services.issue_document(m, KIND.INVOICE, user, AFTER)

    def test_labels_get_the_next_crate_number(self):
        m = manifest_for(self, approved_lot(self))
        a = services.issue_label(m, self.clerk, AFTER)
        b = services.issue_label(m, self.clerk2, AFTER)
        self.assertEqual((a.crate_number, b.crate_number), (1, 2))
        self.assertEqual(ShipmentLabel.objects.count(), 2)

    def test_labels_blocked_when_locked(self):
        lot = approved_lot(self)
        m = manifest_for(self, lot)
        make_red(lot, 20)
        with self.assertRaises(ShippingInterlock):
            services.issue_label(m, self.clerk, AFTER)
        self.assertFalse(ShipmentLabel.objects.exists())

    def test_authorize_print_checks_the_interlock_and_audits(self):
        lot = approved_lot(self)
        m = manifest_for(self, lot)
        self.assertEqual(services.authorize_print(m, self.clerk, "packing slip", AFTER), m)
        make_red(lot, 20)
        with self.assertRaises(ShippingInterlock):
            services.authorize_print(m, self.clerk, "packing slip", AFTER)
        self.assertTrue(AuditLog.objects.filter(remarks__contains="print the packing slip").exists())


class CertificateTests(DispatchTestCase):
    def label(self, lot=None):
        lot = lot or approved_lot(self)
        m = manifest_for(self, lot)
        return services.issue_label(m, self.clerk, AFTER)

    def test_valid_token_returns_the_report(self):
        label = self.label()
        data = services.certificate(label.token)
        self.assertTrue(data["valid"])
        self.assertEqual(data["manifest"], label.manifest)
        self.assertEqual(len(data["serials"]), 2)
        self.assertEqual(len(data["readings"]), 4)  # the four PDI fields
        self.assertEqual(data["decision"].decision, "APPROVED")
        self.assertIsNone(data["concession"])

    def test_concession_is_disclosed(self):
        data = services.certificate(self.label(conceded_lot(self)).token)
        self.assertTrue(data["valid"])
        self.assertIsNotNone(data["concession"])
        self.assertTrue(any(not m.is_within_tolerance for m in data["readings"]))

    def test_forged_or_wrong_kind_tokens_are_rejected(self):
        label = self.label()
        self.assertIsNone(services.certificate(label.token[:-3] + "xyz"))
        self.assertIsNone(services.certificate("garbage"))
        self.assertIsNone(services.certificate(make_token(QRKind.TRAVELER, label.pk)))
        self.assertIsNone(services.certificate(make_token(QRKind.SHIPMENT, 987654)))
        self.assertIsNone(services.certificate(make_token(QRKind.SHIPMENT, "not-a-number")))

    def test_certificate_is_withdrawn_if_the_lot_turns_red(self):
        lot = approved_lot(self)
        label = self.label(lot)
        make_red(lot, 20)
        data = services.certificate(label.token)
        self.assertFalse(data["valid"])
        self.assertTrue(data["problems"])


class OverviewTests(DispatchTestCase):
    def test_board_rows(self):
        green = approved_lot(self)
        rows = services.overview(AFTER)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["clearance"].cleared)
        self.assertEqual((rows[0]["shipped"], rows[0]["total"]), (0, 2))
        manifest_for(self, green, serials=[green.serials.first()])
        self.assertEqual(services.overview(AFTER)[0]["shipped"], 1)

    def test_closed_travelers_are_not_listed_and_locked_ones_show_why(self):
        lot = quarantined_lot(self)
        row = services.overview(AFTER)[0]
        self.assertFalse(row["clearance"].cleared)
        self.assertTrue(row["first_problem"])


class RecheckInsideTheWriteTests(DispatchTestCase):
    """The interlock is checked again after the lot is locked for writing (a race between
    the first check and the write must not let a document through)."""

    def test_every_issuing_function_rechecks_on_its_own(self):
        lot = approved_lot(self)
        m = manifest_for(self, lot)
        make_red(lot, 20)
        with mock.patch.object(services, "assert_cleared"):
            with self.assertRaises(ShippingInterlock):
                services.create_manifest(lot, self.clerk, vehicle_number="V", now=AFTER)
            with self.assertRaises(ShippingInterlock):
                services.issue_gate_pass(m, self.clerk, AFTER)
            with self.assertRaises(ShippingInterlock):
                services.issue_document(m, KIND.INVOICE, self.clerk, AFTER)
            with self.assertRaises(ShippingInterlock):
                services.issue_label(m, self.clerk, AFTER)
        self.assertEqual(ShipmentManifest.objects.count(), 1)
        self.assertFalse(GatePass.objects.exists() or ShippingDocument.objects.exists() or ShipmentLabel.objects.exists())


class ClosedTravelerTests(DispatchTestCase):
    def test_closed_travelers_are_left_off_the_board(self):
        lot = approved_lot(self)
        set_traveler_status(lot, JobTraveler.Status.CLOSED)
        self.assertEqual(services.overview(AFTER), [])
