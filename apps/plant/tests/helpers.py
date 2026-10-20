from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from apps.plant.models import Department, Gauge, GaugeType, Machine

IST = ZoneInfo("Asia/Kolkata")


def at(year, month, day, hour=10, minute=0):
    """Timezone-aware datetime in the factory's local time."""
    return datetime(year, month, day, hour, minute, tzinfo=IST)


def make_machine(code="CNC-04", active=True):
    dept, _ = Department.objects.get_or_create(code="MS", defaults={"name": "Main Machining Shop"})
    return Machine.objects.create(code=code, name=f"Machine {code}", department=dept, is_active=active)


def make_gauge(gauge_id="VC-104", expiry=None, active=True):
    expiry = expiry or date.today() + timedelta(days=90)
    return Gauge.objects.create(
        gauge_id=gauge_id,
        gauge_type=GaugeType.CALIPER,
        calibrated_on=expiry - timedelta(days=180),
        calibration_expiry=expiry,
        is_active=active,
    )
