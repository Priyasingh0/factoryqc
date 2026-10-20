from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.inspections import services as inspections
from apps.inspections.models import InspectionStatus, Measurement
from apps.inspections.tests.helpers import (
    NOW, InspectionTestCase, begin_first_piece, begin_pdi, enter, enter_good_first_piece,
    field_named, lot_in_setup, lot_ready_for_pdi, tiny_png,
)
from apps.review import services

LATER = NOW + timedelta(hours=5)
LONG_TEXT = "Dimension is out by a hair; fit and function unaffected per drawing note."

__all__ = [
    "NOW", "LATER", "LONG_TEXT", "ReviewTestCase", "pdi_pass", "pdi_noctq_fail", "pdi_ctq_frozen",
    "pdi_ctq_failure_submitted", "first_piece_submitted", "first_piece_failed", "pdf_upload",
]


class ReviewTestCase(InspectionTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.auth_sup = make_user("sup_auth", Role.SHIFT_SUPERVISOR, can_grant_concession=True)


GOOD = {"Shaft diameter": ("25.0000", "MIC-1"), "Bore": ("10.0000", "VC-104"),
        "Roughness Ra": ("0.4000", "RT-9"), "Coating applied": (True, "")}


def _begin(case):
    traveler, station = lot_ready_for_pdi(case)
    return traveler, begin_pdi(case, traveler, station)


def _photos(measurement, user, n=2):
    for i in range(n):
        inspections.add_defect_photo(
            measurement, SimpleUploadedFile(f"p{i}.png", tiny_png(bytes([i + 1])), "image/png"), user, NOW)


def pdi_pass(case):
    """Submitted PDI with every reading in tolerance. Returns (inspection, traveler)."""
    traveler, insp = _begin(case)
    for label, (value, gauge) in GOOD.items():
        enter(case, insp, label, value, gauge=gauge)
    return inspections.submit_inspection(insp, case.qa, now=NOW), traveler


def pdi_noctq_fail(case):
    """Submitted PDI whose only failure is the non-CTQ Roughness Ra (with its 2 photos)."""
    traveler, insp = _begin(case)
    for label, (value, gauge) in GOOD.items():
        if label != "Roughness Ra":
            enter(case, insp, label, value, gauge=gauge)
    m = enter(case, insp, "Roughness Ra", "0.9", gauge="RT-9")
    _photos(m, case.qa)
    return inspections.submit_inspection(insp, case.qa, now=NOW), traveler


def pdi_ctq_frozen(case):
    """PDI frozen by a CTQ failure on Bore."""
    traveler, insp = _begin(case)
    enter(case, insp, "Bore", "10.5", gauge="VC-104")
    insp.refresh_from_db()
    return insp, traveler


def pdi_ctq_failure_submitted(case):
    """A SUBMITTED inspection that contains a CTQ failure.

    Cannot happen through the services (a CTQ failure freezes the checklist), so
    the rows are written directly to prove review blocks the concession anyway.
    """
    traveler, insp = _begin(case)
    for field in insp.template.fields.all():
        if field.field_type == "VISUAL":
            kwargs = dict(value_bool=True)
        else:
            value = {"Bore": "10.5"}.get(field.label, "0.4" if "Ra" in field.label else "25")
            gauge = {"Bore": case.cal, "Roughness Ra": case.other}.get(field.label, case.mic)
            kwargs = dict(value_numeric=value, gauge=gauge)
        Measurement.objects.create(inspection=insp, field=field, entered_by=case.qa, **kwargs)
    insp.status = InspectionStatus.SUBMITTED
    insp.submitted_at = NOW
    insp.save()
    return insp, traveler


def first_piece_submitted(case):
    traveler, machine = lot_in_setup(case)
    insp = begin_first_piece(case, traveler, machine)
    enter_good_first_piece(case, insp)
    return inspections.submit_inspection(insp, case.qa, now=NOW), traveler, machine


def first_piece_failed(case):
    traveler, machine = lot_in_setup(case)
    insp = begin_first_piece(case, traveler, machine)
    m = enter(case, insp, "Shaft diameter", "26")
    _photos(m, case.qa)
    enter(case, insp, "Burrs removed", True, gauge="")
    return inspections.submit_inspection(insp, case.qa, now=NOW), traveler, machine


def pdf_upload(name="approval.pdf", data=b"%PDF-1.4 customer ok"):
    return SimpleUploadedFile(name, data, content_type="application/pdf")
