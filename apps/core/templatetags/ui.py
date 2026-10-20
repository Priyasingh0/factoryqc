"""Template helpers for the shared UI: status badges and icons."""
from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

# status text (normalised) -> (css class, label override)
_SUCCESS, _DANGER, _WARN, _INFO, _PURPLE, _NEUTRAL = "b-success", "b-danger", "b-warn", "b-info", "b-purple", "b-neutral"
STATUS_STYLES = {
    "DRAFT": _NEUTRAL, "PENDING": _WARN, "RELEASED": _INFO, "APPROVED": _SUCCESS, "FAILED": _DANGER,
    "FROZEN": _DANGER, "QUARANTINED": _DANGER, "CONCESSION": _PURPLE, "CLEARED": _SUCCESS,
    "REJECTED": _DANGER, "ON_HOLD": _WARN, "READY": _SUCCESS, "BLOCKED": _DANGER, "COMPLETED": _SUCCESS,
    # statuses used by the individual modules
    "IN_PROGRESS": _INFO, "SUBMITTED": _INFO, "ACTIVE": _INFO, "CLOSED": _NEUTRAL, "OPEN": _DANGER,
    "RECEIVED": _INFO, "IN_LAB_TESTING": _WARN, "SUPPLIER_QUARANTINE": _DANGER, "RETURNED": _NEUTRAL,
    "SCRAPPED": _DANGER, "PASS": _SUCCESS, "FAIL": _DANGER, "PLANNED": _NEUTRAL, "CANCELLED": _NEUTRAL,
    "SENT": _SUCCESS, "SHIPPED": _SUCCESS, "SIGNED": _SUCCESS, "ACKNOWLEDGED": _SUCCESS,
    "AWAITING_SIGNOFF": _WARN, "AWAITING_ACK": _WARN, "PENDING_APPROVAL": _WARN, "AWAITING_APPROVAL": _WARN,
    "VALID": _SUCCESS, "EXPIRED": _DANGER, "INACTIVE": _NEUTRAL, "DISPATCHED": _SUCCESS, "LAB_FAIL": _DANGER,
    "MTC_OUT_OF_SPEC": _DANGER, "NO_VALID_MTC": _DANGER, "DAMAGED": _DANGER, "GOOD": _SUCCESS, "DEGRADED": _WARN,
    "DOWN": _DANGER, "WATCH": _WARN, "CHANGE_NOW": _DANGER, "RUNNING": _INFO, "SETUP": _INFO, "SUPERSEDED": _NEUTRAL,
    "REWORK": _WARN, "SCRAP": _DANGER, "RTV": _PURPLE, "CUSTOMER_CONCESSION": _PURPLE, "CUSTOMER_DEVIATION": _PURPLE,
    "QUALITY_HOLD": _WARN, "MATERIAL_HOLD": _WARN, "PENDING_FINAL_PDI": _WARN, "OK": _SUCCESS, "NO": _NEUTRAL,
}


@register.filter
def status_class(value):
    key = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    return STATUS_STYLES.get(key, _NEUTRAL)


@register.simple_tag
def badge(value, label=None):
    """{% badge obj.status obj.get_status_display %} -> coloured pill."""
    text = label if label not in (None, "") else str(value).replace("_", " ").title()
    return format_html('<span class="badge {}">{}</span>', status_class(value), text)


_ICONS = {
    "dashboard": "M3 12l9-8 9 8M5 10v10h5v-6h4v6h5V10",
    "machine": "M4 20V9l5 3V9l5 3V5h6v15zM8 16h2M13 16h2M17 16h1",
    "gauge": "M12 4a9 9 0 100 18 9 9 0 000-18zM12 13l4-4M7 17h10",
    "part": "M12 2l9 5v10l-9 5-9-5V7zM12 12l9-5M12 12v10M12 12L3 7",
    "checklist": "M9 6h11M9 12h11M9 18h11M3 6l1.5 1.5L7 5M3 12l1.5 1.5L7 11M3 18l1.5 1.5L7 17",
    "workorder": "M7 3h10l3 3v15H4V3zM8 9h8M8 13h8M8 17h5",
    "traveler": "M4 5h16v14H4zM4 10h16M9 10v9",
    "flow": "M4 6h6v4H4zM14 14h6v4h-6zM10 8h4a2 2 0 012 2v4",
    "monitor": "M3 4h18v12H3zM8 20h8M12 16v4M6 12l3-3 3 2 4-5",
    "handover": "M7 7h10l-3-3M17 17H7l3 3M4 12h16",
    "inspect": "M10 4a6 6 0 100 12 6 6 0 000-12zM15 15l6 6M8 10l2 2 3-4",
    "review": "M5 3h14v18H5zM9 8h6M9 12h6M9 16h3",
    "material": "M3 8l9-5 9 5-9 5zM3 8v8l9 5 9-5V8M12 13v8",
    "dispatch": "M2 6h12v10H2zM14 10h4l4 3v3h-8zM6 19a2 2 0 100-4 2 2 0 000 4zM18 19a2 2 0 100-4 2 2 0 000 4z",
    "users": "M9 11a4 4 0 100-8 4 4 0 000 8zM2 21c0-4 3-7 7-7s7 3 7 7M17 4a4 4 0 010 7M22 21c0-3-2-5-4-6",
    "audit": "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4",
    "bell": "M6 16V11a6 6 0 1112 0v5l2 2H4zM10 20a2 2 0 004 0",
    "menu": "M4 6h16M4 12h16M4 18h16",
    "check": "M5 13l4 4L19 7",
    "alert": "M12 3l10 18H2zM12 10v5M12 18v.5",
    "info": "M12 2a10 10 0 100 20 10 10 0 000-20zM12 11v6M12 7.5v.5",
    "x": "M6 6l12 12M18 6L6 18",
    "inbox": "M3 13l3-8h12l3 8v6H3zM3 13h5l1 3h6l1-3h5",
    "lock": "M6 11h12v10H6zM8 11V7a4 4 0 118 0v4",
    "shield": "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z",
    "chevrons": "M13 7l-5 5 5 5M19 7l-5 5 5 5",
    "chevron": "M6 9l6 6 6-6",
    "more": "M5 12h.01M12 12h.01M19 12h.01",
    "plus": "M12 5v14M5 12h14",
    "arrow": "M5 12h14M13 6l6 6-6 6",
    "logout": "M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10",
    "camera": "M4 8h3l2-3h6l2 3h3v11H4zM12 17a3.5 3.5 0 100-7 3.5 3.5 0 000 7z",
    "clock": "M12 3a9 9 0 100 18 9 9 0 000-18zM12 7v5l3 2",
    "print": "M7 9V3h10v6M7 17H4v-7h16v7h-3M7 14h10v7H7z",
    "file": "M6 3h8l4 4v14H6zM14 3v4h4M9 13h6M9 17h6",
    "user": "M12 12a4 4 0 100-8 4 4 0 000 8zM4 21c0-4 3.5-7 8-7s8 3 8 7",
    "pen": "M4 20h4L19 9l-4-4L4 16zM13 7l4 4",
    "eye": "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 15a3 3 0 100-6 3 3 0 000 6z",
    "flag": "M5 21V4M5 4h11l-2 4 2 4H5",
    "download": "M12 4v11M7 11l5 5 5-5M5 20h14",
    "search": "M11 4a7 7 0 100 14 7 7 0 000-14zM20 20l-4-4",
    "tool": "M14 6a4 4 0 005 5L10 20a2.8 2.8 0 01-4-4l9-9zM15 3l-2 2",
    "package": "M3 8l9-5 9 5v8l-9 5-9-5zM3 8l9 5 9-5M12 13v8",
    "pulse": "M3 12h4l3-7 4 14 3-7h4",
    "factory": "M3 21V10l6 4V10l6 4V6h6v15zM7 17h2M12 17h2M17 17h2",
}


@register.simple_tag
def icon(name, cls=""):
    path = _ICONS.get(name, _ICONS["info"])
    return mark_safe(
        f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="{path}"/></svg>'
    )


@register.filter
def initials(user):
    name = (user.get_full_name() or user.get_username() or "?").split()
    return "".join(p[0] for p in name[:2]).upper()


@register.filter
def message_level(tags):
    t = str(tags)
    for k in ("success", "error", "warning", "info"):
        if k in t:
            return k
    return "info"


@register.filter
def pct(part, whole):
    try:
        return int(round(100 * float(part) / float(whole))) if float(whole) else 0
    except (TypeError, ValueError):
        return 0


@register.simple_tag
def range_pos(value, low, high):
    """Dot position (0-100) of a reading on a bar whose green band spans the 25-75 % zone.

    Display only: pass/fail is decided by the services, never by this helper.
    """
    try:
        value, low, high = float(value), float(low), float(high)
        if high <= low:
            return 50
        pos = 25 + 50 * (value - low) / (high - low)
        return round(max(2, min(98, pos)), 1)
    except (TypeError, ValueError):
        return 50


def _fmt_signed(x):
    return ("+" if x >= 0 else "\u2212") + f"{abs(x):.4f}"


@register.simple_tag
def deviation(value, nominal):
    """Signed difference between a reading and nominal, e.g. +0.0120. Display only."""
    try:
        return _fmt_signed(float(value) - float(nominal))
    except (TypeError, ValueError):
        return ""


@register.simple_tag
def excursion(value, low, high):
    """How far a reading lies outside its limits (empty when inside). Display only."""
    try:
        v, lo, hi = float(value), float(low), float(high)
    except (TypeError, ValueError):
        return ""
    if v > hi:
        return _fmt_signed(v - hi)
    if v < lo:
        return _fmt_signed(v - lo)
    return ""


@register.filter
def failed_count(readings):
    """Number of out-of-tolerance readings in the review context list. Display only."""
    return sum(1 for r in readings if not r["m"].is_within_tolerance)


@register.filter
def ctq_failed_count(readings):
    """Number of out-of-tolerance CTQ readings in the review context list. Display only."""
    return sum(1 for r in readings if not r["m"].is_within_tolerance and r["m"].field.is_ctq)


@register.filter
def duration(minutes):
    """Whole minutes -> '45 min', '3 h 20 min', '2 d 4 h'. Display only."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return ""
    if m < 60:
        return f"{m} min"
    if m < 1440:
        h, mm = divmod(m, 60)
        return f"{h} h {mm:02d} min" if mm else f"{h} h"
    d, rest = divmod(m, 1440)
    return f"{d} d {rest // 60} h"


@register.filter
def count_where(rows, key):
    """How many dict rows have a truthy `key` (queue / board summaries). Display only."""
    return sum(1 for r in rows if r.get(key))


@register.filter
def max_of(rows, key):
    """Largest numeric `key` among dict rows (e.g. the oldest wait). Display only."""
    vals = [r.get(key) or 0 for r in rows]
    return max(vals) if vals else 0


_GATES = {"OPERATIONS": "Operations", "HOLDS": "Holds", "FINAL_PDI": "Final PDI"}


@register.filter
def gate_label(code):
    """Short name of a dispatch clearance check for the board's gate trail. Display only."""
    return _GATES.get(code, str(code).replace("_", " ").title())


@register.filter
def sum_attr(items, attr):
    """Sum of a numeric attribute over a sequence (e.g. parts across manifests). Display only."""
    total = 0
    for i in items:
        try:
            total += int(getattr(i, attr) or 0)
        except (TypeError, ValueError):
            pass
    return total


@register.filter
def tdur(value):
    """timedelta -> '2 h 10 min' (see `duration`). Display only."""
    try:
        return duration(int(value.total_seconds() // 60))
    except AttributeError:
        return ""


@register.filter
def count_is(items, spec):
    """{{ tiles|count_is:'status=SLOW' }} -> how many items have attribute/key == value. Display only."""
    key, _, wanted = spec.partition("=")
    n = 0
    for i in items:
        v = i.get(key) if isinstance(i, dict) else getattr(i, key, None)
        if str(v) == wanted:
            n += 1
    return n
