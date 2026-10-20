"""Display-only classification of a dispatch clearance for the readiness board.

This never decides whether a lot may ship (services.clearance does); it only names the
reason shown on screen so the board can group lots at a glance.
"""

READY = "READY"
QUALITY_HOLD = "QUALITY_HOLD"
MATERIAL_HOLD = "MATERIAL_HOLD"
PENDING_FINAL_PDI = "PENDING_FINAL_PDI"
BLOCKED = "BLOCKED"

LABELS = {
    READY: "Ready",
    QUALITY_HOLD: "Quality hold",
    MATERIAL_HOLD: "Material hold",
    PENDING_FINAL_PDI: "Pending final PDI",
    BLOCKED: "Blocked",
}


def readiness(clearance):
    """Return the board category for a services.Clearance."""
    if clearance.cleared:
        return READY
    by_code = {c.code: c for c in clearance.checks}
    holds = by_code.get("HOLDS")
    if holds and not holds.ok:
        if any("quarantined" in p or "closed by an MRB" in p for p in holds.problems):
            return QUALITY_HOLD
        if any(p.startswith("Raw material lot") for p in holds.problems):
            return MATERIAL_HOLD
    ops, pdi = by_code.get("OPERATIONS"), by_code.get("FINAL_PDI")
    if pdi and not pdi.ok and (ops is None or ops.ok) and (holds is None or holds.ok):
        return PENDING_FINAL_PDI
    return BLOCKED
