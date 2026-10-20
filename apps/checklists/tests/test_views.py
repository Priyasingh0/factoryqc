from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests.helpers import TEST_PASSWORD, make_user
from apps.checklists import services
from apps.checklists.models import ChecklistTemplate, TemplateStatus

from .helpers import PDF_BYTES, RoleTestCase, force_status, make_part, make_pin, make_template


class TemplateViewAccessTests(RoleTestCase):
    def setUp(self):
        super().setUp()
        self.template = make_template(self.engineer)
        self.urls = [
            reverse("checklists:list"),
            reverse("checklists:detail", args=[self.template.pk]),
            reverse("checklists:drawing", args=[self.template.pk]),
        ]

    def login(self, user):
        self.client.login(username=user.username, password=TEST_PASSWORD)

    def test_anonymous_is_redirected_to_login(self):
        for url in self.urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn(reverse("accounts:login"), response["Location"])

    def test_roles_with_view_permission_can_open_pages(self):
        for user in (self.engineer, self.manager, self.inspector):
            self.client.logout()
            self.login(user)
            for url in self.urls:
                self.assertEqual(self.client.get(url).status_code, 200, (user, url))

    def test_dock_clerk_is_forbidden(self):
        clerk = make_user("dock", Role.DOCK_CLERK)
        self.login(clerk)
        for url in self.urls:
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_drawing_is_served_as_pdf(self):
        self.login(self.inspector)
        response = self.client.get(self.urls[2])
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(b"".join(response.streaming_content), PDF_BYTES)

    def test_missing_drawing_is_404(self):
        template = make_template(
            self.engineer, part=make_part("P-NODRAW"), drawing=False, with_field=False
        )
        self.login(self.inspector)
        response = self.client.get(reverse("checklists:drawing", args=[template.pk]))
        self.assertEqual(response.status_code, 404)

    def test_list_can_filter_by_status(self):
        released = make_template(self.engineer, part=self.template.part, revision="B")
        force_status(released, TemplateStatus.RELEASED, approver=self.manager)
        self.login(self.manager)
        response = self.client.get(reverse("checklists:list"), {"status": "RELEASED"})
        self.assertEqual([t.pk for t in response.context["templates_"]], [released.pk])


class DetailPageContentTests(RoleTestCase):
    def setUp(self):
        super().setUp()
        self.template = make_template(self.engineer)
        make_pin(self.template, 1)
        field = self.template.fields.get()
        field.is_ctq = True
        field.save()
        self.url = reverse("checklists:detail", args=[self.template.pk])

    def test_shows_spec_ctq_and_pin(self):
        self.client.login(username="eng", password=TEST_PASSWORD)
        response = self.client.get(self.url)
        self.assertContains(response, "25.0000 (+0.0500 / -0.0200) mm")
        self.assertContains(response, "CTQ")
        self.assertContains(response, "10.50")

    def test_engineer_sees_submit_but_not_approve(self):
        self.client.login(username="eng", password=TEST_PASSWORD)
        response = self.client.get(self.url)
        self.assertContains(response, "Submit for approval")
        self.assertNotContains(response, "Approve &amp; release")

    def test_inspector_sees_no_workflow_buttons(self):
        self.client.login(username="insp", password=TEST_PASSWORD)
        response = self.client.get(self.url)
        self.assertNotContains(response, "Submit for approval")
        self.assertNotContains(response, "Edit in admin")

    def test_manager_sees_approve_only_when_pending(self):
        self.client.login(username="qm", password=TEST_PASSWORD)
        self.assertNotContains(self.client.get(self.url), "Approve &amp; release")
        services.submit_for_approval(self.template, self.engineer)
        self.assertContains(self.client.get(self.url), "Approve &amp; release")


class WorkflowViewTests(RoleTestCase):
    def setUp(self):
        super().setUp()
        self.template = make_template(self.engineer)

    def post(self, name, user, data=None, follow=False):
        self.client.logout()
        self.client.login(username=user.username, password=TEST_PASSWORD)
        return self.client.post(reverse(f"checklists:{name}", args=[self.template.pk]), data or {},
                                follow=follow)

    def status(self):
        return ChecklistTemplate.objects.get(pk=self.template.pk).status

    def test_full_happy_path_through_the_views(self):
        self.post("submit", self.engineer)
        self.assertEqual(self.status(), TemplateStatus.PENDING_APPROVAL)
        self.post("approve", self.manager)
        self.assertEqual(self.status(), TemplateStatus.RELEASED)
        response = self.post("revise", self.engineer)
        new = ChecklistTemplate.objects.get(revision="B")
        self.assertRedirects(
            response, reverse("checklists:detail", args=[new.pk]), fetch_redirect_response=False
        )

    def test_reject_with_reason_returns_to_draft(self):
        self.post("submit", self.engineer)
        self.post("reject", self.manager, {"reason": "Wrong tolerance"})
        template = ChecklistTemplate.objects.get(pk=self.template.pk)
        self.assertEqual(template.status, TemplateStatus.DRAFT)
        self.assertEqual(template.review_notes, "Wrong tolerance")

    def test_reject_without_reason_shows_error_and_changes_nothing(self):
        self.post("submit", self.engineer)
        response = self.post("reject", self.manager, {"reason": ""}, follow=True)
        self.assertContains(response, "Give a reason")
        self.assertEqual(self.status(), TemplateStatus.PENDING_APPROVAL)

    def test_engineer_cannot_approve_via_url(self):
        self.post("submit", self.engineer)
        response = self.post("approve", self.engineer2)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.status(), TemplateStatus.PENDING_APPROVAL)

    def test_inspector_cannot_submit_via_url(self):
        response = self.post("submit", self.inspector)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.status(), TemplateStatus.DRAFT)

    def test_incomplete_template_shows_validation_message(self):
        incomplete = make_template(self.engineer, part=make_part_other(), with_field=False)
        self.client.login(username="eng", password=TEST_PASSWORD)
        response = self.client.post(reverse("checklists:submit", args=[incomplete.pk]), follow=True)
        self.assertContains(response, "at least one inspection field")
        incomplete.refresh_from_db()
        self.assertEqual(incomplete.status, TemplateStatus.DRAFT)

    def test_workflow_urls_reject_get(self):
        # Each button URL needs its own permission, so use a user who holds it.
        users = {"submit": "eng", "revise": "eng", "approve": "qm", "reject": "qm"}
        for name, username in users.items():
            self.client.logout()
            self.client.login(username=username, password=TEST_PASSWORD)
            response = self.client.get(reverse(f"checklists:{name}", args=[self.template.pk]))
            self.assertEqual(response.status_code, 405, name)


def make_part_other():
    return make_part("P-OTHER")
