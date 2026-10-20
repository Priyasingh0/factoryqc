from decimal import Decimal

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.checklists import services
from apps.checklists.exceptions import (
    InvalidTransition,
    NotAuthorized,
    TemplateLockedError,
    TemplateNotAvailable,
    TemplateValidationError,
)
from apps.checklists.models import ChecklistTemplate, ChecklistType, TemplateStatus
from apps.core.models import AuditLog

from .helpers import (
    RoleTestCase,
    force_status,
    make_dimension,
    make_part,
    make_pin,
    make_template,
)


class SubmitTests(RoleTestCase):
    def test_complete_draft_goes_to_pending_and_is_audited(self):
        template = make_template(self.engineer)
        services.submit_for_approval(template, self.engineer)
        template.refresh_from_db()
        self.assertEqual(template.status, TemplateStatus.PENDING_APPROVAL)
        entry = AuditLog.objects.get(model_name="checklists.ChecklistTemplate", field_name="status")
        self.assertEqual((entry.previous_value, entry.new_value), ("DRAFT", "PENDING_APPROVAL"))
        self.assertEqual(entry.user, self.engineer)

    def test_draft_without_drawing_is_rejected(self):
        template = make_template(self.engineer, drawing=False)
        with self.assertRaises(TemplateValidationError) as ctx:
            services.submit_for_approval(template, self.engineer)
        self.assertIn("drawing", str(ctx.exception).lower())
        template.refresh_from_db()
        self.assertEqual(template.status, TemplateStatus.DRAFT)

    def test_draft_without_fields_is_rejected(self):
        template = make_template(self.engineer, with_field=False)
        with self.assertRaises(TemplateValidationError) as ctx:
            services.submit_for_approval(template, self.engineer)
        self.assertIn("at least one inspection field", str(ctx.exception))

    def test_invalid_field_blocks_submission_and_is_named(self):
        template = make_template(self.engineer, with_field=False)
        make_dimension(template, label="Bore", required_gauge_type="")
        with self.assertRaises(TemplateValidationError) as ctx:
            services.submit_for_approval(template, self.engineer)
        self.assertIn("Bore", str(ctx.exception))

    def test_in_process_template_without_operation_is_rejected(self):
        template = make_template(self.engineer, checklist_type=ChecklistType.IN_PROCESS)
        with self.assertRaises(TemplateValidationError):
            services.submit_for_approval(template, self.engineer)

    def test_only_a_draft_can_be_submitted(self):
        template = make_template(self.engineer)
        services.submit_for_approval(template, self.engineer)
        with self.assertRaises(InvalidTransition):
            services.submit_for_approval(template, self.engineer)

    def test_inspector_cannot_submit(self):
        template = make_template(self.engineer)
        with self.assertRaises(NotAuthorized):
            services.submit_for_approval(template, self.inspector)


class ApproveRejectTests(RoleTestCase):
    def pending(self, **kwargs):
        template = make_template(self.engineer, **kwargs)
        return services.submit_for_approval(template, self.engineer)

    def test_quality_manager_releases_template(self):
        template = self.pending()
        services.approve_template(template, self.manager)
        template.refresh_from_db()
        self.assertEqual(template.status, TemplateStatus.RELEASED)
        self.assertEqual(template.approved_by, self.manager)
        self.assertIsNotNone(template.approved_at)
        entry = AuditLog.objects.get(action=AuditLog.Action.DECISION)
        self.assertEqual(entry.user, self.manager)
        self.assertEqual(entry.new_value, "RELEASED")

    def test_engineer_cannot_approve(self):
        template = self.pending()
        with self.assertRaises(NotAuthorized):
            services.approve_template(template, self.engineer2)

    def test_author_cannot_approve_own_template_even_with_permission(self):
        from django.contrib.auth.models import Permission

        author = make_user("qm2", Role.QUALITY_MANAGER)
        author.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="checklists", codename="change_checklisttemplate"
            )
        )
        author = type(author).objects.get(pk=author.pk)  # drop cached permissions
        template = make_template(author)
        services.submit_for_approval(template, author)
        with self.assertRaisesMessage(NotAuthorized, "cannot approve a template you created"):
            services.approve_template(template, author)
        template.refresh_from_db()
        self.assertEqual(template.status, TemplateStatus.PENDING_APPROVAL)

    def test_cannot_approve_a_draft(self):
        template = make_template(self.engineer)
        with self.assertRaises(InvalidTransition):
            services.approve_template(template, self.manager)

    def test_approving_new_revision_supersedes_old_one(self):
        old = self.pending()
        services.approve_template(old, self.manager)
        new = services.create_revision(old, self.engineer)
        services.submit_for_approval(new, self.engineer)
        # Old stays Released until the new revision is approved.
        old.refresh_from_db()
        self.assertEqual(old.status, TemplateStatus.RELEASED)
        services.approve_template(new, self.manager)
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(old.status, TemplateStatus.SUPERSEDED)
        self.assertEqual(new.status, TemplateStatus.RELEASED)
        self.assertTrue(
            AuditLog.objects.filter(object_id=str(old.pk), new_value="SUPERSEDED",
                                    remarks="Superseded by Rev B").exists()
        )

    def test_release_does_not_supersede_other_parts_or_operations(self):
        other_part = self.pending(part=make_part("P-200"))
        services.approve_template(other_part, self.manager)
        op20 = self.pending(checklist_type=ChecklistType.IN_PROCESS, operation_number=20)
        services.approve_template(op20, self.manager)
        op30 = self.pending(checklist_type=ChecklistType.IN_PROCESS, operation_number=30)
        services.approve_template(op30, self.manager)
        pdi = self.pending()
        services.approve_template(pdi, self.manager)
        self.assertEqual(
            ChecklistTemplate.objects.filter(status=TemplateStatus.RELEASED).count(), 4
        )

    def test_reject_returns_draft_with_reason_and_allows_editing(self):
        template = self.pending()
        services.reject_template(template, self.manager, "Tolerance on pin 2 is wrong")
        template.refresh_from_db()
        self.assertEqual(template.status, TemplateStatus.DRAFT)
        self.assertEqual(template.review_notes, "Tolerance on pin 2 is wrong")
        template.customer_agreement = "fixed"
        template.save()  # editable again
        entry = AuditLog.objects.get(action=AuditLog.Action.DECISION)
        self.assertIn("Tolerance on pin 2", entry.remarks)

    def test_reject_requires_a_reason(self):
        template = self.pending()
        with self.assertRaises(TemplateValidationError):
            services.reject_template(template, self.manager, "   ")
        template.refresh_from_db()
        self.assertEqual(template.status, TemplateStatus.PENDING_APPROVAL)

    def test_engineer_cannot_reject(self):
        with self.assertRaises(NotAuthorized):
            services.reject_template(self.pending(), self.engineer2, "no")

    def test_approval_clears_old_review_notes(self):
        template = self.pending()
        services.reject_template(template, self.manager, "fix it")
        services.submit_for_approval(template, self.engineer)
        services.approve_template(template, self.manager)
        template.refresh_from_db()
        self.assertEqual(template.review_notes, "")


class CreateRevisionTests(RoleTestCase):
    def released(self):
        template = make_template(self.engineer)
        pin = make_pin(template, 1, color="#ff8800")
        field = template.fields.get()
        field.pin = pin
        field.is_ctq = True
        field.save()
        make_dimension(template, sequence=2, label="Length", nominal=Decimal("100"))
        services.submit_for_approval(template, self.engineer)
        return services.approve_template(template, self.manager)

    def test_new_draft_copies_pins_fields_ctq_and_drawing(self):
        old = self.released()
        new = services.create_revision(old, self.engineer)
        self.assertEqual(new.revision, "B")
        self.assertEqual(new.status, TemplateStatus.DRAFT)
        self.assertEqual(new.created_by, self.engineer)
        self.assertEqual(new.drawing_pdf.name, old.drawing_pdf.name)
        self.assertEqual(new.fields.count(), 2)
        self.assertEqual(new.pins.count(), 1)
        copied = new.fields.get(sequence=1)
        self.assertTrue(copied.is_ctq)
        self.assertEqual(copied.nominal, Decimal("25.0000"))
        # The copied field points at the NEW template's pin, not the old one.
        self.assertEqual(copied.pin.template_id, new.pk)
        self.assertEqual(copied.pin.color, "#ff8800")
        self.assertEqual(old.fields.count(), 2)

    def test_new_revision_is_editable_while_old_stays_frozen(self):
        old = self.released()
        new = services.create_revision(old, self.engineer)
        field = new.fields.get(sequence=1)
        field.nominal = Decimal("26")
        field.save()
        old_field = old.fields.get(sequence=1)
        old_field.nominal = Decimal("26")
        with self.assertRaises(TemplateLockedError):
            old_field.save()

    def test_only_one_open_revision_at_a_time(self):
        old = self.released()
        services.create_revision(old, self.engineer)
        with self.assertRaises(InvalidTransition):
            services.create_revision(old, self.engineer)

    def test_only_released_templates_can_be_revised(self):
        draft = make_template(self.engineer)
        with self.assertRaises(InvalidTransition):
            services.create_revision(draft, self.engineer)

    def test_revision_letters_continue_after_superseded(self):
        old = self.released()
        rev_b = services.create_revision(old, self.engineer)
        services.submit_for_approval(rev_b, self.engineer)
        services.approve_template(rev_b, self.manager)
        rev_c = services.create_revision(rev_b, self.engineer)
        self.assertEqual(rev_c.revision, "C")

    def test_inspector_cannot_create_revision(self):
        with self.assertRaises(NotAuthorized):
            services.create_revision(self.released(), self.inspector)

    def test_creation_is_audited(self):
        old = self.released()
        new = services.create_revision(old, self.engineer)
        entry = AuditLog.objects.get(action=AuditLog.Action.CREATE, object_id=str(new.pk),
                                     model_name="checklists.ChecklistTemplate")
        self.assertIn("Rev A", entry.remarks)


class ActiveTemplateTests(RoleTestCase):
    def test_only_released_template_is_returned(self):
        template = make_template(self.engineer)
        part = template.part
        with self.assertRaises(TemplateNotAvailable):
            services.get_active_template(part, ChecklistType.PDI)
        services.submit_for_approval(template, self.engineer)
        with self.assertRaises(TemplateNotAvailable):
            services.get_active_template(part, ChecklistType.PDI)
        services.approve_template(template, self.manager)
        self.assertEqual(services.get_active_template(part, ChecklistType.PDI), template)

    def test_operation_specific_lookup(self):
        template = make_template(self.engineer, checklist_type=ChecklistType.FIRST_PIECE, operation_number=20)
        force_status(template, TemplateStatus.RELEASED, approver=self.manager)
        self.assertEqual(
            services.get_active_template(template.part, ChecklistType.FIRST_PIECE, 20), template
        )
        with self.assertRaises(TemplateNotAvailable):
            services.get_active_template(template.part, ChecklistType.FIRST_PIECE, 30)

    def test_assert_template_usable(self):
        template = make_template(self.engineer)
        with self.assertRaises(TemplateNotAvailable):
            services.assert_template_usable(template)
        force_status(template, TemplateStatus.RELEASED, approver=self.manager)
        services.assert_template_usable(template)
        force_status(template, TemplateStatus.SUPERSEDED)
        with self.assertRaises(TemplateNotAvailable):
            services.assert_template_usable(template)
