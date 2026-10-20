# Factory Operations & Quality Control System

Zapuza Technologies LLP, Python Django Developer Internship evaluation.
Python + Django only; Django templates for the frontend.

## Status

| Step | App | State |
|---|---|---|
| 1 | accounts, core | Done (this README section) |
| 2 | plant | Done (this README section) |
| 3 | checklists | Done (this README section) |
| 4 | production | Done (this README section) |
| 5 | inspections | Done (this README section) |
| 6 | review | Done (this README section) |
| 7 | dispatch | Done (this README section) |
| 8 | materials | Done (this README section) |
| 9 | handover | Done (this README section) |
| 10 | Demo video | Pending final recording |

## Architecture

Python 3.10+ and Django 5.2 only; Django templates for the tablet UI; SQLite for development.

```
 Dock intake -> MTC review -> cleared lot -> issue to work order -> release -> traveler
      materials                                 production (sequence, holds, first piece, counts)
                                                     |              ^
                         inspections (first piece, periodic, final PDI) --> review (approve / concession / quarantine / MRB)
                                                     |
                                       dispatch interlock -> manifest -> gate pass -> packing slip -> invoice -> QR label
                                                     ^
 handover: shift logbook locks a machine until acknowledged; the flow view shows every stage and its blockers
```

Rules that hold everywhere:

- **Models in `models.py`, rules in `services.py`, thin views.** A view only parses the form, calls one service
  function and shows the message of the `BusinessRuleViolation` it raises.
- **Server-side rules.** Buttons are hidden when a user may not use them, but every service re-checks permission,
  state and the PRD rule. Every state-changing endpoint is POST-only.
- **No second source of truth.** The dispatch interlock and the flow view read the state production, inspections,
  review and materials already keep; they add no flags that could be edited.
- **Append-only records.** Audit log, measurements, decisions, concessions, quarantine records, MRB sign-offs,
  MTC readings, lab results, SNCRs, material issues, shipping documents and shift handovers cannot be updated, bulk-updated or
  deleted through the model, the queryset or the admin (read-only admin, no add/change/delete permissions).
- **Blocked attempts are audited** and, where someone must act, the relevant role is notified. Audit and alert are
  written outside the transaction that is rolled back, so they survive the refusal.
- **Defence in depth.** A check made before a write is repeated inside the write transaction after the row is locked.

## Step 1 scope

- Custom user model with 12 roles (PRD section 12, plus Quality Head, Production Lead,
  Maintenance Supervisor, Dock Clerk, Warehouse Clerk, Machine Operator named elsewhere in the PRD)
- One Django Group per role, kept in sync with the user's role; permissions are defined in
  `apps/accounts/roles.py`
- Login/logout using Django authentication (logout is POST-only)
- Append-only `AuditLog` (PRD 13.3): user, timestamp, IP, device serial, previous/new value
- Login, failed login, logout, user creation and role changes are audited automatically
- Signed QR token helper (PRD 13.1), used from later steps
- `Notification` model (PRD 2.2 / 4.3); sending logic is added later

## Step 2 scope (plant)

- Master data: `Department`, `Machine`, `Part`, `ToolingProgram`, `Gauge` (PRD 2.1), maintained in `/admin/`
  with every create/change/delete written to the audit log
- `MachinePMCheck`: one daily/per-shift PM + safety zero-point record per machine (PRD 2.2)
- **PM lock (PRD 2.2):** `plant.services.assert_machine_ready()` blocks job selection unless the current
  shift's PM checklist is complete and the safety zero-point check passed; the Maintenance Supervisor is
  alerted once per machine per shift (email via the console backend + a `Notification` row)
- **Gauge calibration (PRD 2.3):** `plant.services.assert_gauge_usable()` raises "Expired Tool Calibration"
  for expired or inactive gauges; a gauge expiring today is still valid
- Signed station QR token per machine (`Machine.qr_token`, `resolve_machine_from_qr()`), used by Step 5
- Pages: `/plant/machines/` (with Ready/Locked status and PM check form), `/plant/gauges/`, `/plant/parts/`
- Shifts: Morning 06:00-14:00, Evening 14:00-22:00, Night 22:00-06:00 (`plant.services.shift_for`)

Later steps call `assert_machine_ready()` before opening a `transaction.atomic` block (so the alert is not
rolled back) and `assert_gauge_usable()` when a measurement is recorded.

## Step 3 scope (checklists)

- `ChecklistTemplate` (PDI / in-process / first-piece) per part, with revision letters (A, B, ... AA) and a
  workflow **Draft -> Pending approval -> Released -> Superseded** (PRD 4.1)
- `TemplateField`: dimension with +/- tolerance, maximum, minimum, or visual pass/fail; `is_ctq` flag
  (PRD 4.2, 4.3). Tolerances are `DecimalField`s; `TemplateField.evaluate(value)` gives the green/red result
  with inclusive limits (`nominal - lower_tol <= value <= nominal + upper_tol`)
- `BlueprintPin`: numbered, colour-coded position (% of page) on the PDF drawing; a field can point at its pin
- Measured fields must name the gauge type the inspector has to log (PRD 4.2 tool logging)
- **Only a Draft can be edited.** Pending and Released templates (and their fields/pins) are locked in the model,
  in bulk queryset operations and in the admin. A drawing change = a new revision
- Only the Quality Manager can approve or reject, never the template's author; rejecting needs a reason
- Approving a revision supersedes the previous Released one for the same part / type / operation
- `services.get_active_template()` / `assert_template_usable()` give later steps the Released template only
- Pages: `/checklists/` (filter by status), `/checklists/<id>/` (specs, CTQ rows, pins, workflow buttons),
  `/checklists/<id>/drawing/` (PDF, login + permission required)
- Authoring (fields, pins, drawing upload) is done in `/admin/` by Quality Engineers, who need "Staff status"
  ticked on their user. Every create/change/delete is audited.

Not implemented: clicking directly on the PDF to place pins (positions are typed as percentages).

## Step 4 scope (production)

- Work orders (`WorkOrder`) are created in `/admin/` by the Production Lead / Plant Manager. They need a raw-material
  heat number. Once released they cannot be edited or deleted (model, queryset and admin all refuse).
- Each part has a process routing (`RoutingOperation`: op number, hold minutes, first-piece flag, periodic-check
  rule, final-PDI flag). Routing steps used by a released traveler are locked.
- **Release** (`/production/work-orders/<id>/`) is refused unless the heat number is set, the routing exists with
  exactly one final PDI as its last step, and a Released first-piece template (per flagged op) and PDI template exist.
  Release generates the Digital Job Traveler with a signed barcode.
- **Sequence**: a step starts only after every earlier step is completed and signed off. Start needs both scans
  (station QR + traveler barcode, pasted as text), an active idle machine, and a passed PM lock (Step 2).
  Machine, operator, shift, clock-in/out and cycle time are recorded.
- **Hold timer**: signing off a step with `hold_minutes` stamps `hold_until`; the next step is refused with
  "Mandatory Cooling Hold Active (1 hr 22 min remaining)". The check is made on the server on every start.
- **First piece**: flagged steps start in Setup Mode; the part counter and sign-off are refused until a QA Inspector
  signs off (never the operator who started it).
- **Periodic inspection**: interval (minutes) and/or every-N-parts. Due state is computed on demand; 15 minutes of
  grace, then the overdue time is removed from the cycle time when the QA inspector logs the sample. A step cannot
  be finished while a check is due.
- Traceability page `/production/trace/`: heat number -> work orders/serials, serial -> birth certificate.
- Pages: `/production/work-orders/`, `/production/travelers/`, `/production/travelers/<id>/`, `/production/trace/`.

Hooks left for later steps: the final PDI step is completed by the inspection/review workflow (Steps 5-6);
first-piece sign-off will require a submitted first-piece inspection (Step 5); `heat_number` becomes a link to
material intake (Step 8); serial issue (`services.generate_serials`) is called when the traveler completes.
Not implemented: camera scanning (codes are typed or pasted) and live countdown refresh (reload the page).

## Step 5 scope (inspections)

- **Start** (`/inspections/start/`): the inspector pastes the station QR and the traveler barcode (no job search, no
  pick-list). The checklist is the Released template for that lot: the first-piece template of the step in Setup Mode
  (and the scanned machine must be the one running it), or the PDI template once every production step is done and no
  hold is running. A user keeps one open inspection at a time; a step has one open inspection at a time.
- **Readings**: each template field gets one reading with the exact gauge serial. The gauge goes through the plant
  rule (unknown / inactive / "Expired Tool Calibration" are blocked, expiry date itself is still valid) and must be
  the type the template asks for. Decimals only (max 4 places, no floats). Pass/fail is computed by
  `TemplateField.evaluate` (limits inclusive) when the row is saved, never taken from the request. Visual checks
  take Pass/Fail and no gauge. A blocked expired-gauge attempt is written to the audit log.
- **Photos**: an out-of-tolerance reading needs at least 2 different photos (JPEG/PNG/WebP, checked by file
  signature, 8 MB max, stored under a random name, served only to logged-in users with view permission).
- **CTQ failure**: freezes the checklist at once (no more readings, no submit), alerts the Quality Head, writes an
  audit decision. A new inspection of that step is refused while a frozen one exists (Step 6 adds the decision).
- **Submit**: every field must have a reading and every failure its photos; then the status is Awaiting approval,
  Shift Supervisors are notified, and the inspection is locked.
- **Locked for good** (PRD 13.3): readings and photos are insert-only; submitted/frozen inspections cannot be saved,
  bulk-updated or deleted; there are no add/change/delete permissions and the admin is read-only. A reading cannot
  be re-entered, so a typing mistake stays on record and is dealt with at supervisor review.
- **First-piece hook** (PRD 9.1): submitting a first-piece inspection links it to the production run.
  `production.services.clear_first_piece` now refuses unless that inspection is submitted and fully in tolerance.
  After a failed first piece the inspector can inspect again.
- Every start, reading, photo, freeze and submit is in the audit log with user, IP, device, and values.
- Pages: `/inspections/`, `/inspections/<id>/` (green/red rows, orange CTQ badge), `/inspections/start/`.

Not implemented / later: periodic in-process inspections still use the simple
"log sample" button from Step 4, camera scanning, a client-side live colour change (the colour appears as soon as
the reading is posted), clicking blueprint pins.

## Step 6 scope (review / supervisor approval)

- **Queue** (`/review/`): submitted and CTQ-frozen inspections awaiting a decision; sort by urgency, shift or machine.
- **Approve**: only a clean inspection (no out-of-tolerance reading, no CTQ failure). A passed first piece clears
  setup; a passed final PDI completes the last operation, signs it off and issues part serials.
- **Concession**: needs the authority flag (Quality Manager, or a Shift Supervisor with `can_grant_concession`), a
  reason code and a justification of at least 20 characters. Blocked outright when any CTQ field failed. The lot is
  permanently flagged "Concession" in quality history. First pieces cannot be conceded.
- **Quarantine**: moves the traveler to the Digital Rejection Holding Area, locks every downstream operation, frees
  the machine and notifies the MRB. A CTQ-frozen inspection can only be quarantined.
- **MRB** (`/review/quarantine/`): Plant Manager, Quality Head and Production Lead each sign off a disposition
  (Rework, Scrap, Return to vendor, Customer concession). It executes only when all three have signed; customer
  concession also needs the customer approval PDF. Rework creates a new traveler restarting at the chosen operation
  and closes the original.
- Decisions, concessions, quarantine records and sign-offs are append-only; every decision is audited.

Not implemented / later: scrap and return-to-vendor inventory effects (Step 8). Disposition proposals cannot
be edited or withdrawn.

## Step 7 scope (dispatch / shipping interlock)

- **The interlock** (PRD 10.1) is computed from state the earlier steps already keep; it adds no flags of its own.
  A lot is cleared only when all three rules are green:
  1. every operation on the traveler is completed and signed off;
  2. no mandatory hold timer is still running, the lot is not quarantined (open quarantine record or
     quarantined traveler) and not closed by an MRB disposition;
  3. the final PDI inspection is Approved (or released on concession) by Quality, with a supervisor decision
     on record. A PDI that is still in progress, awaiting approval, frozen or rejected keeps the lot locked.
- **Locked documents** (PRD 10.2): the manifest, gate pass, packing slip, commercial invoice and crate QR
  labels all re-check the interlock when they are created and every time they are shown or printed. A blocked
  attempt shows a red lockout page listing each unfulfilled requirement and is written to the audit log.
  The check is repeated again after the lot is locked for writing, so a change between the check and the
  write cannot slip a document through.
- **Manifest** (PRD 11): customer PO (must match the work order's PO), vehicle, batch size and the serial
  numbers shipped. A serial can ship on one manifest only; partial shipments are allowed. Details cannot be
  edited afterwards.
- **Gate pass** marks the manifest Dispatched. Packing slip and invoice are issued once each (later requests
  are audited reprints). Documents record when concession lots are shipped.
- **Customer certificate** (PRD 11.3): each crate label carries a signed QR token that opens `/verify/<token>/`
  without a login, showing the part, PO, heat number, nominal specifications, measured values and the Quality
  sign-off. A forged token shows "not found". If the lot ever turns red, the page says the certificate is not
  valid.
- **Roles**: Warehouse Clerks create manifests and issue documents. Supervisors, Quality, Plant Manager and
  Production Lead can see the board and the green/red check, but cannot issue anything.
- Pages: `/dispatch/` (board), `/dispatch/<traveler>/check/`, `/dispatch/manifests/<id>/`, `/verify/<token>/`.

Not implemented / later: drawn QR images (the signed link is shown as text), prices and tax on the invoice (the
PRD has none), supplier MTCs and lab heat certificates on the customer page (Step 8 adds material intake), and
stock movement for shipped parts.

## Step 8 scope (materials / raw material intake)

- **Intake** (PRD 3.1, `/materials/new/`): the Dock Clerk logs supplier, PO, material grade, heat number and
  quantity. The record **cannot be saved without a readable MTC** (PDF, JPEG or PNG, at most 10 MB, stored under
  a random name; the tablet camera can be used) **and a verified heat number**: the heat number printed on the
  MTC is typed a second time and must match. The same heat on the same PO from the same supplier cannot be received twice.
- **MTC cross-check** (PRD 3.2): each grade holds its chemical and mechanical limits (Carbon, Tensile strength...).
  A Quality person enters the certified values; the system checks every one against the limits (inclusive) and
  keeps the limits that applied. Someone other than the receiver must do this. In spec: the lot is **Cleared**, or
  **In Lab Testing** when the grade needs internal testing (a sample tag is printed, and Quality is alerted).
  A lab pass clears it; a lab fail quarantines it.
- **Reject Intake / SNCR** (PRD 3.3): rejecting at the dock, an out-of-spec MTC, or a lab fail tags the lot
  **Supplier Quarantine**, raises an **SNCR**, and notifies Purchasing, the Quality Manager and the Quality Head.
  A quarantined lot can leave quarantine only by being **returned to vendor** (debit note reference; the Dock Clerk
  is told) or **scrapped** (cost code); it can never be cleared again.
- **Recall** (PRD 7.2): the Quality Head or Quality Manager can quarantine an already cleared lot. Every work
  order that drew on it is listed in the alert to the Production Lead, and work on those orders stops.
- **Issue to work orders**: the Production Lead issues cleared stock of the work order's heat number (pick the lot or
  scan its bundle barcode). Quarantined, rejected, returned, scrapped or unreviewed stock is refused under any
  circumstances, and the attempt is audited. Stock cannot be over-issued.
- **Hooks in earlier steps**: a work order **cannot be released** until cleared material is issued to it; no
  operation can **start, count parts, finish or close the final PDI** while its material lot is not cleared; the
  **dispatch interlock** treats a quarantined lot as a quality hold; the **traceability page** lists supplier
  lots per heat number and the birth certificate shows the lot; the customer certificate shows the certified MTC values.
- **Labels**: sample tag while in the lab; the signed bundle barcode only after clearance.
- **Roles**: a new **Purchasing Officer** role (PRD 3.3 names Purchasing). Dock Clerk: log and reject intake.
  Quality Engineer / Manager / Head: review MTC, lab results; the Head and Manager can quarantine a cleared
  lot. Purchasing and the Quality Head close SNCRs. Supplier and grade master data are maintained in the admin
  site by Purchasing / Plant Manager (suppliers) and the Quality Engineer / Manager (grades and limits).
- Intake data, MTC readings, lab results, SNCRs and issues cannot be edited or deleted; every step is audited.

Not implemented / later: scrap and return-to-vendor stock accounting beyond the lot status, supplier ratings, drawn
barcode images (the signed code is shown as text), and the MTC file on the public customer page (only the verified
values are shown).

## Step 9 scope (handover, final integration, floor monitor)

**Shift Handover Logbook (PRD 12.2)** - `/handover/`

- At a shift change the outgoing machine operator (or supervisor) completes the **Digital Shift Handover** form for
  the machine. The system fills in what it knows and the worker cannot change it: the running jobs and their status,
  parts recorded this shift, open quality alerts (open quarantines and CTQ-frozen inspections) and pending
  inspection reviews. The worker enters the scrap count, tool wear status and machine health. Anything not normal needs a
  description (10+ characters), and a job still running needs a status note for the next shift. All the problems are
  listed at once on the tablet.
- A **different supervisor countersigns** the form (never its author). Then the **incoming worker acknowledges** it
  (never the person who handed over or the one who countersigned).
- **The lock:** once a handover exists for an earlier shift and is not acknowledged, nobody can **start, count parts
  on or finish an operation** on that machine. The tablet shows exactly what is missing (countersign or
  acknowledge). The attempt is audited and the Shift Supervisors are alerted. Handovers are not demanded where none was
  filed, so an idle machine is never locked by paperwork nobody wrote.
- Maintenance is alerted when the machine is down or degraded, or tooling must be changed.
- The three records (form, supervisor sign-off, acknowledgement) are insert-only; the status is derived from which
  records exist, so there is no status field to tamper with. One form per machine per shift (database constraint).

**End-to-end release view** - `/flow/` and `/flow/travelers/<id>/`

- Shows each lot as five stages: Material, Production, Quality (holds and quarantine), Final PDI, Dispatch, each green or
  blocked with the exact reasons. It is composed from the dispatch interlock, the material rules and production state, and the
  tests check that it can never show green for a lot the interlock would stop.

**Live floor monitor (PRD 8.3)** - `/flow/monitor/`

- Elapsed, active machining, hold and idle queue time for every active lot; lots waiting longer than 4 hours between
  stations are listed as stuck; each machine shows idle, running or slow (more than 1.5 times the usual cycle of
  that step, once at least 3 runs exist). Everything is calculated from the operation timestamps on demand.

**Integration checked end to end** (`apps/handover/tests/test_end_to_end.py`): a heat is received, reviewed, issued, released,
machined, inspected, approved and shipped using only the real services, with each gate refusing at each step, and a
later recall locking the documents again.

Roles: Machine Operator and Shift Supervisor complete and acknowledge; only Shift Supervisors countersign; Plant
Manager, Quality Manager, Quality Head, Production Lead, QA Inspector and Maintenance Supervisor can read the log.

## User interface (design system)

One stylesheet (`static/css/app.css`) and one small script (`static/js/app.js`, progressive enhancement only) style every page; there is no front-end framework. The shell is `templates/base.html` with partials in `templates/partials/` (sidebar, top bar, toasts, confirm dialog, empty state, pagination, form fields). The sidebar is built in Python from the user's permissions (`apps/core/navigation.py`), never from role names, and the notification count comes from `apps/core/context_processors.py` (unseen = created after the user last opened `/notifications/`; kept in the session, no model change). Status colours live in one place, the `{% badge %}` tag in `apps/core/templatetags/ui.py`. The dashboard (`apps/core/dashboard.py`) reads live tables only and shows a block only if the user may open that module. `apps/dispatch/presentation.py` names the board category (Ready, Pending final PDI, Quality hold, Material hold, Blocked) for display; it never decides clearance. Irreversible actions carry `data-confirm`, which opens a confirmation dialog.

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py check
python manage.py makemigrations accounts core plant checklists production inspections review dispatch materials handover
python manage.py migrate
python manage.py setup_roles     # creates role groups and permissions (re-run after each step)
python manage.py createsuperuser # asks for username, email, employee_id, password
python manage.py runserver
```

Open http://127.0.0.1:8000/

`AUTH_USER_MODEL` is a custom model, so the first `migrate` must happen after the
`accounts` migration exists. If you ever migrated before that, delete `db.sqlite3` and the
generated `apps/*/migrations/0*.py` files and start again.

## Tests

```bash
python manage.py test                      # 838 tests
python manage.py test apps.handover        # Step 9 only
```

Beyond the scenario tests, the critical rules were each broken on purpose (one line changed at a time) to
confirm that at least one test fails: 48 deliberate one-line breakages of the Step 9 rules (and the production hooks they depend on) were each caught by at least one test. A surviving change would mean a rule that no test protects.

## Demo video

Screen recording with voice narration explaining the project structure, key business rules, and a live walkthrough of the implemented workflows.

**Video:** **Video:** [Watch the Demo Video](https://drive.google.com/file/d/1sy2Jnw8G3bjM0ZsiS9Ci-sBrcgQ0ciED/view?usp=sharing)

## Roles

| Role | Main work in the system |
|---|---|
| Plant Manager | Master data, users, MRB member, read-only view of everything |
| Quality Engineer | Checklist templates, material grade limits, MTC review |
| Quality Manager | Approves templates, concessions, MTC review, recalls a cleared lot |
| Quality Head | CTQ alerts, MRB member, recalls a cleared lot, closes SNCRs |
| Shift Supervisor | Approves or quarantines inspections, countersigns shift handovers |
| QA Floor Inspector | First-piece, periodic and final inspections |
| Production Lead | Work orders, issues material, MRB member |
| Machine Operator | Runs operations, PM checks, completes and acknowledges shift handovers |
| Maintenance Supervisor | PM checks, machine alerts |
| Dock Clerk | Raw material intake and dock rejection |
| Warehouse Clerk | Manifests, gate pass, packing slip, invoice, labels |
| Purchasing Officer | Supplier master data, SNCRs and vendor returns |

Create users in `/admin/`. A superuser created from the command line gets the Plant Manager role.
Views check Django *permissions*, never role names; `apps/accounts/roles.py` is the single
role-to-permission table.

## End-to-end workflow

1. **Dock Clerk** logs the delivery with the MTC file and heat number; the heat number is typed a second time from the MTC.
2. **Quality** enters the certified values; out of limits means Supplier Quarantine and an SNCR, in limits means Cleared
   (or In Lab Testing first when the grade needs it).
3. **Production Lead** issues cleared stock to the work order (matching heat number) and **releases** it. Release is refused
   without cleared, issued material, a routing with one final PDI and Released checklists. This creates the digital traveler.
4. **Operators** scan the station QR and the traveler barcode and run the steps in numerical order. The PM lock, mandatory hold
   timers, first-piece sign-off, periodic-check prompts and the shift-handover lock each stop work on the server.
5. **QA Inspectors** record readings with calibrated gauges. A CTQ failure freezes the checklist and alerts the Quality Head.
6. **Shift Supervisors** approve, issue a concession (Quality Manager or authorised supervisor), or quarantine. MRB
   (Plant Manager, Quality Head, Production Lead) dispositions quarantined lots.
7. **At every shift change** the outgoing worker completes the handover, a supervisor countersigns and the incoming worker acknowledges.
8. **Warehouse Clerk** can create the manifest, gate pass, packing slip, invoice and QR labels only while the dispatch interlock is
   green; the customer scans the label to see the certificate.
9. `/flow/` shows where every lot stands and why it is blocked; `/flow/monitor/` shows the live floor.

## Known limitations (intentionally not built)

- **Offline tablet storage** (PRD 13.2): the pages need the network. Offline capture and later sync would need a
  service worker and a conflict policy that keeps the append-only rules.
- **Camera scanning and drawn QR/barcode images:** codes are typed or pasted, and signed tokens are shown as text or links.
  Clicking a blueprint pin directly on the drawing is not implemented (positions are typed).
- **Physical-world steps:** physical scrap-bin verification, rollout phases, champion training and pilot sign-offs
  (PRD 14) are procedures, not software.
- **Stock accounting:** material is tracked by lot status and issues, not by inventory levels or costs; no supplier ratings.
- **Live refresh:** pages show the situation when loaded (countdowns and the floor monitor need a reload).
- **Transport security and encryption at rest** are deployment settings (HTTPS, database/disk encryption, revoking
  UPDATE/DELETE on the audit table), not application code.
- The shift lock only applies where a handover was filed for an earlier shift. A job left running across a shift change
  with no handover at all is flagged on the board ("Handover missing") but does not lock the machine, so the handover is
  mandatory in the sense that supervisors are shown every gap, not that the system can force a form nobody wrote.

## Project layout

```
config/            settings, root urls
apps/accounts/     User, Role, role groups, login/logout, user list
apps/core/         AuditLog, Notification (+ notify_role), QR signing, audited admin mixin, request context middleware
apps/plant/        Department, Machine, MachinePMCheck, Part, ToolingProgram, Gauge, PM-lock and gauge rules
apps/checklists/   ChecklistTemplate, TemplateField, BlueprintPin, approval workflow, tolerance evaluation
apps/inspections/  Inspection, Measurement, DefectPhoto, scan-to-start, gauge/tolerance/photo/CTQ-freeze rules
apps/review/       SupervisorDecision, Concession, QuarantineRecord, MRBDisposition, MRBSignoff, review queue
apps/materials/    Supplier, MaterialGrade, GradeProperty, RawMaterialIntake, MTCReading, LabResult, SNCR, MaterialIssue
apps/handover/     ShiftHandover, HandoverSignoff, HandoverAcknowledgement, machine lock, release flow, floor monitor
apps/dispatch/     ShipmentManifest, ManifestSerial, GatePass, ShippingDocument, ShipmentLabel, shipping interlock
apps/production/   WorkOrder, RoutingOperation, JobTraveler, TravelerOperation, ProductionRun, PartSerial, hold/first-piece/periodic-check rules
templates/         base.html, 403.html, one folder per app
```

## Security notes

- The audit log cannot be edited or deleted through the app, queryset or admin. For
  production, also revoke UPDATE/DELETE on its table at database level.
- `X-Device-Serial` is informational; a production deployment should set it from the tablet/MDM.
- Set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0` and `DJANGO_ALLOWED_HOSTS` in production.
  QR tokens are signed with the secret key, so changing it invalidates existing QR labels.
