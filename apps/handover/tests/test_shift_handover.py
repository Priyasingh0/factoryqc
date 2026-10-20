from django.db import IntegrityError
from django.db.models import QuerySet
from unittest import mock

from apps.core.models import AuditLog, Notification
from apps.handover import services
from apps.handover.exceptions import (
    HandoverIncomplete, InvalidTransition, NotAuthorized, RecordLocked,
)
from apps.handover.models import (
    HandoverAcknowledgement, HandoverSignoff, MachineHealth, ShiftHandover, ToolWear,
)
from apps.inspections.models import Inspection, InspectionStatus
from apps.plant.tests.helpers import make_machine

from .base import EVENING, HANDOVER_AT, LONG, HandoverTestCase


class CreateHandoverTests(HandoverTestCase):
    def test_form_stores_system_snapshot_user_and_time(self):
        traveler, machine, op = self.running_job(parts=2)
        h = self.hand_over(machine, scrap_count=1)
        self.assertEqual(h.parts_produced, 2)
        self.assertEqual(h.scrap_count, 1)
        self.assertEqual(h.outgoing_by, self.operator)
        self.assertEqual(h.created_at, HANDOVER_AT)
        self.assertEqual(len(h.open_jobs), 1)
        self.assertEqual(h.open_jobs[0]["work_order"], traveler.work_order.number)
        self.assertEqual(h.open_jobs[0]["parts_produced"], 2)
        self.assertEqual(h.status, "AWAITING_SIGNOFF")

    def test_creation_is_audited_with_the_user(self):
        _, machine, _ = self.running_job()
        h = self.hand_over(machine)
        row = self.audit(model_name="handover.ShiftHandover", object_id=str(h.pk)).get()
        self.assertEqual(row.action, AuditLog.Action.CREATE)
        self.assertEqual(row.user, self.operator)
        self.assertIn("scrap 0", row.remarks)

    def test_idle_machine_snapshot_is_empty(self):
        machine = make_machine("CNC-77")
        h = self.hand_over(machine, job_status_notes="")
        self.assertEqual((h.parts_produced, h.open_jobs, h.open_quality_alerts, h.pending_pdi_reviews), (0, [], 0, 0))

    def test_open_alerts_and_pending_reviews_come_from_the_system(self):
        _, machine, op = self.running_job()
        inspection = Inspection.objects.get(traveler_operation=op)  # the first piece, SUBMITTED, awaiting a supervisor
        self.assertEqual(services.machine_snapshot(machine, HANDOVER_AT)["pending_pdi_reviews"], 1)
        QuerySet.update(Inspection.objects.filter(pk=inspection.pk), status=InspectionStatus.FROZEN)
        snap = services.machine_snapshot(machine, HANDOVER_AT)
        self.assertEqual((snap["open_quality_alerts"], snap["pending_pdi_reviews"]), (1, 0))

    def test_only_operators_and_supervisors_complete_the_form(self):
        _, machine, _ = self.running_job()
        for user in (self.qa, self.dock, self.warehouse, self.lead, self.maint, self.plant_manager):
            with self.assertRaises(NotAuthorized, msg=user.role):
                self.hand_over(machine, user=user)
        self.assertFalse(ShiftHandover.objects.exists())
        self.hand_over(machine, user=self.sup)

    def test_scrap_count_must_be_a_whole_number_not_below_zero(self):
        _, machine, _ = self.running_job()
        for bad in (-1, True, "3", 2.5, None):
            with self.assertRaises(HandoverIncomplete, msg=repr(bad)):
                self.hand_over(machine, scrap_count=bad)
        self.assertFalse(ShiftHandover.objects.exists())

    def test_worn_tools_need_a_description(self):
        _, machine, _ = self.running_job()
        for wear in (ToolWear.WATCH, ToolWear.CHANGE_NOW):
            with self.assertRaisesMessage(HandoverIncomplete, "tool wear"):
                self.hand_over(machine, tool_wear=wear, tool_wear_notes="short")
        self.hand_over(machine, tool_wear=ToolWear.WATCH, tool_wear_notes=LONG)

    def test_machine_problems_need_a_description(self):
        _, machine, _ = self.running_job()
        with self.assertRaisesMessage(HandoverIncomplete, "machine problem"):
            self.hand_over(machine, machine_health=MachineHealth.DEGRADED, machine_health_notes="   ")
        self.hand_over(machine, machine_health=MachineHealth.DOWN, machine_health_notes=LONG)

    def test_a_running_job_needs_a_status_note_for_the_next_shift(self):
        _, machine, _ = self.running_job()
        with self.assertRaisesMessage(HandoverIncomplete, "still running"):
            self.hand_over(machine, job_status_notes="too short")

    def test_choices_must_be_valid_and_every_problem_is_reported_together(self):
        _, machine, _ = self.running_job()
        with self.assertRaises(HandoverIncomplete) as ctx:
            self.hand_over(machine, scrap_count=-5, tool_wear="BROKEN", machine_health="???", job_status_notes="")
        message = str(ctx.exception)
        for part in ("scrap count", "tool wear status", "machine health", "still running"):
            self.assertIn(part, message)

    def test_one_handover_per_machine_per_shift(self):
        _, machine, _ = self.running_job()
        self.hand_over(machine)
        with self.assertRaisesMessage(InvalidTransition, "already recorded"):
            self.hand_over(machine, user=self.sup)
        self.assertEqual(ShiftHandover.objects.count(), 1)

    def test_database_enforces_one_handover_per_machine_per_shift(self):
        _, machine, _ = self.running_job()
        first = self.hand_over(machine)
        with self.assertRaises(IntegrityError):
            ShiftHandover.objects.create(
                machine=machine, shift_date=first.shift_date, shift=first.shift, outgoing_by=self.sup,
                parts_produced=0, open_quality_alerts=0, pending_pdi_reviews=0, scrap_count=0,
                tool_wear=ToolWear.OK, machine_health=MachineHealth.GOOD,
            )

    def test_inactive_machine_cannot_be_handed_over(self):
        machine = make_machine("CNC-88", active=False)
        with self.assertRaisesMessage(HandoverIncomplete, "inactive"):
            self.hand_over(machine, job_status_notes="")

    def test_a_race_that_loses_to_the_unique_constraint_is_reported_cleanly(self):
        _, machine, _ = self.running_job()
        with mock.patch.object(ShiftHandover.objects, "filter", return_value=mock.Mock(exists=lambda: False)):
            self.hand_over(machine)
            with self.assertRaisesMessage(InvalidTransition, "just recorded"):
                self.hand_over(machine, user=self.sup)

    def test_supervisors_are_asked_to_countersign(self):
        _, machine, _ = self.running_job()
        h = self.hand_over(machine)
        notes = Notification.objects.filter(related_model="handover.ShiftHandover", related_object_id=str(h.pk))
        self.assertEqual({n.recipient for n in notes}, {self.sup, self.sup2})
        self.assertTrue(all(n.recipient_role == "SHIFT_SUPERVISOR" and "sign-off" in n.subject for n in notes))

    def test_maintenance_is_alerted_only_when_the_machine_or_tooling_needs_it(self):
        _, machine, _ = self.running_job()
        self.hand_over(machine, tool_wear=ToolWear.WATCH, tool_wear_notes=LONG)
        self.assertFalse(Notification.objects.filter(recipient_role="MAINTENANCE_SUPERVISOR").exists())
        other = make_machine("CNC-55")
        self.hand_over(other, machine_health=MachineHealth.DOWN, machine_health_notes=LONG, job_status_notes="")
        self.assertTrue(Notification.objects.filter(recipient_role="MAINTENANCE_SUPERVISOR").exists())
        third = make_machine("CNC-56")
        self.hand_over(third, tool_wear=ToolWear.CHANGE_NOW, tool_wear_notes=LONG, job_status_notes="")
        self.assertEqual(
            Notification.objects.filter(recipient_role="MAINTENANCE_SUPERVISOR").values("related_object_id").distinct().count(), 2)


class SignoffTests(HandoverTestCase):
    def setUp(self):
        _, self.machine, _ = self.running_job()
        self.handover = self.hand_over(self.machine)

    def test_a_supervisor_countersigns_with_user_and_time(self):
        services.sign_handover(self.handover, self.sup, "Checked with the operator", HANDOVER_AT)
        sign = HandoverSignoff.objects.get()
        self.assertEqual((sign.signed_by, sign.signed_at, sign.remarks), (self.sup, HANDOVER_AT, "Checked with the operator"))
        self.assertEqual(ShiftHandover.objects.get(pk=self.handover.pk).status, "AWAITING_ACK")
        row = self.audit(model_name="handover.ShiftHandover", action=AuditLog.Action.SIGN_OFF).get()
        self.assertEqual(row.user, self.sup)

    def test_operators_and_other_roles_cannot_countersign(self):
        for user in (self.operator, self.operator2, self.qa, self.lead, self.manager, self.plant_manager):
            with self.assertRaises(NotAuthorized, msg=user.role):
                services.sign_handover(self.handover, user, "", HANDOVER_AT)
        self.assertFalse(HandoverSignoff.objects.exists())

    def test_the_author_cannot_countersign_their_own_form(self):
        own = self.hand_over(make_machine("CNC-90"), user=self.sup, job_status_notes="")
        with self.assertRaisesMessage(InvalidTransition, "another supervisor"):
            services.sign_handover(own, self.sup, "", HANDOVER_AT)
        services.sign_handover(own, self.sup2, "", HANDOVER_AT)

    def test_cannot_countersign_twice(self):
        services.sign_handover(self.handover, self.sup, "", HANDOVER_AT)
        with self.assertRaisesMessage(InvalidTransition, "already signed"):
            services.sign_handover(self.handover, self.sup2, "", HANDOVER_AT)
        self.assertEqual(HandoverSignoff.objects.count(), 1)

    def test_operators_are_told_when_it_is_ready_to_acknowledge(self):
        services.sign_handover(self.handover, self.sup, "", HANDOVER_AT)
        notes = Notification.objects.filter(recipient_role="MACHINE_OPERATOR")
        self.assertEqual({n.recipient for n in notes}, {self.operator, self.operator2})
        self.assertTrue(all("ready to acknowledge" in n.subject for n in notes))


class AcknowledgeTests(HandoverTestCase):
    def setUp(self):
        _, self.machine, _ = self.running_job()
        self.handover = self.hand_over(self.machine)

    def test_acknowledgement_needs_the_supervisor_countersign_first(self):
        with self.assertRaisesMessage(InvalidTransition, "not countersigned"):
            services.acknowledge_handover(self.handover, self.operator2, "", EVENING)
        self.assertFalse(HandoverAcknowledgement.objects.exists())
        row = self.audit(field_name="acknowledge", new_value="BLOCKED").get()
        self.assertEqual(row.user, self.operator2)

    def test_incoming_worker_acknowledges_with_user_and_time(self):
        services.sign_handover(self.handover, self.sup, "", HANDOVER_AT)
        services.acknowledge_handover(self.handover, self.operator2, "Reviewed, will watch the spindle", EVENING)
        ack = HandoverAcknowledgement.objects.get()
        self.assertEqual((ack.acknowledged_by, ack.acknowledged_at), (self.operator2, EVENING))
        self.assertEqual(ShiftHandover.objects.get(pk=self.handover.pk).status, "ACKNOWLEDGED")
        self.assertEqual(self.audit(model_name="handover.ShiftHandover", action=AuditLog.Action.SIGN_OFF).count(), 2)

    def test_the_people_who_handed_over_cannot_acknowledge(self):
        services.sign_handover(self.handover, self.sup, "", HANDOVER_AT)
        for user in (self.operator, self.sup):
            with self.assertRaisesMessage(InvalidTransition, "someone other"):
                services.acknowledge_handover(self.handover, user, "", EVENING)
        self.assertFalse(HandoverAcknowledgement.objects.exists())

    def test_only_operators_and_supervisors_can_acknowledge(self):
        services.sign_handover(self.handover, self.sup, "", HANDOVER_AT)
        for user in (self.qa, self.lead, self.dock, self.warehouse, self.maint, self.manager):
            with self.assertRaises(NotAuthorized, msg=user.role):
                services.acknowledge_handover(self.handover, user, "", EVENING)

    def test_cannot_acknowledge_twice(self):
        services.sign_handover(self.handover, self.sup, "", HANDOVER_AT)
        services.acknowledge_handover(self.handover, self.operator2, "", EVENING)
        with self.assertRaisesMessage(InvalidTransition, "already been acknowledged"):
            services.acknowledge_handover(self.handover, self.sup2, "", EVENING)
        self.assertEqual(HandoverAcknowledgement.objects.count(), 1)

    def test_inner_recheck_stops_a_second_acknowledgement_that_slipped_past_the_first_check(self):
        services.sign_handover(self.handover, self.sup, "", HANDOVER_AT)
        services.acknowledge_handover(self.handover, self.operator2, "", EVENING)
        with mock.patch.object(services, "_check_acknowledgeable", side_effect=[None, InvalidTransition("late")]):
            with self.assertRaises(InvalidTransition):
                services.acknowledge_handover(self.handover, self.sup2, "", EVENING)
        self.assertEqual(HandoverAcknowledgement.objects.count(), 1)


class ImmutabilityTests(HandoverTestCase):
    def setUp(self):
        _, machine, _ = self.running_job()
        self.handover = self.acknowledged(machine)

    def rows(self):
        return [self.handover, self.handover.signoff, self.handover.acknowledgement]

    def test_saved_records_cannot_be_changed(self):
        for row in self.rows():
            with self.assertRaises(RecordLocked, msg=type(row).__name__):
                row.save()

    def test_a_scrap_count_cannot_be_edited_after_the_fact(self):
        stored = ShiftHandover.objects.get(pk=self.handover.pk)
        stored.scrap_count = 99
        with self.assertRaises(RecordLocked):
            stored.save()
        self.assertEqual(ShiftHandover.objects.get(pk=self.handover.pk).scrap_count, 0)

    def test_records_cannot_be_deleted(self):
        for row in self.rows():
            with self.assertRaises(RecordLocked, msg=type(row).__name__):
                row.delete()
        for model in (ShiftHandover, HandoverSignoff, HandoverAcknowledgement):
            with self.assertRaises(RecordLocked):
                model.objects.all().delete()
            with self.assertRaises(RecordLocked):
                model.objects.all().update(pk=1)
            with self.assertRaises(RecordLocked):
                model.objects.bulk_update([], ["pk"])
        self.assertEqual(ShiftHandover.objects.count(), 1)

    def test_even_a_superuser_cannot_add_change_or_delete_in_the_admin(self):
        from apps.accounts.models import User

        User.objects.create_superuser("root", "r@x.com", "pw-12345-xyz", employee_id="E-root")
        self.client.login(username="root", password="pw-12345-xyz")
        for name in ("shifthandover", "handoversignoff", "handoveracknowledgement"):
            base = f"/admin/handover/{name}/"
            self.assertEqual(self.client.get(base + "add/").status_code, 403, name)
            self.assertEqual(self.client.post(base + "1/change/", {}).status_code, 403, name)
            self.assertEqual(self.client.post(base + "1/delete/", {"post": "yes"}).status_code, 403, name)
        self.assertEqual(ShiftHandover.objects.count(), 1)
