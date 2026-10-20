from datetime import timedelta

from apps.core.models import AuditLog, Notification
from apps.production import services as production
from apps.production.models import JobTraveler, PartSerial, TravelerOperation
from apps.review import services
from apps.review.exceptions import (
    InvalidDisposition, NotAuthorized, RecordLocked, SignoffError,
)
from apps.review.models import (
    Disposition, MRBDisposition, MRBSignoff, QuarantineRecord,
)

from .helpers import LATER, NOW, ReviewTestCase, pdf_upload, pdi_noctq_fail, pdi_pass


class MrbBase(ReviewTestCase):
    def setUp(self):
        self.insp, self.traveler = pdi_noctq_fail(self)
        self.quarantine = services.quarantine_lot(self.insp, self.supervisor, "Reject the lot, roughness high", LATER)

    def propose(self, kind=Disposition.SCRAP, user=None, notes="Corrective action text", **kw):
        defaults = {
            Disposition.SCRAP: dict(scrap_cost_code="SC-100", scrap_bin_verified=True),
            Disposition.RTV: dict(debit_note_ref="DN-77"),
            Disposition.REWORK: dict(rework_from_op=30),
            Disposition.CUSTOMER_CONCESSION: {},
        }[kind]
        defaults.update(kw)
        return services.propose_disposition(
            self.quarantine, user or self.qa_head, kind, notes, now=LATER, **defaults)

    def sign_all(self, disposition):
        for user in (self.plant_manager, self.qa_head, self.lead):
            services.sign_disposition(disposition, user, LATER)

    def reload(self):
        self.traveler.refresh_from_db()
        self.quarantine.refresh_from_db()


class ProposalTests(MrbBase):
    def test_board_members_can_propose(self):
        d = self.propose(user=self.lead)
        self.assertEqual((d.proposed_by, d.disposition), (self.lead, Disposition.SCRAP))
        self.assertFalse(d.is_executed)

    def test_other_roles_cannot_propose(self):
        for user in (self.supervisor, self.manager, self.qa, self.operator, self.engineer, self.dock):
            with self.assertRaises(NotAuthorized):
                self.propose(user=user)

    def test_only_one_proposal_per_quarantine(self):
        self.propose()
        with self.assertRaisesMessage(InvalidDisposition, "already been proposed"):
            self.propose(Disposition.RTV)

    def test_unknown_disposition_and_missing_notes_refused(self):
        with self.assertRaises(InvalidDisposition):
            services.propose_disposition(self.quarantine, self.qa_head, "BURN", "notes", now=LATER)
        with self.assertRaises(InvalidDisposition):
            services.propose_disposition(self.quarantine, self.qa_head, Disposition.RTV, "  ",
                                         debit_note_ref="D", now=LATER)

    def test_scrap_needs_cost_code_and_bin_verification(self):
        with self.assertRaisesMessage(InvalidDisposition, "cost code"):
            self.propose(Disposition.SCRAP, scrap_cost_code="")
        with self.assertRaisesMessage(InvalidDisposition, "scrap bin"):
            self.propose(Disposition.SCRAP, scrap_bin_verified=False)

    def test_rtv_needs_a_debit_note(self):
        with self.assertRaisesMessage(InvalidDisposition, "debit note"):
            self.propose(Disposition.RTV, debit_note_ref=" ")

    def test_rework_needs_a_valid_restart_operation(self):
        for bad in (None, 5, 40, 99):  # 40 is the final PDI
            with self.assertRaisesMessage(InvalidDisposition, "restarts"):
                self.propose(Disposition.REWORK, rework_from_op=bad)

    def test_proposal_cannot_be_edited(self):
        d = self.propose()
        d = MRBDisposition.objects.get(pk=d.pk)
        d.disposition = Disposition.RTV
        with self.assertRaises(RecordLocked):
            d.save()
        d = MRBDisposition.objects.get(pk=d.pk)
        d.notes = "rewritten"
        with self.assertRaises(RecordLocked):
            d.save()

    def test_proposal_is_audited_and_board_notified(self):
        self.propose()
        self.assertTrue(AuditLog.objects.filter(model_name="review.MRBDisposition", field_name="disposition").exists())
        for user in (self.plant_manager, self.qa_head, self.lead):
            self.assertTrue(Notification.objects.filter(recipient=user, subject__startswith="MRB sign-off needed").exists())

    def test_no_proposal_once_the_quarantine_is_closed(self):
        d = self.propose()
        self.sign_all(d)
        with self.assertRaises(InvalidDisposition):
            self.propose(Disposition.RTV)


class SignoffTests(MrbBase):
    def test_all_three_roles_are_required(self):
        d = self.propose()
        self.assertFalse(services.sign_disposition(d, self.plant_manager, LATER))
        self.assertFalse(services.sign_disposition(d, self.qa_head, LATER))
        d.refresh_from_db()
        self.assertFalse(d.is_executed)
        self.reload()
        self.assertEqual(self.traveler.status, JobTraveler.Status.QUARANTINED)
        self.assertEqual(self.quarantine.status, QuarantineRecord.Status.OPEN)
        self.assertTrue(services.sign_disposition(d, self.lead, LATER))

    def test_two_of_three_is_not_enough(self):
        d = self.propose()
        services.sign_disposition(d, self.qa_head, LATER)
        services.sign_disposition(d, self.lead, LATER)
        d.refresh_from_db()
        self.assertIsNone(d.decided_at)

    def test_only_board_roles_may_sign(self):
        d = self.propose()
        for user in (self.supervisor, self.manager, self.qa, self.operator, self.dock):
            with self.assertRaises((NotAuthorized, SignoffError)):
                services.sign_disposition(d, user, LATER)
        self.assertEqual(MRBSignoff.objects.count(), 0)

    def test_a_role_signs_once(self):
        d = self.propose()
        services.sign_disposition(d, self.qa_head, LATER)
        with self.assertRaisesMessage(SignoffError, "already signed"):
            services.sign_disposition(d, self.qa_head, LATER)

    def test_a_second_user_of_the_same_role_does_not_count(self):
        from apps.accounts.models import Role
        from apps.accounts.tests.helpers import make_user
        second_head = make_user("qh2", Role.QUALITY_HEAD)
        d = self.propose()
        services.sign_disposition(d, self.qa_head, LATER)
        with self.assertRaisesMessage(SignoffError, "Quality Head has already signed"):
            services.sign_disposition(d, second_head, LATER)

    def test_signoffs_are_audited_and_permanent(self):
        d = self.propose()
        services.sign_disposition(d, self.plant_manager, LATER)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.SIGN_OFF, user=self.plant_manager,
                                                model_name="review.MRBSignoff").exists())
        s = MRBSignoff.objects.get()
        s.role = "QUALITY_HEAD"
        with self.assertRaises(RecordLocked):
            s.save()
        with self.assertRaises(RecordLocked):
            s.delete()
        with self.assertRaises(RecordLocked):
            MRBSignoff.objects.all().delete()

    def test_cannot_sign_an_executed_disposition(self):
        d = self.propose()
        self.sign_all(d)
        with self.assertRaisesMessage(SignoffError, "already been executed"):
            services.sign_disposition(d, self.plant_manager, LATER)

    def test_executed_disposition_is_locked(self):
        d = self.propose()
        self.sign_all(d)
        d = MRBDisposition.objects.get(pk=d.pk)
        d.notes = "changed"
        with self.assertRaises(RecordLocked):
            d.save()
        with self.assertRaises(RecordLocked):
            d.delete()


class ExecutionTests(MrbBase):
    def test_scrap_closes_the_lot(self):
        d = self.propose(Disposition.SCRAP)
        self.sign_all(d)
        self.reload()
        self.assertEqual(self.traveler.status, JobTraveler.Status.CLOSED)
        self.assertEqual(self.quarantine.status, QuarantineRecord.Status.CLOSED)
        self.assertEqual(self.quarantine.closed_at, LATER)
        d.refresh_from_db()
        self.assertEqual(d.decided_at, LATER)
        self.assertEqual(d.scrap_cost_code, "SC-100")
        self.assertTrue(d.scrap_bin_verified)

    def test_rtv_closes_the_lot_and_alerts_the_dock(self):
        d = self.propose(Disposition.RTV)
        self.sign_all(d)
        self.reload()
        self.assertEqual(self.traveler.status, JobTraveler.Status.CLOSED)
        note = Notification.objects.get(recipient=self.dock)
        self.assertIn("DN-77", note.subject)

    def test_rework_creates_a_rework_traveler_from_the_chosen_operation(self):
        d = self.propose(Disposition.REWORK, rework_from_op=30)
        self.sign_all(d)
        self.reload()
        d.refresh_from_db()
        rework = d.rework_traveler
        self.assertTrue(rework.is_rework)
        self.assertEqual(rework.parent_traveler, self.traveler)
        self.assertEqual(rework.quantity, self.traveler.quantity)
        self.assertEqual(rework.status, JobTraveler.Status.ACTIVE)
        self.assertEqual(
            list(rework.operations.order_by("routing_operation__op_number")
                 .values_list("routing_operation__op_number", "status")),
            [(30, "PENDING"), (40, "PENDING")])
        self.assertEqual(self.traveler.status, JobTraveler.Status.CLOSED)
        self.assertEqual(self.quarantine.status, QuarantineRecord.Status.CLOSED)

    def test_rework_traveler_has_its_own_signed_barcode_and_can_be_worked(self):
        from apps.production.tests.helpers import make_machine_ready, op_at
        d = self.propose(Disposition.REWORK, rework_from_op=30)
        self.sign_all(d)
        d.refresh_from_db()
        rework = d.rework_traveler
        self.assertEqual(production.resolve_traveler_from_barcode(rework.barcode), rework)
        machine = make_machine_ready("CNC-RW", LATER, self.operator)
        op = production.start_operation(op_at(rework, 30), self.operator, machine, LATER)
        self.assertEqual(op.status, TravelerOperation.Status.IN_PROGRESS)

    def test_rework_pdi_completion_numbers_serials_separately(self):
        d = self.propose(Disposition.REWORK, rework_from_op=30)
        self.sign_all(d)
        d.refresh_from_db()
        rework = d.rework_traveler
        TravelerOperation.objects.filter(traveler=rework).update(status="COMPLETED")
        JobTraveler.objects.filter(pk=rework.pk).update(status="COMPLETED")
        rework.refresh_from_db()
        serials = production.generate_serials(rework, self.lead)
        self.assertTrue(serials[0].serial_number.startswith(f"WO-PDI-R{rework.pk}-"))

    def test_execution_waits_for_the_last_signature_only(self):
        d = self.propose(Disposition.SCRAP)
        services.sign_disposition(d, self.plant_manager, LATER)
        self.reload()
        self.assertEqual(self.traveler.status, JobTraveler.Status.QUARANTINED)

    def test_execution_is_audited(self):
        d = self.propose(Disposition.SCRAP)
        self.sign_all(d)
        self.assertTrue(AuditLog.objects.filter(
            model_name="review.MRBDisposition", field_name="decided_at", user=self.lead).exists())


class CustomerConcessionTests(MrbBase):
    def test_batch_stays_held_until_the_customer_approval_is_uploaded(self):
        d = self.propose(Disposition.CUSTOMER_CONCESSION)
        self.sign_all(d)
        d.refresh_from_db()
        self.reload()
        self.assertFalse(d.is_executed)
        self.assertEqual(self.traveler.status, JobTraveler.Status.QUARANTINED)
        self.assertIn("customer", services.execution_blockers(d)[0].lower())

    def test_uploading_the_approval_releases_the_lot_and_closes_the_pdi(self):
        d = self.propose(Disposition.CUSTOMER_CONCESSION)
        self.sign_all(d)
        self.assertTrue(services.attach_customer_approval(d, pdf_upload(), self.plant_manager, LATER))
        self.reload()
        self.assertEqual(self.traveler.status, JobTraveler.Status.COMPLETED)  # released AND PDI closed
        self.assertEqual(self.quarantine.status, QuarantineRecord.Status.CLOSED)
        self.assertEqual(PartSerial.objects.count(), 2)

    def test_signing_last_after_the_upload_also_releases(self):
        d = self.propose(Disposition.CUSTOMER_CONCESSION)
        services.sign_disposition(d, self.plant_manager, LATER)
        services.sign_disposition(d, self.qa_head, LATER)
        self.assertFalse(services.attach_customer_approval(d, pdf_upload(), self.qa_head, LATER))
        self.assertTrue(services.sign_disposition(d, self.lead, LATER))
        self.reload()
        self.assertEqual(self.traveler.status, JobTraveler.Status.COMPLETED)

    def test_upload_without_the_signatures_does_not_release(self):
        d = self.propose(Disposition.CUSTOMER_CONCESSION)
        services.attach_customer_approval(d, pdf_upload(), self.qa_head, LATER)
        self.reload()
        self.assertEqual(self.traveler.status, JobTraveler.Status.QUARANTINED)

    def test_only_documents_are_accepted_and_stored_under_a_random_name(self):
        d = self.propose(Disposition.CUSTOMER_CONCESSION)
        for data in (b"<html>", b"GIF89a", b"", None):
            with self.assertRaises(InvalidDisposition):
                services.attach_customer_approval(d, pdf_upload(data=data) if data is not None else None, self.qa_head, LATER)
        services.attach_customer_approval(d, pdf_upload(name="../../evil.php"), self.qa_head, LATER)
        d.refresh_from_db()
        self.assertNotIn("evil", d.customer_approval_file.name)
        self.assertTrue(d.customer_approval_file.name.endswith(".pdf"))

    def test_approval_cannot_be_replaced(self):
        d = self.propose(Disposition.CUSTOMER_CONCESSION)
        services.attach_customer_approval(d, pdf_upload(), self.qa_head, LATER)
        with self.assertRaisesMessage(InvalidDisposition, "already uploaded"):
            services.attach_customer_approval(d, pdf_upload(), self.qa_head, LATER)

    def test_other_dispositions_take_no_approval(self):
        d = self.propose(Disposition.SCRAP)
        with self.assertRaises(InvalidDisposition):
            services.attach_customer_approval(d, pdf_upload(), self.qa_head, LATER)

    def test_only_board_members_upload(self):
        d = self.propose(Disposition.CUSTOMER_CONCESSION)
        for user in (self.supervisor, self.qa, self.manager):
            with self.assertRaises(NotAuthorized):
                services.attach_customer_approval(d, pdf_upload(), user, LATER)
