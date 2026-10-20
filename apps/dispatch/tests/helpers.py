from datetime import timedelta

from django.db.models import QuerySet

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.inspections.models import Inspection, InspectionStatus
from apps.production.models import JobTraveler, TravelerOperation
from apps.review import services as review
from apps.review.models import QuarantineRecord
from apps.review.tests.helpers import (
    LATER, LONG_TEXT, ReviewTestCase, pdi_ctq_frozen, pdi_noctq_fail, pdi_pass,
)

AFTER = LATER + timedelta(hours=1)  # "now" for dispatch calls; after every decision in the helpers

__all__ = [
    "AFTER", "LATER", "LONG_TEXT", "DispatchTestCase", "approved_lot", "conceded_lot", "quarantined_lot",
    "frozen_lot", "make_red", "set_hold", "force_pdi_pending",
]


class DispatchTestCase(ReviewTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.clerk = make_user("clerk", Role.WAREHOUSE_CLERK)
        cls.clerk2 = make_user("clerk2", Role.WAREHOUSE_CLERK)


def approved_lot(case):
    """A lot whose final PDI was approved by a supervisor: serials issued, interlock green."""
    insp, traveler = pdi_pass(case)
    review.approve_inspection(insp, case.supervisor, "", LATER)
    traveler.refresh_from_db()
    return traveler


def conceded_lot(case):
    insp, traveler = pdi_noctq_fail(case)
    review.issue_concession(insp, case.auth_sup, "COSMETIC", LONG_TEXT, LATER)
    traveler.refresh_from_db()
    return traveler


def quarantined_lot(case):
    insp, traveler = pdi_noctq_fail(case)
    review.quarantine_lot(insp, case.supervisor, "Roughness out; reject the lot", LATER)
    traveler.refresh_from_db()
    return traveler


def frozen_lot(case):
    insp, traveler = pdi_ctq_frozen(case)
    return traveler


def make_red(traveler, op_number=20):
    """Un-sign one operation behind the services' back, as if it had been skipped."""
    QuerySet.update(
        TravelerOperation.objects.filter(traveler=traveler, routing_operation__op_number=op_number),
        status=TravelerOperation.Status.PENDING,
    )


def set_hold(traveler, until, op_number=20):
    QuerySet.update(
        TravelerOperation.objects.filter(traveler=traveler, routing_operation__op_number=op_number),
        hold_until=until,
    )


def set_traveler_status(traveler, status):
    QuerySet.update(JobTraveler.objects.filter(pk=traveler.pk), status=status)


def force_pdi_pending(traveler, status=InspectionStatus.SUBMITTED):
    """Final PDI step looks done but the inspection is not approved (decision rows untouched)."""
    pdi = Inspection.objects.get(traveler=traveler, checklist_type="PDI")
    QuerySet.update(Inspection.objects.filter(pk=pdi.pk), status=status)
    return pdi
