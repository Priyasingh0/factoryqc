"""Scenario builders for the materials tests, also used by other apps' helpers to give work orders real stock."""

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.materials import services
from apps.materials.models import (
    GradeProperty, IntakeStatus, MaterialGrade, MaterialIssue, RawMaterialIntake, Supplier,
)
from apps.checklists.tests.helpers import MediaTestCase  # noqa: F401  (re-exported for the test cases)
from apps.production.tests.helpers import NOW

LATER = NOW + timedelta(hours=2)
PDF = b"%PDF-1.4 mill test certificate"


def pdf(name="mtc.pdf", data=PDF):
    return SimpleUploadedFile(name, data, "application/pdf")


def make_supplier(code="SUP-1", name="Steel Mills Ltd", active=True):
    return Supplier.objects.get_or_create(code=code, defaults={"name": name, "is_active": active})[0]


def make_grade(code="EN8", lab=False, with_specs=True):
    grade, created = MaterialGrade.objects.get_or_create(code=code, defaults={"requires_lab_testing": lab})
    if created and with_specs:
        GradeProperty.objects.create(
            grade=grade, name="Carbon", kind="CHEMICAL", unit="%", min_value=Decimal("0.36"), max_value=Decimal("0.44"))
        GradeProperty.objects.create(
            grade=grade, name="Tensile strength", kind="MECHANICAL", unit="MPa", min_value=Decimal("700"))
    return grade


def good_readings(grade):
    values = {"Carbon": "0.40", "Tensile strength": "750"}
    return {p.pk: values.get(p.name, "1") for p in grade.properties.all()}


def bad_readings(grade):
    values = {"Carbon": "0.60", "Tensile strength": "750"}
    return {p.pk: values.get(p.name, "1") for p in grade.properties.all()}


def cleared_lot_for(work_order, user, quantity=None):
    """Give a work order a cleared lot of its heat number and issue it (ORM only; used by other apps' helpers)."""
    supplier = make_supplier("SUP-T", "Test Steel")
    grade = make_grade("T-GRADE", with_specs=False)
    heat = services.normalise_heat(work_order.heat_number)
    intake, _ = RawMaterialIntake.objects.get_or_create(
        supplier=supplier, heat_number=heat, po_number="PO-T",
        defaults=dict(grade=grade, quantity=Decimal("100000"), lab_required=False, mtc_file="mtc/test.pdf",
                      mtc_sha256="0" * 64, received_by=user, status=IntakeStatus.CLEARED),
    )
    MaterialIssue.objects.create(
        intake=intake, work_order=work_order, quantity=quantity or Decimal(work_order.quantity), issued_by=user)
    return intake
