import shutil
import tempfile
from decimal import Decimal
from io import StringIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db.models import QuerySet
from django.test import TestCase, override_settings

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.checklists.models import (
    BlueprintPin,
    ChecklistTemplate,
    ChecklistType,
    FieldType,
    TemplateField,
    TemplateStatus,
)
from apps.plant.models import GaugeType, Part

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def pdf_upload(name="drawing.pdf"):
    return SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf")


class MediaTestCase(TestCase):
    """TestCase whose uploads go to a temporary folder, never your real media/."""

    @classmethod
    def setUpClass(cls):
        cls._media_dir = tempfile.mkdtemp()
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_dir)
        cls._media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._media_override.disable()
        shutil.rmtree(cls._media_dir, ignore_errors=True)


class RoleTestCase(MediaTestCase):
    """Creates the role groups and one user per role used by the workflow."""

    @classmethod
    def setUpTestData(cls):
        # Built once per class (password hashing is slow); each test gets its own copy.
        call_command("setup_roles", stdout=StringIO())
        cls.engineer = make_user("eng", Role.QUALITY_ENGINEER)
        cls.engineer2 = make_user("eng2", Role.QUALITY_ENGINEER)
        cls.manager = make_user("qm", Role.QUALITY_MANAGER)
        cls.inspector = make_user("insp", Role.QA_INSPECTOR)


def make_part(number="P-100"):
    part, _ = Part.objects.get_or_create(part_number=number, defaults={"name": f"Part {number}"})
    return part


def make_template(
    created_by,
    part=None,
    checklist_type=ChecklistType.PDI,
    operation_number=None,
    revision="A",
    drawing=True,
    with_field=True,
):
    template = ChecklistTemplate(
        part=part or make_part(),
        checklist_type=checklist_type,
        operation_number=operation_number,
        revision=revision,
        created_by=created_by,
    )
    if drawing:
        template.drawing_pdf = pdf_upload()
    template.save()
    if with_field:
        make_dimension(template)
    return template


def make_dimension(template, sequence=1, **overrides):
    values = dict(
        template=template, sequence=sequence, label="Shaft diameter",
        field_type=FieldType.DIMENSION, nominal=Decimal("25.0000"),
        upper_tol=Decimal("0.0500"), lower_tol=Decimal("0.0200"), unit="mm",
        required_gauge_type=GaugeType.MICROMETER,
    )
    values.update(overrides)
    return TemplateField.objects.create(**values)


def make_pin(template, number=1, **overrides):
    values = dict(template=template, number=number, x_pct=Decimal("10.5"), y_pct=Decimal("20"))
    values.update(overrides)
    return BlueprintPin.objects.create(**values)


def force_status(template, status, approver=None):
    """Test shortcut: set a status bypassing the workflow (base QuerySet.update)."""
    from django.utils import timezone

    extra = {}
    if status == TemplateStatus.RELEASED:
        extra = {"approved_by": approver, "approved_at": timezone.now()}
    QuerySet.update(ChecklistTemplate.objects.filter(pk=template.pk), status=status, **extra)
    template.refresh_from_db()
    return template
