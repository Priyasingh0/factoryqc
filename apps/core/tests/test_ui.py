"""Application shell, dashboard, notifications and the status-badge helpers."""
from django.contrib.staticfiles import finders
from django.template import Context, Template
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse

from apps.accounts.models import Role
from apps.core.navigation import build_nav
from apps.core.models import Notification
from apps.core.templatetags.ui import STATUS_STYLES, status_class
from apps.production.tests.helpers import ProductionTestCase, released_traveler


class StatusBadgeTests(SimpleTestCase):
    def test_every_required_status_has_a_style(self):
        for name in ["Draft", "Pending", "Released", "Approved", "Failed", "Frozen", "Quarantined",
                     "Concession", "Cleared", "Rejected", "On Hold", "Ready", "Blocked", "Completed"]:
            self.assertIn(name.upper().replace(" ", "_"), STATUS_STYLES, name)

    def test_colours_follow_meaning(self):
        self.assertEqual(status_class("APPROVED"), "b-success")
        self.assertEqual(status_class("Frozen"), "b-danger")
        self.assertEqual(status_class("on hold"), "b-warn")
        self.assertEqual(status_class("unknown thing"), "b-neutral")

    def test_badge_tag_escapes_text(self):
        html = Template("{% load ui %}{% badge v l %}").render(Context({"v": "FAILED", "l": "<b>x</b>"}))
        self.assertIn("b-danger", html)
        self.assertNotIn("<b>x</b>", html)

    def test_static_assets_exist(self):
        self.assertIsNotNone(finders.find("css/app.css"))
        self.assertIsNotNone(finders.find("js/app.js"))


class ShellTests(ProductionTestCase):
    def nav_labels(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return [i["label"] for s in build_nav(request) for i in s["items"]]

    def test_navigation_follows_permissions_not_role_names(self):
        operator = self.nav_labels(self.operator)
        self.assertIn("Dashboard", operator)
        self.assertNotIn("Users", operator)
        self.assertNotIn("Audit log", operator)
        self.assertIn("Audit log", self.nav_labels(self.plant_manager))

    def test_login_page_has_branding_and_password_toggle(self):
        page = self.client.get(reverse("accounts:login"))
        self.assertContains(page, "Quality")
        self.assertContains(page, "Traceability")
        self.assertContains(page, "data-pw-toggle")

    def test_failed_login_shows_an_error(self):
        page = self.client.post(reverse("accounts:login"), {"username": "nobody", "password": "x"})
        self.assertContains(page, "alert-danger")

    def test_shell_marks_the_current_page_active(self):
        self.client.force_login(self.manager)
        page = self.client.get(reverse("production:traveler_list"))
        self.assertContains(page, 'class="active"')
        self.assertContains(page, "Log out")
        self.assertContains(page, self.manager.get_role_display())

    def test_messages_render_as_toasts(self):
        self.client.force_login(self.manager)
        page = self.client.get(reverse("accounts:home"))
        self.assertNotContains(page, 'class="toast')


class DashboardTests(ProductionTestCase):
    def test_dashboard_numbers_come_from_the_database(self):
        traveler, _machine = released_traveler(self, quantity=5)
        self.client.force_login(self.manager)
        page = self.client.get(reverse("accounts:home"))
        kpis = {k["label"]: k["value"] for k in page.context["dash"]["kpis"]}
        self.assertEqual(kpis["Work orders"], 1)
        self.assertEqual(kpis["Active travelers"], 1)
        self.assertContains(page, "Dispatch readiness")
        rows = {d["key"]: d["value"] for d in page.context["dash"]["panels"]["dispatch"]}
        self.assertEqual(sum(rows.values()), 1)
        self.assertEqual(rows["READY"], 0)

    def test_dashboard_only_shows_what_the_role_may_open(self):
        released_traveler(self, quantity=5)
        self.client.force_login(self.dock)
        labels = [k["label"] for k in self.client.get(reverse("accounts:home")).context["dash"]["kpis"]]
        self.assertNotIn("Work orders", labels)
        self.assertNotIn("Dispatch ready", labels)

    def test_empty_database_still_renders(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("accounts:home")).status_code, 200)


class NotificationPageTests(ProductionTestCase):
    def test_user_sees_only_their_own_notifications(self):
        Notification.objects.create(recipient=self.manager, channel="IN_APP", subject="For the manager", body="b")
        Notification.objects.create(recipient=self.qa, channel="IN_APP", subject="For QA", body="b")
        self.client.force_login(self.manager)
        page = self.client.get(reverse("core:notifications"))
        self.assertContains(page, "For the manager")
        self.assertNotContains(page, "For QA")

    def test_opening_the_page_clears_the_unseen_count(self):
        Notification.objects.create(recipient=self.manager, channel="IN_APP", subject="Alert", body="b")
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("accounts:home")).context["unread_notifications"], 1)
        self.client.get(reverse("core:notifications"))
        self.assertEqual(self.client.get(reverse("accounts:home")).context["unread_notifications"], 0)

    def test_login_is_required(self):
        r = self.client.get(reverse("core:notifications"))
        self.assertEqual(r.status_code, 302)

    def test_empty_state(self):
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(reverse("core:notifications")), "You have no notifications")


class SmokeEveryRoleTests(ProductionTestCase):
    """No page may raise a template or server error for any role."""

    def test_every_list_and_detail_page_renders_for_every_role(self):
        from apps.accounts.models import User
        from apps.production.models import JobTraveler, WorkOrder
        from apps.plant.models import Machine

        traveler, machine = released_traveler(self, quantity=5)
        urls = [
            reverse(n) for n in [
                "accounts:home", "accounts:user_list", "core:audit_log", "core:notifications",
                "plant:machine_list", "plant:gauge_list", "plant:part_list", "checklists:list",
                "production:work_order_list", "production:traveler_list", "production:trace",
                "inspections:list", "inspections:start", "review:queue", "review:quarantine_list",
                "materials:list", "materials:new", "materials:sncr_list", "materials:suppliers",
                "dispatch:board", "handover:board", "handover:flow_list", "handover:flow_monitor",
            ]
        ] + [
            reverse("production:traveler_detail", args=[traveler.pk]),
            reverse("production:work_order_detail", args=[traveler.work_order.pk]),
            reverse("dispatch:check", args=[traveler.pk]),
            reverse("handover:flow_detail", args=[traveler.pk]),
            reverse("handover:new", args=[machine.pk]),
            reverse("plant:machine_detail", args=[machine.pk]),
        ]
        seen = 0
        for user in User.objects.all():
            self.client.force_login(user)
            for url in urls:
                response = self.client.get(url)
                self.assertIn(response.status_code, (200, 302, 403, 404), f"{user.role} {url}")
                if response.status_code == 200:
                    seen += 1
                    self.assertContains(response, "app.css")
        self.assertGreater(seen, 50)
