from datetime import date, timedelta
from decimal import Decimal

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.checklists.models import ChecklistType, FieldType, TemplateStatus
from apps.checklists.tests.helpers import force_status, make_dimension, make_part, make_template
from apps.inspections import services
from apps.plant.models import Gauge, GaugeType
from apps.production import services as production
from apps.production.tests.helpers import (
    NOW, ProductionTestCase, make_machine_ready, make_routing, make_work_order, op_at, run_through,
)

D = Decimal
VALID_UNTIL = date(2026, 12, 31)
EXPIRED_ON = date(2026, 3, 9)  # the day before NOW


class InspectionTestCase(ProductionTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.qa_head = make_user("qh", Role.QUALITY_HEAD)
        cls.supervisor = make_user("sup", Role.SHIFT_SUPERVISOR)
        cls.mic = make_gauge("MIC-1", GaugeType.MICROMETER)
        cls.cal = make_gauge("VC-104", GaugeType.CALIPER)
        cls.other = make_gauge("RT-9", GaugeType.OTHER)


def make_gauge(gauge_id, gauge_type, expiry=VALID_UNTIL, active=True):
    return Gauge.objects.create(
        gauge_id=gauge_id, gauge_type=gauge_type, calibrated_on=expiry - timedelta(days=180),
        calibration_expiry=expiry, is_active=active,
    )


def _release(template, approver):
    return force_status(template, TemplateStatus.RELEASED, approver)


def build_part(author, approver, number="P-300", op20_hold=0):
    """Routing 10 (first piece) / 20 / 30 (first piece) / 40 final PDI, with real field specs.

    First-piece op 10: Shaft diameter (micrometer, 25.00 +0.05/-0.02), Burrs removed (visual).
    PDI: Shaft diameter, Bore (CTQ, caliper, 10.00 +/-0.10), Roughness (max 0.8), Coating (visual).
    """
    part = make_part(number)
    make_routing(part, op20_hold=op20_hold)

    fp10 = make_template(author, part=part, checklist_type=ChecklistType.FIRST_PIECE,
                         operation_number=10, with_field=False)
    make_dimension(fp10, 1, label="Shaft diameter")
    make_visual(fp10, 2, "Burrs removed")
    _release(fp10, approver)

    fp30 = make_template(author, part=part, checklist_type=ChecklistType.FIRST_PIECE,
                         operation_number=30, with_field=False)
    make_dimension(fp30, 1, label="Ground diameter")
    _release(fp30, approver)

    pdi = make_template(author, part=part, checklist_type=ChecklistType.PDI, with_field=False)
    make_dimension(pdi, 1, label="Shaft diameter")
    make_dimension(
        pdi, 2, label="Bore", is_ctq=True, nominal=D("10.0000"), upper_tol=D("0.1000"),
        lower_tol=D("0.1000"), required_gauge_type=GaugeType.CALIPER,
    )
    make_dimension(
        pdi, 3, label="Roughness Ra", field_type=FieldType.MAX, nominal=None, upper_tol=None,
        lower_tol=None, max_limit=D("0.8000"), unit="um", required_gauge_type=GaugeType.OTHER,
    )
    make_visual(pdi, 4, "Coating applied")
    _release(pdi, approver)
    return part


def make_visual(template, sequence, label, **overrides):
    from apps.checklists.models import TemplateField

    values = dict(template=template, sequence=sequence, label=label, field_type=FieldType.VISUAL)
    values.update(overrides)
    return TemplateField.objects.create(**values)


def field_named(template, label):
    return template.fields.get(label=label)


def lot_in_setup(case, quantity=2):
    """A released lot whose Op 10 is running on a machine in Setup Mode. Returns (traveler, machine)."""
    part = build_part(case.engineer, case.manager)
    traveler = production.release_work_order(make_work_order(case.lead, part, quantity=quantity), case.lead)
    machine = make_machine_ready(user=case.operator)
    production.start_operation(op_at(traveler, 10), case.operator, machine, NOW)
    return traveler, machine


def begin_first_piece(case, traveler, machine, user=None, now=NOW):
    return services.start_inspection(
        user or case.qa, machine.qr_token, traveler.barcode, ChecklistType.FIRST_PIECE, now
    )


def enter(case, inspection, label, value, gauge="MIC-1", user=None, now=NOW):
    field = field_named(inspection.template, label)
    return services.record_measurement(
        inspection, field, value=value, gauge_id=gauge, user=user or case.qa, now=now
    )


def enter_good_first_piece(case, inspection):
    enter(case, inspection, "Shaft diameter", "25.0000")
    enter(case, inspection, "Burrs removed", True, gauge="")


def lot_ready_for_pdi(case, quantity=2):
    """All production steps done (holds waived); returns (traveler, station machine)."""
    part = build_part(case.engineer, case.manager, number="P-301", op20_hold=0)
    traveler = production.release_work_order(
        make_work_order(case.lead, part, number="WO-PDI", quantity=quantity), case.lead
    )
    t = NOW
    station = None
    for number, code in ((10, "CNC-10"), (20, "CNC-20"), (30, "CNC-30")):
        machine = make_machine_ready(code, t, case.operator)
        run_through(case, traveler, machine, number, t, quantity)
        station = machine
        t += timedelta(hours=1)
    return traveler, station


def begin_pdi(case, traveler, machine, user=None, now=NOW + timedelta(hours=4)):
    return services.start_inspection(
        user or case.qa, machine.qr_token, traveler.barcode, ChecklistType.PDI, now
    )


def tiny_png(extra=b""):
    """A few bytes with a real PNG signature; `extra` makes each photo different."""
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 16 + extra
