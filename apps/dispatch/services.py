"""Shipping interlock, manifests, gate pass, packing slip, invoice and QR labels (PRD 10, 11).

The interlock reads the state the earlier steps already keep (traveler operations and
hold timers from production, the traveler's quarantine status and open quarantine
records from review, the final-PDI decision from inspections and review). It
creates no flags of its own, so there is no second source of truth to bypass.

Every function that issues or prints something re-checks the interlock; a blocked
attempt is written to the audit log and raises ShippingInterlock listing every
unfulfilled requirement (PRD 10.2).
"""

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from apps.checklists.models import ChecklistType
from apps.core.audit import log_event
from apps.core.exceptions import InvalidQRToken
from apps.core.models import AuditLog
from apps.core.signing import QRKind, read_token
from apps.inspections.models import Inspection, InspectionStatus
from apps.materials import services as materials
from apps.production import services as production
from apps.production.models import JobTraveler, PartSerial, TravelerOperation
from apps.review.models import HOLDING_AREA, QuarantineRecord

from .exceptions import DocumentError, ManifestError, NotAuthorized, ShippingInterlock
from .models import (
    GatePass, ManifestSerial, ShipmentLabel, ShipmentManifest, ShippingDocument,
)

PERM_MANIFEST = "dispatch.create_manifest"
PERM_GATE_PASS = "dispatch.issue_gate_pass"
PERM_DOCUMENTS = "dispatch.issue_shipping_documents"

OP = TravelerOperation.Status
TS = JobTraveler.Status
APPROVED_STATES = (InspectionStatus.APPROVED, InspectionStatus.CONCESSION)
PENDING_STATES = (InspectionStatus.IN_PROGRESS, InspectionStatus.SUBMITTED, InspectionStatus.FROZEN)


def _require(user, perm, action):
    if not user.has_perm(perm):
        raise NotAuthorized(f"Your role is not allowed to {action}.")


# ------------------------------------------------------------ the interlock

@dataclass
class Check:
    code: str
    title: str
    problems: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.problems


@dataclass
class Clearance:
    traveler: object
    checks: list

    @property
    def cleared(self):
        return all(c.ok for c in self.checks)

    @property
    def problems(self):
        return [p for c in self.checks for p in c.problems]


def final_pdi_operation(traveler):
    return traveler.operations.select_related("routing_operation", "signed_off_by").filter(
        routing_operation__is_final_pdi=True
    ).first()


def approved_final_pdi(traveler):
    """The approved (or conceded) final PDI inspection of this lot, or None (PRD 10.1 c)."""
    op = final_pdi_operation(traveler)
    if op is None or op.status != OP.COMPLETED:
        return None
    return (
        Inspection.objects.filter(
            traveler=traveler, traveler_operation=op, checklist_type=ChecklistType.PDI,
            status__in=APPROVED_STATES, decision__isnull=False,
        )
        .select_related("decision__decided_by", "template", "inspector")
        .first()
    )


def _operations_check(traveler):
    """PRD 10.1 a: every required manufacturing operation is signed off."""
    check = Check("OPERATIONS", "All manufacturing operations are signed off")
    operations = production.ordered_operations(traveler)
    if not operations:
        check.problems.append("The job traveler has no operations.")
    for op in operations:
        step = op.routing_operation
        if op.status != OP.COMPLETED:
            check.problems.append(f"Op {step.op_number} {step.name} is {op.get_status_display().lower()}.")
        elif not op.signed_off_by_id:
            check.problems.append(f"Op {step.op_number} {step.name} has no sign-off.")
    return check


def _holds_check(traveler, now):
    """PRD 10.1 b: hold timers finished and no quality or quarantine hold."""
    check = Check("HOLDS", "Hold times fulfilled and no quality hold")
    for op in traveler.operations.select_related("routing_operation").order_by("routing_operation__op_number"):
        remaining = op.hold_remaining(now)
        if remaining:
            step = op.routing_operation
            check.problems.append(
                f"Op {step.op_number} {step.name}: mandatory hold has {production.format_remaining(remaining)} remaining."
            )
    open_quarantine = QuarantineRecord.objects.filter(
        traveler=traveler, status=QuarantineRecord.Status.OPEN
    ).first()
    if open_quarantine or traveler.status == TS.QUARANTINED:
        check.problems.append(
            f"The lot is quarantined in the {HOLDING_AREA}; the Material Review Board must disposition it."
        )
    for lot in materials.blocking_lots(traveler.work_order):
        check.problems.append(
            f"Raw material lot {lot.lot_number} (heat {lot.heat_number}) is {lot.get_status_display().lower()}."
        )
    if traveler.status == TS.CLOSED:
        check.problems.append("This traveler was closed by an MRB disposition; its parts cannot ship.")
    return check


def _pdi_check(traveler):
    """PRD 10.1 c: the final PDI is signed and approved by Quality."""
    check = Check("FINAL_PDI", "Final PDI is signed and approved by Quality")
    op = final_pdi_operation(traveler)
    if op is None:
        check.problems.append("The routing has no final PDI step.")
        return check
    if approved_final_pdi(traveler) is not None:
        return check
    pdi = Inspection.objects.filter(traveler=traveler, checklist_type=ChecklistType.PDI)
    pending = pdi.filter(status__in=PENDING_STATES).first()
    if pending:
        waiting = "still in progress" if pending.status == InspectionStatus.IN_PROGRESS else "awaiting supervisor approval"
        check.problems.append(f"Final PDI inspection #{pending.pk} is {waiting}.")
    elif pdi.filter(status=InspectionStatus.QUARANTINED).exists():
        check.problems.append("The final PDI was rejected and the lot quarantined.")
    elif op.status != OP.COMPLETED:
        check.problems.append("The final PDI has not been carried out.")
    else:
        check.problems.append("No approved final PDI inspection is on record for this lot.")
    return check


def clearance(traveler, now=None):
    """The three mandatory pre-dispatch checks for a traveler (PRD 10.1)."""
    now = now or timezone.now()
    traveler = JobTraveler.objects.select_related("work_order__part").get(pk=traveler.pk)
    return Clearance(traveler, [_operations_check(traveler), _holds_check(traveler, now), _pdi_check(traveler)])


def assert_cleared(traveler, user, action, now=None):
    """Raise ShippingInterlock (and audit the attempt) unless the lot is cleared (PRD 10.2).

    Call outside any transaction so the record of a blocked attempt is kept.
    """
    result = clearance(traveler, now)
    if not result.cleared:
        log_event(
            action=AuditLog.Action.OTHER, user=user, instance=traveler, field_name="shipping_interlock",
            new_value="BLOCKED", remarks=f"Tried to {action}. " + " ".join(result.problems),
        )
        raise ShippingInterlock(result.problems)
    return result


def _recheck(traveler, now):
    """Same check inside the write transaction, after locking the traveler."""
    result = clearance(traveler, now)
    if not result.cleared:
        raise ShippingInterlock(result.problems)


# ---------------------------------------------------------------- manifest

def create_manifest(traveler, user, *, customer_po="", vehicle_number="", serials=None, now=None):
    """Log a shipment: PO, vehicle and serial numbers (PRD 11). Needs a green interlock."""
    now = now or timezone.now()
    _require(user, PERM_MANIFEST, "create shipment manifests")
    assert_cleared(traveler, user, "create a shipment manifest", now)
    with transaction.atomic():
        locked = JobTraveler.objects.select_for_update().select_related("work_order").get(pk=traveler.pk)
        _recheck(locked, now)
        order_po = locked.work_order.customer_po.strip()
        po = (customer_po or "").strip() or order_po
        vehicle = (vehicle_number or "").strip()
        if not po:
            raise ManifestError("Enter the customer PO number.")
        if order_po and po.lower() != order_po.lower():
            raise ManifestError(f"PO {po} does not match the work order's customer PO ({order_po}).")
        if not vehicle:
            raise ManifestError("Enter the transport vehicle number.")
        unshipped = list(locked.serials.filter(manifest_item__isnull=True))
        if serials is None:
            chosen = unshipped
        else:
            wanted = {getattr(s, "pk", s) for s in serials}
            known = {s.pk: s for s in locked.serials.all()}
            if not wanted <= known.keys():
                raise ManifestError("Some selected parts do not belong to this lot.")
            shipped = sorted(known[pk].serial_number for pk in wanted if hasattr(known[pk], "manifest_item"))
            if shipped:
                raise ManifestError("Already shipped on another manifest: " + ", ".join(shipped) + ".")
            chosen = [known[pk] for pk in sorted(wanted)]
        if not chosen:
            raise ManifestError("There are no unshipped parts to put on a manifest.")
        manifest = ShipmentManifest.objects.create(
            traveler=locked, customer_po=po, vehicle_number=vehicle, batch_size=len(chosen),
            created_by=user, created_at=now,
        )
        for serial in chosen:
            ManifestSerial.objects.create(manifest=manifest, serial=serial)
        log_event(
            action=AuditLog.Action.CREATE, user=user, instance=manifest,
            new_value=f"{len(chosen)} parts", remarks=f"PO {po}, vehicle {vehicle}; interlock green",
        )
    return manifest


def _load(manifest):
    return ShipmentManifest.objects.select_related("traveler__work_order__part").get(pk=manifest.pk)


def issue_gate_pass(manifest, user, now=None):
    """Issue the dispatch gate pass and mark the manifest dispatched (PRD 10.1)."""
    now = now or timezone.now()
    _require(user, PERM_GATE_PASS, "issue gate passes")
    manifest = _load(manifest)
    assert_cleared(manifest.traveler, user, "issue a dispatch gate pass", now)
    with transaction.atomic():
        locked = ShipmentManifest.objects.select_for_update().get(pk=manifest.pk)
        _recheck(manifest.traveler, now)
        if locked.status != ShipmentManifest.Status.OPEN:
            raise DocumentError("A gate pass has already been issued for this manifest.")
        gate_pass = GatePass.objects.create(
            manifest=locked, number=f"GP-{locked.pk:06d}", issued_by=user, issued_at=now,
        )
        locked.status = ShipmentManifest.Status.DISPATCHED
        locked.dispatched_at = now
        locked.save()
        log_event(
            action=AuditLog.Action.DECISION, user=user, instance=locked, field_name="status",
            previous_value=ShipmentManifest.Status.OPEN, new_value=ShipmentManifest.Status.DISPATCHED,
            remarks=f"Gate pass {gate_pass.number} issued for {locked.batch_size} parts",
        )
    return gate_pass


_PREFIX = {ShippingDocument.Kind.PACKING_SLIP: "PS", ShippingDocument.Kind.INVOICE: "INV"}


def issue_document(manifest, kind, user, now=None):
    """Issue the packing slip or commercial invoice. Returns (document, created)."""
    now = now or timezone.now()
    if kind not in _PREFIX:
        raise DocumentError("Unknown document type.")
    label = ShippingDocument.Kind(kind).label.lower()
    _require(user, PERM_DOCUMENTS, f"issue a {label}")
    manifest = _load(manifest)
    assert_cleared(manifest.traveler, user, f"issue a {label}", now)
    with transaction.atomic():
        locked = ShipmentManifest.objects.select_for_update().get(pk=manifest.pk)
        _recheck(manifest.traveler, now)
        existing = locked.documents.filter(kind=kind).first()
        if existing:
            log_event(
                action=AuditLog.Action.OTHER, user=user, instance=existing, field_name="reprint",
                new_value=existing.number, remarks=f"{label.capitalize()} reprinted",
            )
            return existing, False
        document = ShippingDocument.objects.create(
            manifest=locked, kind=kind, number=f"{_PREFIX[kind]}-{locked.pk:06d}", issued_by=user, issued_at=now,
        )
        log_event(
            action=AuditLog.Action.CREATE, user=user, instance=document, new_value=document.number,
            remarks=f"{label.capitalize()} issued for manifest {locked.pk}; interlock green",
        )
    return document, True


def issue_label(manifest, user, now=None):
    """Create the next crate QR label for the manifest (PRD 11)."""
    now = now or timezone.now()
    _require(user, PERM_DOCUMENTS, "issue QR labels")
    manifest = _load(manifest)
    assert_cleared(manifest.traveler, user, "issue a shipping QR label", now)
    with transaction.atomic():
        locked = ShipmentManifest.objects.select_for_update().get(pk=manifest.pk)
        _recheck(manifest.traveler, now)
        last = locked.labels.order_by("-crate_number").values_list("crate_number", flat=True).first() or 0
        label = ShipmentLabel.objects.create(manifest=locked, crate_number=last + 1, issued_by=user, issued_at=now)
        log_event(
            action=AuditLog.Action.CREATE, user=user, instance=label, new_value=f"crate {label.crate_number}",
            remarks=f"QR label for manifest {locked.pk}; interlock green",
        )
    return label


def authorize_print(manifest, user, what, now=None):
    """Re-verify the interlock before any issued document is shown or printed (PRD 10.1)."""
    manifest = _load(manifest)
    assert_cleared(manifest.traveler, user, f"print the {what}", now)
    return manifest


# ----------------------------------------------------------------- reports

def overview(now=None):
    """One row per traveler for the dispatch board: green or red, and what has shipped."""
    now = now or timezone.now()
    rows = []
    travelers = JobTraveler.objects.exclude(status=TS.CLOSED).select_related("work_order__part")
    for traveler in travelers:
        result = clearance(traveler, now)
        total = traveler.serials.count()
        shipped = ManifestSerial.objects.filter(manifest__traveler=traveler).count()
        rows.append({
            "traveler": traveler, "clearance": result, "total": total, "shipped": shipped,
            "first_problem": result.problems[0] if result.problems else "",
            "manifests": traveler.manifests.count(),
        })
    return rows


def certificate(token):
    """Data for the public customer certificate behind a crate QR code (PRD 11.3).

    Returns None for a forged, edited or unknown token.
    """
    try:
        label_id = int(read_token(token, QRKind.SHIPMENT))
        label = ShipmentLabel.objects.select_related(
            "manifest__traveler__work_order__part", "manifest__gate_pass"
        ).get(pk=label_id)
    except (InvalidQRToken, ValueError, ShipmentLabel.DoesNotExist):
        return None
    manifest = label.manifest
    traveler = manifest.traveler
    inspection = approved_final_pdi(traveler)
    readings = []
    concession = None
    if inspection is not None:
        readings = list(inspection.measurements.select_related("field", "gauge"))
        concession = getattr(inspection.decision, "concession", None)
    result = clearance(traveler)
    return {
        "label": label, "manifest": manifest, "traveler": traveler, "work_order": traveler.work_order,
        "part": traveler.work_order.part, "serials": [i.serial for i in manifest.items.select_related("serial")],
        "inspection": inspection, "decision": inspection.decision if inspection else None,
        "readings": readings, "concession": concession,
        "lots": materials.lots_for_work_order(traveler.work_order),
        "valid": result.cleared and inspection is not None, "problems": result.problems,
    }
