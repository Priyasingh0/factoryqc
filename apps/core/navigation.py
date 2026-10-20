"""Role-aware navigation for the application shell.

The menu is built from permissions (never from role names), the same rule the views follow.
Each entry is (label, url name, permission, icon, url-name prefixes that mark it active).
"""
from django.urls import NoReverseMatch, reverse

SECTIONS = [
    ("Overview", [
        ("Dashboard", "accounts:home", None, "dashboard", ("accounts:home",)),
    ]),
    ("Plant", [
        ("Machines", "plant:machine_list", "plant.view_machine", "machine", ("plant:machine",)),
        ("Gauges", "plant:gauge_list", "plant.view_gauge", "gauge", ("plant:gauge",)),
        ("Parts", "plant:part_list", "plant.view_part", "part", ("plant:part",)),
        ("Checklists", "checklists:list", "checklists.view_checklisttemplate", "checklist", ("checklists:",)),
    ]),
    ("Production", [
        ("Work orders", "production:work_order_list", "production.view_workorder", "workorder", ("production:work_order",)),
        ("Travelers", "production:traveler_list", "production.view_jobtraveler", "traveler", ("production:traveler", "production:trace")),
        ("Traceability", "production:trace", "production.view_jobtraveler", "shield", ("production:trace",)),
        ("Flow", "handover:flow_list", "production.view_jobtraveler", "flow", ("handover:flow_list", "handover:flow_detail")),
        ("Monitor", "handover:flow_monitor", "production.view_jobtraveler", "monitor", ("handover:flow_monitor",)),
        ("Handover", "handover:board", "handover.view_shifthandover", "handover", ("handover:board", "handover:new", "handover:detail", "handover:sign", "handover:acknowledge")),
    ]),
    ("Quality", [
        ("Inspections", "inspections:list", "inspections.view_inspection", "inspect", ("inspections:",)),
        ("Supervisor review", "review:queue", "review.view_supervisordecision", "review", ("review:queue", "review:detail", "review:approve", "review:concession", "review:quarantine")),
        ("Quarantine / MRB", "review:quarantine_list", "review.view_quarantinerecord", "lock", ("review:quarantine_list", "review:quarantine_detail", "review:propose", "review:sign", "review:customer_approval")),
    ]),
    ("Supply & shipping", [
        ("Materials", "materials:list", "materials.view_rawmaterialintake", "material", ("materials:",)),
        ("SNCR", "materials:sncr_list", "materials.view_sncr", "alert", ("materials:sncr",)),
        ("Suppliers", "materials:suppliers", "materials.view_supplier", "users", ("materials:suppliers",)),
        ("Dispatch", "dispatch:board", "dispatch.view_shipmentmanifest", "dispatch", ("dispatch:",)),
    ]),
    ("Administration", [
        ("Users", "accounts:user_list", "accounts.view_user", "users", ("accounts:user",)),
        ("Audit log", "core:audit_log", "core.view_auditlog", "audit", ("core:audit_log",)),
    ]),
]


def _score(match, prefixes):
    """Length of the longest prefix of the current URL name that this item claims (0 = none)."""
    if match is None:
        return 0
    name = f"{match.namespace}:{match.url_name}" if match.namespace else match.url_name
    return max((len(p) for p in prefixes if name.startswith(p)), default=0)


def build_nav(request):
    user = request.user
    match = getattr(request, "resolver_match", None)
    sections = []
    best, best_score = None, 0
    for title, items in SECTIONS:
        rows = []
        for label, url_name, perm, icon, prefixes in items:
            if perm and not user.has_perm(perm):
                continue
            try:
                url = reverse(url_name)
            except NoReverseMatch:
                continue
            row = {"label": label, "url": url, "icon": icon, "active": False}
            score = _score(match, prefixes)
            if score > best_score:
                best, best_score = row, score
            rows.append(row)
        if rows:
            sections.append({"title": title, "items": rows})
    if best is not None:
        best["active"] = True
    return sections
