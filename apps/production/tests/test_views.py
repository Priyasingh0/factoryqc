from datetime import timedelta

from django.contrib.admin.sites import site as admin_site
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import TEST_PASSWORD
from apps.plant.tests.helpers import make_machine
from apps.production import services
from apps.production.models import JobTraveler, ProductionRun, TravelerOperation, WorkOrder

from .helpers import (
    NOW, ProductionTestCase, attach_passed_first_piece, make_machine_ready, make_ready_part, make_work_order, op_at, released_traveler,
)

OP = TravelerOperation.Status


class ViewTests(ProductionTestCase):
    def login(self, user):
        self.client.login(username=user.username, password=TEST_PASSWORD)

    def test_anonymous_redirected_to_login(self):
        for name, args in (("work_order_list", ()), ("traveler_list", ()), ("trace", ())):
            r = self.client.get(reverse(f"production:{name}", args=args))
            self.assertEqual(r.status_code, 302)
            self.assertIn("login", r["Location"])

    def test_dock_clerk_forbidden(self):
        self.login(self.dock)
        for name in ("work_order_list", "traveler_list", "trace"):
            self.assertEqual(self.client.get(reverse(f"production:{name}")).status_code, 403)

    def test_lead_sees_work_orders_and_release_form(self):
        part = make_ready_part(self.engineer, self.manager)
        wo = make_work_order(self.lead, part)
        self.login(self.lead)
        self.assertContains(self.client.get(reverse("production:work_order_list")), wo.number)
        page = self.client.get(reverse("production:work_order_detail", args=[wo.pk]))
        self.assertContains(page, "Release &amp; generate traveler")

    def test_release_via_view_creates_traveler(self):
        part = make_ready_part(self.engineer, self.manager)
        wo = make_work_order(self.lead, part)
        self.login(self.lead)
        r = self.client.post(reverse("production:release", args=[wo.pk]))
        traveler = JobTraveler.objects.get()
        self.assertRedirects(r, reverse("production:traveler_detail", args=[traveler.pk]))

    def test_release_problem_shown_not_crashed(self):
        wo = make_work_order(self.lead, make_ready_part(self.engineer, self.manager), heat="")
        self.login(self.lead)
        r = self.client.post(reverse("production:release", args=[wo.pk]), follow=True)
        self.assertContains(r, "heat number")
        self.assertFalse(JobTraveler.objects.exists())
        self.assertContains(self.client.get(reverse("production:work_order_detail", args=[wo.pk])), "Not ready to release")

    def test_release_requires_post_and_permission(self):
        wo = make_work_order(self.lead, make_ready_part(self.engineer, self.manager))
        url = reverse("production:release", args=[wo.pk])
        self.login(self.engineer)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.login(self.lead)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertFalse(WorkOrder.objects.get(pk=wo.pk).is_released)

    def test_release_button_hidden_from_viewers(self):
        wo = make_work_order(self.lead, make_ready_part(self.engineer, self.manager))
        self.login(self.manager)
        self.assertNotContains(self.client.get(reverse("production:work_order_detail", args=[wo.pk])), "Release &amp;")

    def test_nav_links_follow_permissions(self):
        self.login(self.operator)
        page = self.client.get(reverse("accounts:home"))
        self.assertContains(page, "Travelers")
        self.assertNotContains(page, "Work orders")


class ShopfloorViewTests(ProductionTestCase):
    def setUp(self):
        self.traveler, self.machine = released_traveler(self, quantity=3, ready_at=timezone.now())
        self.op10 = op_at(self.traveler, 10)

    def login(self, user):
        self.client.login(username=user.username, password=TEST_PASSWORD)

    def start(self, **override):
        data = {"machine_qr": self.machine.qr_token, "traveler_code": self.traveler.barcode}
        data.update(override)
        return self.client.post(reverse("production:start", args=[self.op10.pk]), data, follow=True)

    def test_detail_shows_start_form_only_for_next_step(self):
        self.login(self.operator)
        page = self.client.get(reverse("production:traveler_detail", args=[self.traveler.pk]))
        self.assertEqual(page.content.decode().count("Start operation</button>"), 1)

    def test_full_flow_through_the_views(self):
        self.login(self.operator)
        r = self.start()
        self.assertContains(r, "Operation started")
        self.assertContains(r, "Setup mode")
        # counter is locked
        r = self.client.post(reverse("production:parts", args=[self.op10.pk]), {"count": 1}, follow=True)
        self.assertContains(r, "Setup Mode")
        # QA signs off (needs the submitted first-piece inspection, attached here as Step 5 will do)
        attach_passed_first_piece(ProductionRun.objects.get(traveler_operation=self.op10), self.qa)
        self.login(self.qa)
        r = self.client.post(reverse("production:first_piece", args=[self.op10.pk]), follow=True)
        self.assertContains(r, "First piece signed off")
        # operator counts and finishes
        self.login(self.operator)
        self.client.post(reverse("production:parts", args=[self.op10.pk]), {"count": 3})
        r = self.client.post(reverse("production:complete", args=[self.op10.pk]), follow=True)
        self.assertContains(r, "Operation signed off")
        self.assertEqual(TravelerOperation.objects.get(pk=self.op10.pk).status, OP.COMPLETED)

    def test_bad_scan_is_a_message_not_a_crash(self):
        self.login(self.operator)
        r = self.start(machine_qr="garbage")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(TravelerOperation.objects.get(pk=self.op10.pk).status, OP.PENDING)
        self.assertContains(r, "error")

    def test_blank_scan_fields_refused(self):
        self.login(self.operator)
        r = self.start(machine_qr="")
        self.assertContains(r, "Scan both")

    def test_invalid_part_count_refused(self):
        self.login(self.operator)
        self.start()
        r = self.client.post(reverse("production:parts", args=[self.op10.pk]), {"count": "x"}, follow=True)
        self.assertContains(r, "whole number")

    def test_permissions_enforced_on_every_action(self):
        self.login(self.qa)
        for name in ("start", "parts", "complete"):
            self.assertEqual(self.client.post(reverse(f"production:{name}", args=[self.op10.pk])).status_code, 403)
        self.login(self.operator)
        for name in ("first_piece", "periodic_check"):
            self.assertEqual(self.client.post(reverse(f"production:{name}", args=[self.op10.pk])).status_code, 403)

    def test_actions_reject_get(self):
        self.login(self.operator)
        for name in ("start", "parts", "complete"):
            self.assertEqual(self.client.get(reverse(f"production:{name}", args=[self.op10.pk])).status_code, 405)
        self.login(self.qa)
        for name in ("first_piece", "periodic_check"):
            self.assertEqual(self.client.get(reverse(f"production:{name}", args=[self.op10.pk])).status_code, 405)

    def test_missing_operation_is_404(self):
        self.login(self.operator)
        self.assertEqual(self.client.post(reverse("production:complete", args=[99999])).status_code, 404)

    def test_hold_banner_shown_on_traveler(self):
        t = self.traveler
        TravelerOperation.objects.filter(pk=op_at(t, 20).pk).update(
            status=OP.COMPLETED, hold_until=timezone.now() + timedelta(minutes=30))
        self.login(self.operator)
        page = self.client.get(reverse("production:traveler_detail", args=[t.pk]))
        self.assertContains(page, "Mandatory Cooling Hold Active")

    def test_trace_page(self):
        t = self.traveler
        TravelerOperation.objects.filter(traveler=t).update(status=OP.COMPLETED)
        JobTraveler.objects.filter(pk=t.pk).update(status=JobTraveler.Status.COMPLETED)
        t.refresh_from_db()
        services.generate_serials(t, self.lead)
        self.login(self.qa)
        r = self.client.get(reverse("production:trace"), {"kind": "heat", "q": "h-7788"})
        self.assertContains(r, "WO-1")
        self.assertContains(r, "WO-1-0001")
        r = self.client.get(reverse("production:trace"), {"kind": "serial", "q": "WO-1-0002"})
        self.assertContains(r, "Birth certificate")
        self.assertContains(r, "H-7788")
        r = self.client.get(reverse("production:trace"), {"kind": "serial", "q": "nope"})
        self.assertContains(r, "No part found")
        r = self.client.get(reverse("production:trace"), {"kind": "heat", "q": "zzz"})
        self.assertContains(r, "No work orders found")
