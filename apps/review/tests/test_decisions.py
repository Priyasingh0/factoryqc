from apps.checklists.models import ChecklistType
from apps.core.models import AuditLog, Notification
from apps.inspections.models import Inspection, InspectionStatus
from apps.production import services as production
from apps.production.exceptions import OperationBlocked
from apps.production.models import JobTraveler, PartSerial, ProductionRun, TravelerOperation
from apps.review import services
from apps.review.exceptions import CtqConcessionBlocked, DecisionBlocked, NotAuthorized
from apps.review.models import (
    HOLDING_AREA, Concession, Decision, QuarantineRecord, ReasonCode, SupervisorDecision,
)

from .helpers import (
    LATER, LONG_TEXT, NOW, ReviewTestCase, first_piece_failed, first_piece_submitted, pdi_ctq_failure_submitted,
    pdi_ctq_frozen, pdi_noctq_fail, pdi_pass,
)


class ApproveTests(ReviewTestCase):
    def test_approving_a_clean_pdi_clears_the_lot(self):
        insp, traveler = pdi_pass(self)
        decision = services.approve_inspection(insp, self.supervisor, "all good", LATER)
        insp.refresh_from_db()
        traveler.refresh_from_db()
        self.assertEqual(decision.decision, Decision.APPROVED)
        self.assertEqual(insp.status, InspectionStatus.APPROVED)
        pdi_op = traveler.operations.get(routing_operation__is_final_pdi=True)
        self.assertEqual(pdi_op.status, TravelerOperation.Status.COMPLETED)
        self.assertEqual(pdi_op.signed_off_by, self.supervisor)
        self.assertEqual(traveler.status, JobTraveler.Status.COMPLETED)

    def test_approved_pdi_issues_serial_numbers(self):
        insp, traveler = pdi_pass(self)
        services.approve_inspection(insp, self.supervisor, "", LATER)
        self.assertEqual(
            list(PartSerial.objects.values_list("serial_number", flat=True)), ["WO-PDI-0001", "WO-PDI-0002"])

    def test_decision_is_audited(self):
        insp, _ = pdi_pass(self)
        services.approve_inspection(insp, self.supervisor, "ok", LATER)
        row = AuditLog.objects.get(action=AuditLog.Action.DECISION, model_name="inspections.Inspection",
                                   field_name="status", new_value="APPROVED")
        self.assertEqual((row.user, row.previous_value), (self.supervisor, "SUBMITTED"))

    def test_only_roles_with_the_permission_may_approve(self):
        insp, _ = pdi_pass(self)
        for user in (self.qa, self.manager, self.qa_head, self.operator, self.lead, self.dock):
            with self.assertRaises(NotAuthorized):
                services.approve_inspection(insp, user, "", LATER)
        insp.refresh_from_db()
        self.assertEqual(insp.status, InspectionStatus.SUBMITTED)

    def test_cannot_approve_with_an_out_of_tolerance_reading(self):
        insp, _ = pdi_noctq_fail(self)
        with self.assertRaisesMessage(DecisionBlocked, "every reading within tolerance"):
            services.approve_inspection(insp, self.supervisor, "", LATER)

    def test_cannot_approve_a_frozen_checklist(self):
        insp, _ = pdi_ctq_frozen(self)
        with self.assertRaisesMessage(DecisionBlocked, "frozen"):
            services.approve_inspection(insp, self.supervisor, "", LATER)

    def test_cannot_approve_an_unsubmitted_inspection(self):
        from apps.inspections.tests.helpers import begin_pdi, lot_ready_for_pdi
        traveler, station = lot_ready_for_pdi(self)
        insp = begin_pdi(self, traveler, station)
        with self.assertRaisesMessage(DecisionBlocked, "not submitted"):
            services.approve_inspection(insp, self.supervisor, "", LATER)

    def test_cannot_decide_twice(self):
        insp, _ = pdi_pass(self)
        services.approve_inspection(insp, self.supervisor, "", LATER)
        for action in (
            lambda: services.approve_inspection(insp, self.supervisor, "", LATER),
            lambda: services.quarantine_lot(insp, self.supervisor, "changed my mind now", LATER),
        ):
            with self.assertRaisesMessage(DecisionBlocked, "already decided"):
                action()
        self.assertEqual(SupervisorDecision.objects.count(), 1)

    def test_supervisor_cannot_decide_on_their_own_inspection(self):
        insp, _ = pdi_pass(self)
        # Re-create the same inspection row as if the supervisor had carried it out.
        clone = Inspection.objects.create(
            template=insp.template, checklist_type=insp.checklist_type, traveler=insp.traveler,
            traveler_operation=insp.traveler_operation, machine=insp.machine, inspector=self.supervisor,
            status=InspectionStatus.SUBMITTED, submitted_at=NOW)
        with self.assertRaisesMessage(NotAuthorized, "yourself"):
            services.approve_inspection(clone, self.supervisor, "", LATER)

    def test_approving_a_first_piece_keeps_the_signoff_path_open(self):
        insp, traveler, _ = first_piece_submitted(self)
        services.approve_inspection(insp, self.supervisor, "", LATER)
        run = ProductionRun.objects.get(traveler_operation__traveler=traveler)
        production.clear_first_piece(run, self.qa2, LATER)  # APPROVED status is accepted by production
        run.refresh_from_db()
        self.assertEqual(run.state, ProductionRun.State.RUNNING)

    def test_approval_does_not_touch_the_final_step_of_a_first_piece(self):
        insp, traveler, _ = first_piece_submitted(self)
        services.approve_inspection(insp, self.supervisor, "", LATER)
        self.assertFalse(traveler.operations.filter(
            routing_operation__is_final_pdi=True, status=TravelerOperation.Status.COMPLETED).exists())


class ConcessionTests(ReviewTestCase):
    def concede(self, insp, user=None, code=ReasonCode.FIT_FORM_FUNCTION, text=LONG_TEXT):
        return services.issue_concession(insp, user or self.auth_sup, code, text, LATER)

    def test_authorised_supervisor_can_issue_a_concession(self):
        insp, traveler = pdi_noctq_fail(self)
        decision = self.concede(insp)
        insp.refresh_from_db()
        self.assertEqual(decision.decision, Decision.CONCESSION)
        self.assertEqual(insp.status, InspectionStatus.CONCESSION)
        concession = Concession.objects.get()
        self.assertEqual((concession.reason_code, concession.issued_by), (ReasonCode.FIT_FORM_FUNCTION, self.auth_sup))
        self.assertEqual(concession.justification, LONG_TEXT)

    def test_quality_manager_can_issue_a_concession(self):
        insp, _ = pdi_noctq_fail(self)
        self.concede(insp, user=self.manager)
        self.assertEqual(Concession.objects.get().issued_by, self.manager)

    def test_unauthorised_supervisor_cannot(self):
        insp, _ = pdi_noctq_fail(self)
        with self.assertRaisesMessage(NotAuthorized, "authorised"):
            self.concede(insp, user=self.supervisor)
        self.assertFalse(Concession.objects.exists())

    def test_other_roles_cannot(self):
        insp, _ = pdi_noctq_fail(self)
        for user in (self.qa_head, self.plant_manager, self.lead, self.qa, self.operator):
            with self.assertRaises(NotAuthorized):
                self.concede(insp, user=user)

    def test_released_lot_completes_the_pdi_and_is_flagged(self):
        insp, traveler = pdi_noctq_fail(self)
        self.concede(insp)
        traveler.refresh_from_db()
        self.assertEqual(traveler.status, JobTraveler.Status.COMPLETED)
        self.assertTrue(traveler.inspections.filter(status=InspectionStatus.CONCESSION).exists())
        self.assertEqual(PartSerial.objects.count(), 2)

    def test_reason_code_is_required_and_must_be_valid(self):
        insp, _ = pdi_noctq_fail(self)
        for bad in ("", None, "MADE_UP"):
            with self.assertRaisesMessage(DecisionBlocked, "reason code"):
                self.concede(insp, code=bad)

    def test_justification_is_required_and_must_be_detailed(self):
        insp, _ = pdi_noctq_fail(self)
        for bad in ("", "   ", None, "too short", "x" * 19):
            with self.assertRaisesMessage(DecisionBlocked, "justification"):
                self.concede(insp, text=bad)
        self.assertFalse(Concession.objects.exists())
        self.concede(insp, text="y" * 20)  # exactly the minimum is accepted

    def test_ctq_failure_blocks_the_concession(self):
        insp, _ = pdi_ctq_failure_submitted(self)
        for user in (self.auth_sup, self.manager):
            with self.assertRaisesMessage(CtqConcessionBlocked, "Bore"):
                self.concede(insp, user=user)
        insp.refresh_from_db()
        self.assertEqual(insp.status, InspectionStatus.SUBMITTED)
        self.assertFalse(Concession.objects.exists())

    def test_ctq_block_wins_even_when_a_non_ctq_also_failed(self):
        insp, _ = pdi_ctq_failure_submitted(self)
        self.assertTrue(insp.measurements.filter(field__is_ctq=True, is_within_tolerance=False).exists())
        with self.assertRaises(CtqConcessionBlocked):
            self.concede(insp)

    def test_frozen_checklist_blocks_the_concession(self):
        insp, _ = pdi_ctq_frozen(self)
        with self.assertRaises(CtqConcessionBlocked):
            self.concede(insp, user=self.manager)

    def test_all_in_tolerance_needs_no_concession(self):
        insp, _ = pdi_pass(self)
        with self.assertRaisesMessage(DecisionBlocked, "approve"):
            self.concede(insp)

    def test_first_piece_cannot_be_conceded(self):
        insp, _, _ = first_piece_failed(self)
        with self.assertRaisesMessage(DecisionBlocked, "first piece"):
            self.concede(insp)

    def test_concession_is_audited_and_quality_head_told(self):
        insp, _ = pdi_noctq_fail(self)
        self.concede(insp)
        self.assertTrue(AuditLog.objects.filter(
            model_name="review.Concession", field_name="justification", user=self.auth_sup).exists())
        self.assertTrue(AuditLog.objects.filter(
            model_name="inspections.Inspection", new_value="CONCESSION", user=self.auth_sup).exists())
        self.assertTrue(Notification.objects.filter(
            recipient=self.qa_head, subject__startswith="Concession issued").exists())

    def test_cannot_concede_twice(self):
        insp, _ = pdi_noctq_fail(self)
        self.concede(insp)
        with self.assertRaisesMessage(DecisionBlocked, "already decided"):
            self.concede(insp, user=self.manager)


class QuarantineTests(ReviewTestCase):
    def test_quarantine_locks_the_traveler_and_creates_the_record(self):
        insp, traveler = pdi_noctq_fail(self)
        record = services.quarantine_lot(insp, self.supervisor, "Out of tolerance, reject lot", LATER)
        traveler.refresh_from_db()
        insp.refresh_from_db()
        self.assertEqual(traveler.status, JobTraveler.Status.QUARANTINED)
        self.assertEqual(insp.status, InspectionStatus.QUARANTINED)
        self.assertEqual(record.status, QuarantineRecord.Status.OPEN)
        self.assertEqual(record.location, HOLDING_AREA)
        self.assertEqual(SupervisorDecision.objects.get().decision, Decision.QUARANTINED)

    def test_quarantined_lot_blocks_all_downstream_work(self):
        insp, traveler, machine = first_piece_failed(self)
        services.quarantine_lot(insp, self.supervisor, "Setup failed, reject lot", LATER)
        op10 = traveler.operations.get(routing_operation__op_number=10)
        with self.assertRaisesMessage(OperationBlocked, "Quarantined"):
            production.record_parts(op10, self.operator, 1, LATER)
        with self.assertRaises(OperationBlocked):
            production.complete_operation(op10, self.operator, LATER)
        op20 = traveler.operations.get(routing_operation__op_number=20)
        with self.assertRaises(OperationBlocked):
            production.start_operation(op20, self.operator, machine, LATER)

    def test_quarantined_lot_no_longer_occupies_its_machine(self):
        from apps.production.tests.helpers import make_ready_part, make_work_order, op_at
        insp, traveler, machine = first_piece_failed(self)
        services.quarantine_lot(insp, self.supervisor, "Setup failed, reject lot", LATER)
        part2 = make_ready_part(self.engineer, self.manager, number="P-NEXT")
        t2 = production.release_work_order(make_work_order(self.lead, part2, number="WO-NEXT"), self.lead)
        op = production.start_operation(op_at(t2, 10), self.operator, machine, NOW)
        self.assertEqual(op.status, TravelerOperation.Status.IN_PROGRESS)

    def test_no_new_inspection_can_open_on_a_quarantined_lot(self):
        from apps.inspections.exceptions import CannotStart
        from apps.inspections.tests.helpers import begin_first_piece
        insp, traveler, machine = first_piece_failed(self)
        services.quarantine_lot(insp, self.supervisor, "Setup failed, reject lot", LATER)
        with self.assertRaises(CannotStart):
            begin_first_piece(self, traveler, machine)

    def test_quality_head_can_quarantine_a_ctq_frozen_checklist(self):
        insp, traveler = pdi_ctq_frozen(self)
        services.quarantine_lot(insp, self.qa_head, "CTQ bore out of tolerance, reject", LATER)
        insp.refresh_from_db()
        traveler.refresh_from_db()
        self.assertEqual(insp.status, InspectionStatus.QUARANTINED)
        self.assertEqual(traveler.status, JobTraveler.Status.QUARANTINED)

    def test_who_may_quarantine(self):
        insp, _ = pdi_noctq_fail(self)
        for user in (self.qa, self.manager, self.lead, self.plant_manager, self.operator):
            with self.assertRaises(NotAuthorized):
                services.quarantine_lot(insp, user, "Out of tolerance, reject lot", LATER)

    def test_reason_is_required(self):
        insp, _ = pdi_noctq_fail(self)
        for bad in ("", "  ", "short", None):
            with self.assertRaisesMessage(DecisionBlocked, "Explain why"):
                services.quarantine_lot(insp, self.supervisor, bad, LATER)

    def test_mrb_is_alerted(self):
        insp, _ = pdi_noctq_fail(self)
        services.quarantine_lot(insp, self.supervisor, "Out of tolerance, reject lot", LATER)
        for user in (self.plant_manager, self.qa_head, self.lead):
            self.assertTrue(Notification.objects.filter(
                recipient=user, subject__startswith="Lot quarantined").exists(), user)

    def test_quarantine_is_audited_for_decision_and_traveler(self):
        insp, traveler = pdi_noctq_fail(self)
        services.quarantine_lot(insp, self.supervisor, "Out of tolerance, reject lot", LATER)
        self.assertTrue(AuditLog.objects.filter(
            model_name="inspections.Inspection", new_value="QUARANTINED", user=self.supervisor).exists())
        row = AuditLog.objects.get(model_name="production.JobTraveler", field_name="status")
        self.assertEqual((row.previous_value, row.new_value), ("ACTIVE", "QUARANTINED"))

    def test_an_already_decided_inspection_cannot_be_quarantined(self):
        insp, _ = pdi_noctq_fail(self)
        services.quarantine_lot(insp, self.supervisor, "Out of tolerance, reject lot", LATER)
        with self.assertRaisesMessage(DecisionBlocked, "already decided"):
            services.quarantine_lot(insp, self.supervisor, "Out of tolerance, reject lot", LATER)
        self.assertEqual(QuarantineRecord.objects.count(), 1)


class DecisionProblemsTests(ReviewTestCase):
    def test_explains_what_is_blocked(self):
        insp, _ = pdi_noctq_fail(self)
        problems = services.decision_problems(insp)
        self.assertIn("within tolerance", problems["approve"])
        self.assertEqual(problems["concession"], "")
        self.assertEqual(problems["quarantine"], "")

    def test_frozen_inspection_only_allows_quarantine(self):
        insp, _ = pdi_ctq_frozen(self)
        problems = services.decision_problems(insp)
        self.assertTrue(problems["approve"])
        self.assertIn("not allowed", problems["concession"])
        self.assertEqual(problems["quarantine"], "")
