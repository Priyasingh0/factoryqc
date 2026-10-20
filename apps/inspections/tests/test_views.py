from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import TEST_PASSWORD
from apps.core.models import AuditLog
from apps.inspections import services
from apps.inspections.models import DefectPhoto, Inspection, InspectionStatus, Measurement
from apps.production import services as production
from apps.production.tests.helpers import make_machine_ready, make_work_order

from .helpers import (
    InspectionTestCase, begin_first_piece, build_part, field_named, tiny_png,
)
from .test_photos_and_freeze import PdiCase, upload

from django.core.files.uploadedfile import SimpleUploadedFile


class ViewBase(InspectionTestCase):
    def login(self, user):
        self.client.login(username=user.username, password=TEST_PASSWORD)

    def setUp(self):
        # Views use the real clock, so the machine and the Op 10 start are "now".
        now = timezone.now()
        part = build_part(self.engineer, self.manager, number="P-VIEW")
        self.traveler = production.release_work_order(make_work_order(self.lead, part, quantity=2), self.lead)
        self.machine = make_machine_ready(now=now, user=self.operator)
        self.op = self.traveler.operations.get(routing_operation__op_number=10)
        production.start_operation(self.op, self.operator, self.machine, now)

    def start_via_view(self, **override):
        data = {"checklist_type": "FIRST_PIECE", "machine_qr": self.machine.qr_token,
                "traveler_code": self.traveler.barcode}
        data.update(override)
        return self.client.post(reverse("inspections:start"), data, follow=True)


class AccessTests(ViewBase):
    def test_anonymous_redirected(self):
        for name in ("list", "start"):
            r = self.client.get(reverse(f"inspections:{name}"))
            self.assertEqual(r.status_code, 302)
            self.assertIn("login", r["Location"])

    def test_operator_and_clerk_forbidden(self):
        for user in (self.operator, self.dock):
            self.login(user)
            self.assertEqual(self.client.get(reverse("inspections:list")).status_code, 403)
            self.assertEqual(self.client.get(reverse("inspections:start")).status_code, 403)

    def test_supervisor_can_view_but_not_start(self):
        self.login(self.supervisor)
        self.assertEqual(self.client.get(reverse("inspections:list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("inspections:start")).status_code, 403)

    def test_nav_link_follows_permission(self):
        self.login(self.qa)
        self.assertContains(self.client.get(reverse("accounts:home")), "Inspections")
        self.login(self.operator)
        self.assertNotContains(self.client.get(reverse("accounts:home")), 'href="/inspections/"')


class TabletFlowTests(ViewBase):
    def test_start_requires_both_valid_scans(self):
        self.login(self.qa)
        r = self.start_via_view(machine_qr="garbage")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Inspection.objects.exists())
        self.assertContains(r, "Invalid or tampered")

    def test_blank_scans_rejected_by_form(self):
        self.login(self.qa)
        self.start_via_view(traveler_code="")
        self.assertFalse(Inspection.objects.exists())

    def test_end_to_end_first_piece_through_the_views(self):
        self.login(self.qa)
        r = self.start_via_view()
        insp = Inspection.objects.get()
        self.assertRedirects(r, reverse("inspections:detail", args=[insp.pk]))
        field = field_named(insp.template, "Shaft diameter")

        r = self.client.post(reverse("inspections:measure", args=[insp.pk, field.pk]),
                             {"value": "25.01", "gauge_id": "MIC-1"}, follow=True)
        self.assertContains(r, "GREEN")
        self.assertContains(r, "WITHIN TOLERANCE")

        visual = field_named(insp.template, "Burrs removed")
        r = self.client.post(reverse("inspections:measure", args=[insp.pk, visual.pk]), {"result": "PASS"}, follow=True)
        self.assertContains(r, "GREEN")

        r = self.client.post(reverse("inspections:submit", args=[insp.pk]), {"remarks": "ok"}, follow=True)
        self.assertContains(r, "awaiting supervisor approval")
        insp.refresh_from_db()
        self.assertEqual(insp.status, InspectionStatus.SUBMITTED)

    def test_out_of_tolerance_shows_red_and_photo_form(self):
        self.login(self.qa)
        self.start_via_view()
        insp = Inspection.objects.get()
        field = field_named(insp.template, "Shaft diameter")
        r = self.client.post(reverse("inspections:measure", args=[insp.pk, field.pk]),
                             {"value": "26", "gauge_id": "MIC-1"}, follow=True)
        self.assertContains(r, "RED")
        self.assertContains(r, "OUT OF TOLERANCE")
        self.assertContains(r, "Upload photo")
        self.assertContains(r, "0</strong> of 2 required")

    def test_photo_upload_and_two_photo_rule_through_views(self):
        self.login(self.qa)
        self.start_via_view()
        insp = Inspection.objects.get()
        field = field_named(insp.template, "Shaft diameter")
        self.client.post(reverse("inspections:measure", args=[insp.pk, field.pk]), {"value": "26", "gauge_id": "MIC-1"})
        self.client.post(reverse("inspections:measure", args=[insp.pk, field_named(insp.template, "Burrs removed").pk]),
                         {"result": "PASS"})
        m = Measurement.objects.get(field=field)
        r = self.client.post(reverse("inspections:submit", args=[insp.pk]), {}, follow=True)
        self.assertContains(r, "defect photo")
        insp.refresh_from_db()
        self.assertEqual(insp.status, InspectionStatus.IN_PROGRESS)
        for i in range(2):
            self.client.post(reverse("inspections:photo_upload", args=[m.pk]),
                             {"image": SimpleUploadedFile(f"p{i}.png", tiny_png(bytes([i + 1])), "image/png")})
        self.assertEqual(m.photos.count(), 2)
        r = self.client.post(reverse("inspections:submit", args=[insp.pk]), {}, follow=True)
        insp.refresh_from_db()
        self.assertEqual(insp.status, InspectionStatus.SUBMITTED)

    def test_expired_gauge_shows_error_not_crash(self):
        self.login(self.qa)
        self.start_via_view()
        from .helpers import make_gauge
        make_gauge("MIC-OLD", "MICROMETER", expiry=timezone.localdate() - timedelta(days=1))
        insp = Inspection.objects.get()
        field = field_named(insp.template, "Shaft diameter")
        r = self.client.post(reverse("inspections:measure", args=[insp.pk, field.pk]),
                             {"value": "25", "gauge_id": "MIC-OLD"}, follow=True)
        self.assertContains(r, "Expired Tool Calibration")
        self.assertFalse(Measurement.objects.exists())

    def test_visual_requires_an_answer(self):
        self.login(self.qa)
        self.start_via_view()
        insp = Inspection.objects.get()
        r = self.client.post(reverse("inspections:measure", args=[insp.pk, field_named(insp.template, "Burrs removed").pk]),
                             {}, follow=True)
        self.assertContains(r, "Choose Pass or Fail")

    def test_actions_are_post_only(self):
        self.login(self.qa)
        self.start_via_view()
        insp = Inspection.objects.get()
        field = field_named(insp.template, "Shaft diameter")
        for url in (reverse("inspections:measure", args=[insp.pk, field.pk]),
                    reverse("inspections:submit", args=[insp.pk]),
                    reverse("inspections:photo_upload", args=[1])):
            self.assertEqual(self.client.get(url).status_code, 405)

    def test_other_inspector_cannot_work_on_it(self):
        self.login(self.qa)
        self.start_via_view()
        insp = Inspection.objects.get()
        field = field_named(insp.template, "Shaft diameter")
        self.login(self.qa2)
        r = self.client.post(reverse("inspections:measure", args=[insp.pk, field.pk]),
                             {"value": "25", "gauge_id": "MIC-1"}, follow=True)
        self.assertContains(r, "opened this inspection")
        self.assertFalse(Measurement.objects.exists())
        page = self.client.get(reverse("inspections:detail", args=[insp.pk]))
        self.assertNotContains(page, "Record</button>")

    def test_supervisor_cannot_post_actions(self):
        self.login(self.qa)
        self.start_via_view()
        insp = Inspection.objects.get()
        self.login(self.supervisor)
        self.assertEqual(self.client.post(reverse("inspections:submit", args=[insp.pk])).status_code, 403)

    def test_missing_objects_are_404(self):
        self.login(self.qa)
        self.assertEqual(self.client.get(reverse("inspections:detail", args=[9999])).status_code, 404)
        self.assertEqual(self.client.post(reverse("inspections:submit", args=[9999])).status_code, 404)
        self.assertEqual(self.client.get(reverse("inspections:photo", args=[9999])).status_code, 404)

    def test_list_filter_and_open_banner(self):
        self.login(self.qa)
        self.start_via_view()
        r = self.client.get(reverse("inspections:list"))
        self.assertContains(r, "You have an open inspection")
        r = self.client.get(reverse("inspections:list"), {"status": "FROZEN"})
        self.assertContains(r, "No inspections yet")
        self.assertEqual(self.client.get(reverse("inspections:list"), {"status": "bogus"}).status_code, 200)

    def test_traveler_page_links_to_inspection(self):
        self.login(self.qa)
        page = self.client.get(reverse("production:traveler_detail", args=[self.traveler.pk]))
        self.assertContains(page, "Inspect first piece")
        self.assertNotContains(page, "Sign off first piece")  # needs the submitted inspection first


class PhotoServingTests(PdiCase):
    def login(self, user):
        self.client.login(username=user.username, password=TEST_PASSWORD)

    def setUp(self):
        super().setUp()
        m = enter_fail(self)
        self.photo_row = services.add_defect_photo(m, upload(), self.qa, services_now())

    def test_anonymous_cannot_fetch(self):
        r = self.client.get(reverse("inspections:photo", args=[self.photo_row.pk]))
        self.assertEqual(r.status_code, 302)

    def test_operator_cannot_fetch(self):
        self.login(self.operator)
        self.assertEqual(self.client.get(reverse("inspections:photo", args=[self.photo_row.pk])).status_code, 403)

    def test_supervisor_can_fetch_with_safe_headers(self):
        self.login(self.supervisor)
        r = self.client.get(reverse("inspections:photo", args=[self.photo_row.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/png")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        r.close()

    def test_frozen_detail_page_shows_banner(self):
        from .helpers import enter
        enter(self, self.insp, "Bore", "10.5", gauge="VC-104")
        self.login(self.qa_head)
        page = self.client.get(reverse("inspections:detail", args=[self.insp.pk]))
        self.assertContains(page, "CHECKLIST FROZEN")
        self.assertContains(page, "concession is not allowed")


def enter_fail(case):
    from .helpers import enter
    return enter(case, case.insp, "Roughness Ra", "0.9", gauge="RT-9")


def services_now():
    from .helpers import NOW
    return NOW
