from django.urls import reverse

from apps.accounts.tests.helpers import TEST_PASSWORD
from apps.core.models import AuditLog
from apps.dispatch.tests.helpers import AFTER, DispatchTestCase, approved_lot, make_red
from apps.handover import services
from apps.handover.models import HandoverAcknowledgement, HandoverSignoff, ShiftHandover
from apps.production.models import ProductionRun
from apps.production.tests.helpers import NOW

from .base import EVENING, HANDOVER_AT, HandoverTestCase


class ViewBase(HandoverTestCase):
    def login(self, user):
        self.client.login(username=user.username, password=TEST_PASSWORD)

    def form(self, **kw):
        data = dict(scrap_count="1", tool_wear="OK", machine_health="GOOD", tool_wear_notes="", machine_health_notes="",
                    job_status_notes="Op 10 running, two parts counted, no issues.")
        data.update(kw)
        return data


class AccessTests(ViewBase):
    def test_anonymous_users_are_sent_to_login(self):
        _, machine, _ = self.running_job()
        h = self.hand_over(machine)
        for url in (reverse("handover:board"), reverse("handover:new", args=[machine.pk]),
                    reverse("handover:detail", args=[h.pk]), reverse("handover:flow_list")):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("login", r["Location"])
        for name in ("sign", "acknowledge"):
            self.assertEqual(self.client.post(reverse(f"handover:{name}", args=[h.pk])).status_code, 302)

    def test_roles_without_permission_get_403(self):
        _, machine, _ = self.running_job()
        h = self.hand_over(machine)
        self.login(self.warehouse)
        for url in (reverse("handover:board"), reverse("handover:detail", args=[h.pk]), reverse("handover:flow_list")):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.login(self.qa)
        self.assertEqual(self.client.get(reverse("handover:new", args=[machine.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("handover:sign", args=[h.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("handover:acknowledge", args=[h.pk])).status_code, 403)
        self.assertFalse(HandoverSignoff.objects.exists() or HandoverAcknowledgement.objects.exists())

    def test_action_endpoints_do_not_accept_get(self):
        _, machine, _ = self.running_job()
        h = self.hand_over(machine)
        self.login(self.sup)
        for name in ("sign", "acknowledge"):
            self.assertEqual(self.client.get(reverse(f"handover:{name}", args=[h.pk])).status_code, 405)
        self.assertFalse(HandoverSignoff.objects.exists())

    def test_nav_links_follow_permission(self):
        self.login(self.operator)
        page = self.client.get(reverse("accounts:home"))
        self.assertContains(page, 'href="/handover/"')
        self.assertContains(page, 'href="/flow/"')
        self.login(self.warehouse)
        page = self.client.get(reverse("accounts:home"))
        self.assertNotContains(page, 'href="/handover/"')


class WorkflowViewTests(ViewBase):
    def test_operator_completes_the_form_and_sees_the_system_figures(self):
        _, machine, _ = self.running_job(parts=2)
        self.login(self.operator)
        page = self.client.get(reverse("handover:new", args=[machine.pk]))
        self.assertContains(page, "Read from the system")
        self.assertContains(page, "WO-1")
        r = self.client.post(reverse("handover:new", args=[machine.pk]), self.form())
        h = ShiftHandover.objects.get()
        self.assertRedirects(r, reverse("handover:detail", args=[h.pk]))
        self.assertEqual((h.scrap_count, h.outgoing_by), (1, self.operator))

    def test_an_incomplete_form_is_refused_with_the_reasons(self):
        _, machine, _ = self.running_job()
        self.login(self.operator)
        r = self.client.post(reverse("handover:new", args=[machine.pk]), self.form(tool_wear="WATCH"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Describe the tool wear")
        self.assertFalse(ShiftHandover.objects.exists())

    def test_client_cannot_inject_the_system_figures(self):
        _, machine, _ = self.running_job(parts=2)
        self.login(self.operator)
        self.client.post(reverse("handover:new", args=[machine.pk]),
                         self.form(parts_produced="999", open_quality_alerts="0", pending_pdi_reviews="0"))
        h = ShiftHandover.objects.get()
        self.assertEqual(h.parts_produced, 2)

    def test_sign_then_acknowledge_through_the_buttons(self):
        _, machine, _ = self.running_job()
        h = self.hand_over(machine)
        self.login(self.sup)
        page = self.client.get(reverse("handover:detail", args=[h.pk]))
        self.assertContains(page, "Countersign handover")
        self.assertNotContains(page, "acknowledge</button>")
        self.client.post(reverse("handover:sign", args=[h.pk]), {"remarks": "ok"})
        self.assertEqual(HandoverSignoff.objects.get().signed_by, self.sup)
        self.login(self.operator2)
        page = self.client.get(reverse("handover:detail", args=[h.pk]))
        self.assertContains(page, "I reviewed this: acknowledge")
        self.client.post(reverse("handover:acknowledge", args=[h.pk]), {})
        self.assertEqual(HandoverAcknowledgement.objects.get().acknowledged_by, self.operator2)

    def test_buttons_hidden_from_the_people_who_may_not_use_them(self):
        _, machine, _ = self.running_job()
        h = self.hand_over(machine)
        self.login(self.operator)
        self.assertNotContains(self.client.get(reverse("handover:detail", args=[h.pk])), "Countersign handover")
        services.sign_handover(h, self.sup, "", HANDOVER_AT)
        self.login(self.operator)
        self.assertNotContains(self.client.get(reverse("handover:detail", args=[h.pk])), "I reviewed this")

    def test_view_shows_service_refusals_and_changes_nothing(self):
        _, machine, _ = self.running_job()
        h = self.hand_over(machine, user=self.sup)
        self.login(self.sup)
        r = self.client.post(reverse("handover:sign", args=[h.pk]), follow=True)
        self.assertContains(r, "another supervisor")
        self.assertFalse(HandoverSignoff.objects.exists())

    def test_board_shows_locked_machines_and_pending_items(self):
        _, machine, _ = self.running_job()
        self.hand_over(machine)
        self.login(self.sup)
        page = self.client.get(reverse("handover:board"))
        self.assertContains(page, "Awaiting supervisor sign-off")
        self.assertContains(page, "CNC-04")

    def test_production_view_shows_the_handover_block_and_audits_it(self):
        _, machine, op = self.running_job(parts=2)
        self.hand_over(machine)
        self.ready_for_evening(machine)
        from unittest import mock
        from django.utils import timezone
        self.login(self.operator2)
        with mock.patch.object(timezone, "now", return_value=EVENING):
            r = self.client.post(reverse("production:parts", args=[op.pk]), {"count": "1"}, follow=True)
        self.assertContains(r, "Shift handover pending")
        self.assertEqual(ProductionRun.objects.get(traveler_operation=op).parts_produced, 2)
        self.assertTrue(AuditLog.objects.filter(field_name="shift_handover", new_value="BLOCKED").exists())


class FlowViewTests(DispatchTestCase):
    def login(self, user):
        self.client.login(username=user.username, password=TEST_PASSWORD)

    def test_flow_detail_shows_exactly_what_blocks_the_lot(self):
        lot = approved_lot(self)
        make_red(lot, 20)
        self.login(self.lead)
        page = self.client.get(reverse("handover:flow_detail", args=[lot.pk]))
        self.assertContains(page, "Heat treatment")
        self.assertContains(page, "Blocked")
        self.assertContains(page, "Dispatch stays locked")

    def test_flow_detail_for_a_cleared_lot_says_so(self):
        lot = approved_lot(self)
        self.login(self.lead)
        self.assertContains(self.client.get(reverse("handover:flow_detail", args=[lot.pk])), "Cleared for dispatch")

    def test_flow_list_has_a_row_per_lot(self):
        lot = approved_lot(self)
        self.login(self.operator)
        page = self.client.get(reverse("handover:flow_list"))
        self.assertContains(page, lot.work_order.number)
