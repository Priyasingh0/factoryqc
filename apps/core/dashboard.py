"""Read-only numbers for the dashboard, taken from the live tables.

Every block is gated on the viewer's permissions, so a role only sees counts for modules it
may open. Nothing here changes data; it reuses the services that already decide the rules.
"""
from collections import Counter
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.dispatch import presentation, services as dispatch
from apps.inspections.models import Inspection, InspectionStatus
from apps.materials.models import IntakeStatus, RawMaterialIntake, SNCR
from apps.plant.models import Gauge
from apps.production.models import JobTraveler, TravelerOperation, WorkOrder
from apps.review.models import QuarantineRecord

from .models import AuditLog

ALERT_DANGER, ALERT_WARN = "danger", "warn"


def _kpi(label, value, url, tone="", sub=""):
    return {"label": label, "value": value, "url": url, "tone": tone, "sub": sub}


def build(user, now=None):
    now = now or timezone.now()
    can = user.has_perm
    kpis, alerts, panels = [], [], {}

    # ---------------------------------------------------------- work orders / production
    if can("production.view_workorder"):
        total = WorkOrder.objects.count()
        open_wo = WorkOrder.objects.filter(released_at__isnull=True).count()
        kpis.append(_kpi("Work orders", total, reverse("production:work_order_list"), "blue",
                         f"{open_wo} not yet released"))
    if can("production.view_jobtraveler"):
        counts = Counter(JobTraveler.objects.values_list("status", flat=True))
        active = counts.get(JobTraveler.Status.ACTIVE, 0)
        kpis.append(_kpi("Active travelers", active, reverse("production:traveler_list"), "teal",
                         f"{counts.get(JobTraveler.Status.COMPLETED, 0)} completed"))
        op_counts = Counter(TravelerOperation.objects.filter(traveler__status=JobTraveler.Status.ACTIVE)
                            .values_list("status", flat=True))
        done = op_counts.get(TravelerOperation.Status.COMPLETED, 0)
        total_ops = sum(op_counts.values())
        panels["production"] = {
            "travelers": [{"label": JobTraveler.Status(k).label.split(" (")[0], "value": v, "key": k}
                          for k, v in counts.items()],
            "ops_done": done, "ops_total": total_ops,
            "ops": [{"label": TravelerOperation.Status(k).label, "value": v} for k, v in op_counts.items()],
        }

    # ------------------------------------------------------------------ inspections
    if can("inspections.view_inspection"):
        counts = Counter(Inspection.objects.values_list("status", flat=True))
        active = counts.get(InspectionStatus.IN_PROGRESS, 0)
        frozen = counts.get(InspectionStatus.FROZEN, 0)
        kpis.append(_kpi("Active inspections", active, reverse("inspections:list"), "teal",
                         f"{frozen} frozen" if frozen else "none frozen"))
        if frozen:
            alerts.append((ALERT_DANGER, f"{frozen} inspection(s) frozen after a CTQ failure.", reverse("review:queue")))
    if can("review.view_supervisordecision"):
        pending = Inspection.objects.filter(status__in=[InspectionStatus.SUBMITTED, InspectionStatus.FROZEN]).count()
        kpis.append(_kpi("Pending reviews", pending, reverse("review:queue"), "orange" if pending else "",
                         "awaiting supervisor decision"))
        if pending:
            alerts.append((ALERT_WARN, f"{pending} inspection(s) waiting for a supervisor decision.", reverse("review:queue")))
        open_q = QuarantineRecord.objects.filter(status=QuarantineRecord.Status.OPEN).count()
        kpis.append(_kpi("Quality holds", open_q, reverse("review:quarantine_list"), "red" if open_q else "",
                         "lots awaiting MRB"))
        if open_q:
            alerts.append((ALERT_DANGER, f"{open_q} lot(s) in quarantine awaiting MRB disposition.", reverse("review:quarantine_list")))

    # -------------------------------------------------------------------- materials
    if can("materials.view_rawmaterialintake"):
        quarantined = RawMaterialIntake.objects.filter(status=IntakeStatus.SUPPLIER_QUARANTINE).count()
        awaiting = RawMaterialIntake.objects.filter(status__in=[IntakeStatus.RECEIVED, IntakeStatus.IN_LAB_TESTING]).count()
        kpis.append(_kpi("Quarantined materials", quarantined, reverse("materials:list"), "red" if quarantined else "",
                         f"{awaiting} awaiting clearance"))
        if quarantined:
            alerts.append((ALERT_DANGER, f"{quarantined} material lot(s) in supplier quarantine.", reverse("materials:list")))
        panels["materials"] = [{"label": IntakeStatus(k).label.split(" (")[0], "value": v, "key": k}
                               for k, v in Counter(RawMaterialIntake.objects.values_list("status", flat=True)).items()]
        open_sncr = SNCR.objects.filter(status=SNCR.Status.OPEN).count()
        if open_sncr:
            alerts.append((ALERT_WARN, f"{open_sncr} open supplier non-conformance report(s).", reverse("materials:sncr_list")))

    # ---------------------------------------------------------------------- dispatch
    if can("dispatch.view_shipmentmanifest"):
        rows = dispatch.overview(now)
        cats = Counter()
        for r in rows:
            r["category"] = presentation.readiness(r["clearance"])
            cats[r["category"]] += 1
        ready = cats.get(presentation.READY, 0)
        kpis.append(_kpi("Dispatch ready", ready, reverse("dispatch:board"), "teal",
                         f"{len(rows) - ready} not cleared"))
        panels["dispatch"] = [{"key": k, "label": presentation.LABELS[k], "value": cats.get(k, 0)}
                              for k in (presentation.READY, presentation.PENDING_FINAL_PDI,
                                        presentation.QUALITY_HOLD, presentation.MATERIAL_HOLD, presentation.BLOCKED)]
        panels["dispatch_total"] = len(rows)

    # ------------------------------------------------------------------ equipment
    if can("plant.view_gauge"):
        today = timezone.localdate()
        expired = Gauge.objects.filter(is_active=True, calibration_expiry__lt=today).count()
        soon = Gauge.objects.filter(is_active=True, calibration_expiry__gte=today,
                                    calibration_expiry__lte=today + timedelta(days=14)).count()
        if expired:
            alerts.append((ALERT_DANGER, f"{expired} gauge(s) past calibration expiry.", reverse("plant:gauge_list")))
        if soon:
            alerts.append((ALERT_WARN, f"{soon} gauge(s) expire within 14 days.", reverse("plant:gauge_list")))
    if can("handover.view_shifthandover"):
        from apps.handover import services as handover
        waiting = len(handover.pending_handovers(now=now))
        if waiting:
            alerts.append((ALERT_WARN, f"{waiting} shift handover(s) not yet acknowledged.", reverse("handover:board")))

    # ----------------------------------------------------------------- recent activity
    activity = []
    if can("core.view_auditlog"):
        activity = list(AuditLog.objects.order_by("-timestamp")[:8])

    alerts.sort(key=lambda a: a[0] != ALERT_DANGER)
    return {"kpis": kpis, "alerts": alerts, "panels": panels, "activity": activity}
