from .models import Role

# "app_label.codename" strings. Add more as later apps introduce permissions,
# then re-run:  python manage.py setup_roles
#
# Plant (Step 2): master data is maintained by the Plant Manager (PRD 12) and
# by the Quality Manager for the gauge register. PM checks are recorded by the
# people who run or maintain the station (PRD 2.2).
_PLANT_VIEW = [
    "plant.view_machine",
    "plant.view_gauge",
    "plant.view_part",
    "plant.view_toolingprogram",
]

# Checklists (Step 3): Quality Engineers author drafts, the Quality Manager
# approves (PRD 12). Everyone who later uses templates can view them.
_CHECKLIST_VIEW = [
    "checklists.view_checklisttemplate",
    "checklists.view_templatefield",
    "checklists.view_blueprintpin",
]
_CHECKLIST_AUTHOR = [
    "checklists.add_checklisttemplate",
    "checklists.change_checklisttemplate",
    "checklists.delete_checklisttemplate",
    "checklists.add_templatefield",
    "checklists.change_templatefield",
    "checklists.delete_templatefield",
    "checklists.add_blueprintpin",
    "checklists.change_blueprintpin",
    "checklists.delete_blueprintpin",
]

# Production (Step 4): the Production Lead creates and releases work orders
# (PRD 12); operators run steps; QA inspectors sign off first pieces and log
# periodic samples. Everyone else can look at the shop-floor record.
_PRODUCTION_VIEW = [
    "production.view_workorder",
    "production.view_routingoperation",
    "production.view_jobtraveler",
    "production.view_traveleroperation",
    "production.view_productionrun",
    "production.view_partserial",
]
_PRODUCTION_PLAN = [
    "production.add_workorder",
    "production.change_workorder",
    "production.add_routingoperation",
    "production.change_routingoperation",
    "production.release_workorder",
]

# Inspections (Step 5): QA inspectors run inspections; the people who review or
# act on them can view. Operators and clerks do not see inspection data.
_INSPECTION_VIEW = [
    "inspections.view_inspection",
    "inspections.view_measurement",
    "inspections.view_defectphoto",
]

# Review (Step 6): Shift Supervisors decide inspections; the Quality Manager (and
# supervisors authorised by flag) issue concessions; the Quality Head can
# quarantine, e.g. after a CTQ freeze; the MRB is Plant Manager, Quality Head
# and Production Lead (PRD 6.3).
_REVIEW_VIEW = [
    "review.view_supervisordecision",
    "review.view_concession",
    "review.view_quarantinerecord",
    "review.view_mrbdisposition",
    "review.view_mrbsignoff",
]
_MRB = ["review.propose_disposition", "review.sign_disposition"]

# Dispatch (Step 7): Warehouse Clerks create manifests and issue the gate pass, packing
# slip, invoice and QR labels once the shipping interlock is green (PRD 10.2). The
# people who clear or review lots can see the dispatch board.
_DISPATCH_VIEW = [
    "dispatch.view_shipmentmanifest",
    "dispatch.view_manifestserial",
    "dispatch.view_gatepass",
    "dispatch.view_shippingdocument",
    "dispatch.view_shipmentlabel",
]
_DISPATCH_ISSUE = [
    "dispatch.create_manifest",
    "dispatch.issue_gate_pass",
    "dispatch.issue_shipping_documents",
]

# Materials (Step 8): the Dock Clerk logs and can reject intake; Quality reviews the MTC
# against the grade specification and records lab results; Quality Head / Manager can
# quarantine a cleared lot; Purchasing is told about SNCRs and closes them by vendor
# return; the Production Lead issues cleared stock to work orders (PRD 3, 12).
_MATERIALS_VIEW = [
    "materials.view_rawmaterialintake",
    "materials.view_supplier",
    "materials.view_sncr",
    "materials.view_mtcreading",
    "materials.view_labresult",
    "materials.view_materialissue",
    "materials.view_materialgrade",
    "materials.view_gradeproperty",
]
_MATERIALS_SPEC = [
    "materials.add_materialgrade",
    "materials.change_materialgrade",
    "materials.add_gradeproperty",
    "materials.change_gradeproperty",
    "materials.delete_gradeproperty",
]
_MATERIALS_SUPPLIER = ["materials.add_supplier", "materials.change_supplier"]
_MATERIALS_QA = ["materials.review_mtc", "materials.record_lab_result", "materials.reject_intake"]

# Handover (Step 9): operators and supervisors complete the outgoing form and acknowledge
# the incoming one; a supervisor other than the author countersigns (PRD 12.2). The people
# who oversee the floor can read the logbook.
_HANDOVER_VIEW = [
    "handover.view_shifthandover",
    "handover.view_handoversignoff",
    "handover.view_handoveracknowledgement",
]

ROLE_PERMISSIONS = {
    Role.PLANT_MANAGER: [
        "accounts.view_user",
        "accounts.add_user",
        "accounts.change_user",
        "core.view_auditlog",
        "plant.view_department",
        "plant.add_department",
        "plant.change_department",
        "plant.add_machine",
        "plant.change_machine",
        "plant.add_part",
        "plant.change_part",
        "plant.add_toolingprogram",
        "plant.change_toolingprogram",
        "plant.add_gauge",
        "plant.change_gauge",
        "plant.view_machinepmcheck",
        *_PLANT_VIEW,
        *_CHECKLIST_VIEW,
        *_PRODUCTION_VIEW,
        *_PRODUCTION_PLAN,
        *_INSPECTION_VIEW,
        *_REVIEW_VIEW,
        *_DISPATCH_VIEW,
        *_MATERIALS_VIEW,
        *_MATERIALS_SUPPLIER,
        *_MRB,
        *_HANDOVER_VIEW,
    ],
    Role.QUALITY_MANAGER: [
        "core.view_auditlog",
        "plant.add_gauge",
        "plant.change_gauge",
        *_PLANT_VIEW,
        *_CHECKLIST_VIEW,
        "checklists.approve_checklisttemplate",
        *_PRODUCTION_VIEW,
        *_INSPECTION_VIEW,
        *_REVIEW_VIEW,
        *_DISPATCH_VIEW,
        *_MATERIALS_VIEW,
        *_MATERIALS_SPEC,
        *_MATERIALS_QA,
        "materials.quarantine_lot",
        "review.issue_concession",
        *_HANDOVER_VIEW,
    ],
    Role.QUALITY_HEAD: [
        "core.view_auditlog",
        *_PLANT_VIEW,
        *_CHECKLIST_VIEW,
        *_PRODUCTION_VIEW,
        *_INSPECTION_VIEW,
        *_REVIEW_VIEW,
        *_DISPATCH_VIEW,
        *_MATERIALS_VIEW,
        *_MATERIALS_QA,
        "materials.quarantine_lot",
        "materials.resolve_sncr",
        "review.quarantine_lot",
        *_MRB,
        *_HANDOVER_VIEW,
    ],
    Role.QUALITY_ENGINEER: [
        *_PLANT_VIEW, *_CHECKLIST_VIEW, *_CHECKLIST_AUTHOR, *_PRODUCTION_VIEW, *_INSPECTION_VIEW,
        *_MATERIALS_VIEW, *_MATERIALS_SPEC, *_MATERIALS_QA,
    ],
    Role.SHIFT_SUPERVISOR: [
        "plant.add_machinepmcheck",
        "plant.view_machinepmcheck",
        *_PLANT_VIEW,
        *_CHECKLIST_VIEW,
        *_PRODUCTION_VIEW,
        *_INSPECTION_VIEW,
        *_REVIEW_VIEW,
        *_DISPATCH_VIEW,
        "review.approve_inspection",
        "review.issue_concession",  # only if authorised: user.can_grant_concession
        "review.quarantine_lot",
        *_HANDOVER_VIEW,
        "handover.create_shifthandover",
        "handover.sign_shifthandover",
        "handover.acknowledge_shifthandover",
    ],
    Role.QA_INSPECTOR: [  # inspection permissions arrive in Step 5
        "plant.view_machine",
        "plant.view_gauge",
        *_CHECKLIST_VIEW,
        *_PRODUCTION_VIEW,
        "production.clear_first_piece",
        "production.log_periodic_check",
        *_INSPECTION_VIEW,
        "inspections.conduct_inspection",
        *_HANDOVER_VIEW,
    ],
    Role.PRODUCTION_LEAD: [
        *_PLANT_VIEW,
        *_CHECKLIST_VIEW,
        *_PRODUCTION_VIEW,
        *_PRODUCTION_PLAN,
        *_INSPECTION_VIEW,
        *_REVIEW_VIEW,
        *_DISPATCH_VIEW,
        *_MATERIALS_VIEW,
        "materials.issue_material",
        *_MRB,
        *_HANDOVER_VIEW,
    ],
    Role.MACHINE_OPERATOR: [
        "plant.add_machinepmcheck",
        "plant.view_machine",
        "plant.view_gauge",
        "production.view_jobtraveler",
        "production.view_traveleroperation",
        "production.view_productionrun",
        "production.operate_traveleroperation",
        *_HANDOVER_VIEW,
        "handover.create_shifthandover",
        "handover.acknowledge_shifthandover",
    ],
    Role.MAINTENANCE_SUPERVISOR: [
        "plant.add_machinepmcheck",
        "plant.view_machinepmcheck",
        *_PLANT_VIEW,
        *_HANDOVER_VIEW,
    ],
    Role.DOCK_CLERK: [
        "materials.view_rawmaterialintake",
        "materials.view_supplier",
        "materials.view_sncr",
        "materials.create_intake",
        "materials.reject_intake",
    ],
    Role.PURCHASING: [*_MATERIALS_VIEW, *_MATERIALS_SUPPLIER, "materials.resolve_sncr"],
    Role.WAREHOUSE_CLERK: [*_DISPATCH_VIEW, *_DISPATCH_ISSUE],
}
