from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.checklists.exceptions import InvalidTransition, TemplateLockedError
from apps.checklists.models import (
    BlueprintPin,
    ChecklistTemplate,
    ChecklistType,
    FieldType,
    TemplateField,
    TemplateStatus,
    next_revision,
)
from apps.checklists.validators import validate_pdf
from apps.plant.models import GaugeType

from .helpers import MediaTestCase, force_status, make_dimension, make_part, make_pin, make_template


def field(field_type, **kwargs):
    """Unsaved TemplateField for pure evaluation tests."""
    return TemplateField(field_type=field_type, **kwargs)


D = Decimal


class EvaluateTests(SimpleTestCase):
    """PRD 4.2 / 5.3: instant green/red against drawing limits. Limits are inclusive."""

    def dimension(self):
        # Target 25.00, upper +0.05, lower -0.02 -> 24.98 .. 25.05
        return field(FieldType.DIMENSION, nominal=D("25.00"), upper_tol=D("0.05"), lower_tol=D("0.02"))

    def test_dimension_inside_limits_passes(self):
        self.assertTrue(self.dimension().evaluate(D("25.00")))
        self.assertTrue(self.dimension().evaluate(D("25.03")))

    def test_dimension_exactly_on_limits_passes(self):
        self.assertTrue(self.dimension().evaluate(D("25.05")))
        self.assertTrue(self.dimension().evaluate(D("24.98")))

    def test_dimension_just_outside_limits_fails(self):
        self.assertFalse(self.dimension().evaluate(D("25.0501")))
        self.assertFalse(self.dimension().evaluate(D("24.9799")))

    def test_tolerances_are_asymmetric(self):
        f = self.dimension()
        self.assertEqual(f.upper_limit, D("25.05"))
        self.assertEqual(f.lower_limit, D("24.98"))
        self.assertFalse(f.evaluate(D("24.97")))  # would pass with a symmetric +/-0.05

    def test_accepts_float_int_and_string_input(self):
        f = self.dimension()
        self.assertTrue(f.evaluate(25))
        self.assertTrue(f.evaluate("25.01"))
        self.assertFalse(f.evaluate(25.06))

    def test_max_limit(self):
        f = field(FieldType.MAX, max_limit=D("0.8"))  # Surface roughness Ra 0.8 MAX
        self.assertTrue(f.evaluate(D("0.8")))
        self.assertTrue(f.evaluate(D("0.1")))
        self.assertFalse(f.evaluate(D("0.81")))
        self.assertIsNone(f.lower_limit)

    def test_min_limit(self):
        f = field(FieldType.MIN, min_limit=D("10"))
        self.assertTrue(f.evaluate(D("10")))
        self.assertFalse(f.evaluate(D("9.999")))
        self.assertIsNone(f.upper_limit)

    def test_visual_pass_fail(self):
        f = field(FieldType.VISUAL)
        self.assertTrue(f.evaluate(True))
        self.assertFalse(f.evaluate(False))

    def test_visual_rejects_numbers_and_numeric_rejects_bool(self):
        with self.assertRaises(TypeError):
            field(FieldType.VISUAL).evaluate(1)
        with self.assertRaises(TypeError):
            self.dimension().evaluate(True)

    def test_spec_display(self):
        self.assertEqual(
            field(FieldType.DIMENSION, nominal=D("25"), upper_tol=D("0.05"), lower_tol=D("0.02"),
                  unit="mm").spec_display,
            "25.0000 (+0.0500 / -0.0200) mm",
        )
        self.assertEqual(field(FieldType.MAX, max_limit=D("0.8"), unit="Ra").spec_display, "0.8000 Ra max")
        self.assertEqual(field(FieldType.VISUAL).spec_display, "Pass / Fail")


class NextRevisionTests(SimpleTestCase):
    def test_sequence(self):
        self.assertEqual(next_revision("A"), "B")
        self.assertEqual(next_revision("Y"), "Z")
        self.assertEqual(next_revision("Z"), "AA")
        self.assertEqual(next_revision("AZ"), "BA")
        self.assertEqual(next_revision("ZZ"), "AAA")


class PdfValidatorTests(SimpleTestCase):
    def upload(self, name, content):
        return SimpleUploadedFile(name, content)

    def test_real_pdf_is_accepted(self):
        validate_pdf(self.upload("a.pdf", b"%PDF-1.7 data"))

    def test_wrong_extension_rejected(self):
        with self.assertRaises(ValidationError):
            validate_pdf(self.upload("a.png", b"%PDF-1.7 data"))

    def test_renamed_non_pdf_rejected(self):
        with self.assertRaises(ValidationError):
            validate_pdf(self.upload("a.pdf", b"MZ\x90\x00 not a pdf"))

    def test_oversized_file_rejected(self):
        big = self.upload("a.pdf", b"%PDF-" + b"0" * (20 * 1024 * 1024))
        with self.assertRaises(ValidationError):
            validate_pdf(big)


class FieldValidationTests(MediaTestCase):
    def setUp(self):
        self.user = make_user("eng", Role.QUALITY_ENGINEER)
        self.template = make_template(self.user, with_field=False)

    def build(self, **kwargs):
        values = dict(
            template=self.template, sequence=1, label="x", field_type=FieldType.DIMENSION,
            nominal=D("1"), upper_tol=D("0.1"), lower_tol=D("0.1"), unit="mm",
            required_gauge_type=GaugeType.CALIPER,
        )
        values.update(kwargs)
        return TemplateField(**values)

    def assertInvalid(self, instance, *field_names):
        with self.assertRaises(ValidationError) as ctx:
            instance.full_clean()
        for name in field_names:
            self.assertIn(name, ctx.exception.message_dict)

    def test_valid_dimension(self):
        self.build().full_clean()

    def test_dimension_needs_all_three_numbers(self):
        self.assertInvalid(self.build(nominal=None), "nominal")
        self.assertInvalid(self.build(upper_tol=None), "upper_tol")
        self.assertInvalid(self.build(lower_tol=None), "lower_tol")

    def test_tolerances_must_be_positive_sizes(self):
        self.assertInvalid(self.build(lower_tol=D("-0.02")), "lower_tol")

    def test_max_field_cannot_carry_dimension_numbers(self):
        self.assertInvalid(
            self.build(field_type=FieldType.MAX, max_limit=D("0.8"), nominal=D("1"),
                       upper_tol=None, lower_tol=None),
            "nominal",
        )

    def test_max_and_min_need_their_limit(self):
        base = dict(nominal=None, upper_tol=None, lower_tol=None)
        self.assertInvalid(self.build(field_type=FieldType.MAX, **base), "max_limit")
        self.assertInvalid(self.build(field_type=FieldType.MIN, **base), "min_limit")
        self.build(field_type=FieldType.MIN, min_limit=D("5"), **base).full_clean()

    def test_measured_field_needs_tool_type_and_unit(self):
        self.assertInvalid(self.build(required_gauge_type=""), "required_gauge_type")
        self.assertInvalid(self.build(unit=""), "unit")

    def test_visual_field_has_no_numbers_tool_or_unit(self):
        base = dict(field_type=FieldType.VISUAL, nominal=None, upper_tol=None, lower_tol=None,
                    unit="", required_gauge_type="")
        self.build(**base).full_clean()
        self.assertInvalid(self.build(**{**base, "required_gauge_type": GaugeType.CALIPER}),
                           "required_gauge_type")

    def test_pin_must_belong_to_same_template(self):
        other = make_template(self.user, part=make_part("P-200"), with_field=False)
        foreign_pin = make_pin(other)
        self.assertInvalid(self.build(pin=foreign_pin), "pin")
        own_pin = make_pin(self.template)
        self.build(pin=own_pin).full_clean()

    def test_ctq_flag_defaults_false_and_can_be_set(self):
        self.assertFalse(self.build().is_ctq)
        self.assertTrue(self.build(is_ctq=True).is_ctq)


class PinValidationTests(MediaTestCase):
    def setUp(self):
        self.user = make_user("eng", Role.QUALITY_ENGINEER)
        self.template = make_template(self.user, with_field=False)

    def pin(self, **kwargs):
        values = dict(template=self.template, number=1, x_pct=D("50"), y_pct=D("50"))
        values.update(kwargs)
        return BlueprintPin(**values)

    def test_percentages_must_be_0_to_100(self):
        self.pin().full_clean()
        for bad in (D("-1"), D("100.01")):
            with self.assertRaises(ValidationError):
                self.pin(x_pct=bad).full_clean()

    def test_colour_must_be_hex(self):
        with self.assertRaises(ValidationError):
            self.pin(color="blue").full_clean()
        self.pin(color="#FF8800").full_clean()

    def test_pin_numbers_are_unique_per_template(self):
        make_pin(self.template, 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_pin(self.template, 1)
        make_pin(self.template, 2)


class TemplateRulesTests(MediaTestCase):
    def setUp(self):
        self.user = make_user("eng", Role.QUALITY_ENGINEER)
        self.qm = make_user("qm", Role.QUALITY_MANAGER)

    def test_in_process_and_first_piece_need_an_operation(self):
        for kind in (ChecklistType.IN_PROCESS, ChecklistType.FIRST_PIECE):
            template = ChecklistTemplate(part=make_part(), checklist_type=kind, created_by=self.user)
            with self.assertRaises(ValidationError) as ctx:
                template.full_clean(exclude=["drawing_pdf"])
            self.assertIn("operation_number", ctx.exception.message_dict)

    def test_pdi_does_not_need_an_operation(self):
        template = ChecklistTemplate(part=make_part(), checklist_type=ChecklistType.PDI,
                                     created_by=self.user)
        template.full_clean(exclude=["drawing_pdf"])

    def test_revision_must_be_capital_letters(self):
        template = ChecklistTemplate(part=make_part(), checklist_type=ChecklistType.PDI,
                                     revision="1", created_by=self.user)
        with self.assertRaises(ValidationError):
            template.full_clean(exclude=["drawing_pdf"])

    def test_same_revision_cannot_repeat_for_part_and_type(self):
        make_template(self.user, with_field=False)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_template(self.user, with_field=False)

    def test_same_revision_allowed_for_different_operations(self):
        make_template(self.user, checklist_type=ChecklistType.IN_PROCESS, operation_number=20, with_field=False)
        make_template(self.user, checklist_type=ChecklistType.IN_PROCESS, operation_number=30, with_field=False)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_template(self.user, checklist_type=ChecklistType.IN_PROCESS, operation_number=30,
                          with_field=False)


class DraftOnlyEditingTests(MediaTestCase):
    """PRD 4.1 / 12: templates cannot be altered once released."""

    def setUp(self):
        self.user = make_user("eng", Role.QUALITY_ENGINEER)
        self.qm = make_user("qm", Role.QUALITY_MANAGER)
        self.template = make_template(self.user)
        self.field = self.template.fields.get()
        self.pin = make_pin(self.template)

    def lock(self, status=TemplateStatus.RELEASED):
        force_status(self.template, status, approver=self.qm)

    def test_draft_can_be_edited(self):
        self.template.customer_agreement = "QA-17"
        self.template.save()
        self.field.label = "Renamed"
        self.field.save()
        make_dimension(self.template, sequence=2)

    def test_released_template_cannot_be_saved_with_changes(self):
        self.lock()
        self.template.customer_agreement = "tampered"
        with self.assertRaises(TemplateLockedError):
            self.template.save()
        with self.assertRaises(TemplateLockedError):
            self.template.save(update_fields=["customer_agreement"])

    def test_pending_template_is_also_locked(self):
        self.lock(TemplateStatus.PENDING_APPROVAL)
        self.field.label = "x"
        with self.assertRaises(TemplateLockedError):
            self.field.save()

    def test_released_template_fields_and_pins_are_frozen(self):
        self.lock()
        self.field.nominal = D("99")
        with self.assertRaises(TemplateLockedError):
            self.field.save()
        with self.assertRaises(TemplateLockedError):
            self.field.delete()
        with self.assertRaises(TemplateLockedError):
            make_dimension(self.template, sequence=2)
        self.pin.x_pct = D("1")
        with self.assertRaises(TemplateLockedError):
            self.pin.save()
        with self.assertRaises(TemplateLockedError):
            self.pin.delete()
        with self.assertRaises(TemplateLockedError):
            make_pin(self.template, 2)
        self.field.refresh_from_db()
        self.assertEqual(self.field.nominal, D("25.0000"))

    def test_bulk_operations_are_blocked_on_locked_templates(self):
        self.lock()
        with self.assertRaises(TemplateLockedError):
            TemplateField.objects.filter(template=self.template).update(nominal=D("1"))
        with self.assertRaises(TemplateLockedError):
            TemplateField.objects.filter(template=self.template).delete()
        with self.assertRaises(TemplateLockedError):
            self.template.fields.all().delete()
        with self.assertRaises(TemplateLockedError):
            ChecklistTemplate.objects.filter(pk=self.template.pk).update(revision="Z")
        with self.assertRaises(TemplateLockedError):
            ChecklistTemplate.objects.filter(pk=self.template.pk).delete()
        self.assertEqual(TemplateField.objects.count(), 1)

    def test_bulk_operations_work_on_drafts(self):
        TemplateField.objects.filter(template=self.template).update(label="bulk")
        self.assertEqual(TemplateField.objects.get().label, "bulk")

    def test_only_draft_can_be_deleted(self):
        self.lock()
        with self.assertRaises(TemplateLockedError):
            self.template.delete()
        self.assertTrue(ChecklistTemplate.objects.filter(pk=self.template.pk).exists())

    def test_draft_delete_removes_fields_and_pins(self):
        self.template.delete()
        self.assertEqual(TemplateField.objects.count(), 0)
        self.assertEqual(BlueprintPin.objects.count(), 0)

    def test_field_cannot_be_moved_to_another_template(self):
        other = make_template(self.user, part=make_part("P-200"), with_field=False)
        self.field.template = other
        with self.assertRaises(TemplateLockedError):
            self.field.save()


class TransitionGuardTests(MediaTestCase):
    def setUp(self):
        self.user = make_user("eng", Role.QUALITY_ENGINEER)
        self.qm = make_user("qm", Role.QUALITY_MANAGER)
        self.template = make_template(self.user)

    def test_cannot_jump_from_draft_to_released(self):
        self.template.status = TemplateStatus.RELEASED
        with self.assertRaises(InvalidTransition):
            self.template.save(update_fields=["status"])

    def test_release_requires_approver_and_time(self):
        force_status(self.template, TemplateStatus.PENDING_APPROVAL)
        self.template.status = TemplateStatus.RELEASED
        with self.assertRaises(InvalidTransition):
            self.template.save(update_fields=["status"])

    def test_superseded_is_final(self):
        force_status(self.template, TemplateStatus.SUPERSEDED)
        self.template.status = TemplateStatus.DRAFT
        with self.assertRaises(InvalidTransition):
            self.template.save(update_fields=["status"])
