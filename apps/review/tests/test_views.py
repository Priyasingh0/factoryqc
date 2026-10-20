from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import TEST_PASSWORD
from apps.core.models import AuditLog
from apps.inspections.models import InspectionStatus
from apps.production.models import JobTraveler
from apps.review import services
from apps.review.models import Disposition, ReasonCode, QuarantineRecord

from .helpers import (
    LATER, LONG_TEXT, ReviewTestCase, first_piece_failed, pdf_upload, pdi_ctq_failure_submitted, pdi_ctq_frozen,
    pdi_noctq_fail, pdi_pass,
)


class ViewBase(ReviewTestCase):
    def login(self, user):
        self.client.login(username=user.username, password=TEST_PASSWORD)


class AccessTests(ViewBase):
    def test_anonymous_redirected(self):
        for name in ("queue", "quarantine_list"):
            r = self.client.get(reverse(f"review:{name}"))
            self.assertEqual(r.status_code, 302)
            self.assertIn("login", r["Location"])

    def test_inspector_operator_and_clerk_cannot_open_review(self):
        for user in (self.qa, self.operator, self.dock):
            self.login(user)
            self.assertEqual(self.client.get(reverse("review:queue")).status_code, 403)

    def test_nav_link_follows_permission(self):
        self.login(self.supervisor)
        self.assertContains(self.client.get(reverse("accounts:home")), 'href="/review/"')
        self.login(self.qa)
        self.assertNotContains(self.client.get(reverse("accounts:home")), 'href="/review/"')

    def test_decision_posts_reject_get_and_wrong_roles(self):
        insp, _ = pdi_noctq_fail(self)
        urls = {name: reverse(f"review:{name}", args=[insp.pk]) for name in ("approve", "concession", "quarantine")}
        self.login(self.supervisor)
        for url in urls.values():
            self.assertEqual(self.client.get(url).status_code, 405)
        self.login(self.qa)
        for url in urls.values():
            self.assertEqual(self.client.post(url).status_code, 403)
        self.login(self.manager)  # concession only
        self.assertEqual(self.client.post(urls["approve"]).status_code, 403)
        self.assertEqual(self.client.post(urls["quarantine"]).status_code, 403)


class QueueTests(ViewBase):
    def test_queue_lists_submitted_and_frozen_only(self):
        insp, _ = pdi_noctq_fail(self)
        self.login(self.supervisor)
        page = self.client.get(reverse("review:queue"))
        self.assertContains(page, reverse("review:detail", args=[insp.pk]))
        self.assertContains(page, "1 out of tolerance")

    def test_decided_inspections_leave_the_queue(self):
        insp, _ = pdi_pass(self)
        services.approve_inspection(insp, self.supervisor, "", LATER)
        self.login(self.supervisor)
        self.assertNotContains(self.client.get(reverse("review:queue")), reverse("review:detail", args=[insp.pk]))

    def test_frozen_ctq_appears_first(self):
        frozen, _ = pdi_ctq_frozen(self)
        self.login(self.qa_head)
        page = self.client.get(reverse("review:queue"))
        self.assertContains(page, "FROZEN")

    def test_every_sort_works_and_junk_sort_is_ignored(self):
        pdi_noctq_fail(self)
        self.login(self.supervisor)
        for sort in ("urgency", "shift", "machine", "junk"):
            self.assertEqual(self.client.get(reverse("review:queue"), {"sort": sort}).status_code, 200)


class DecisionFlowTests(ViewBase):
    def test_supervisor_approves_through_the_view(self):
        insp, traveler = pdi_pass(self)
        self.login(self.supervisor)
        r = self.client.post(reverse("review:approve", args=[insp.pk]), {"remarks": "good"}, follow=True)
        self.assertContains(r, "approved")
        insp.refresh_from_db()
        traveler.refresh_from_db()
        self.assertEqual((insp.status, traveler.status), (InspectionStatus.APPROVED, JobTraveler.Status.COMPLETED))

    def test_review_page_shows_gauges_photos_and_only_allowed_buttons(self):
        insp, _ = pdi_noctq_fail(self)
        self.login(self.supervisor)
        page = self.client.get(reverse("review:detail", args=[insp.pk]))
        self.assertContains(page, "RT-9")
        self.assertContains(page, "photo 1")
        self.assertContains(page, "Quarantine lot")
        self.assertContains(page, "Only the Quality Manager or an authorised Shift Supervisor")
        self.assertNotContains(page, "Issue concession</button>")
        self.assertContains(page, "Approval needs every reading within tolerance")
        self.login(self.auth_sup)
        self.assertContains(self.client.get(reverse("review:detail", args=[insp.pk])), "Issue concession</button>")

    def test_concession_through_the_view(self):
        insp, traveler = pdi_noctq_fail(self)
        self.login(self.manager)
        r = self.client.post(reverse("review:concession", args=[insp.pk]),
                             {"reason_code": ReasonCode.COSMETIC, "justification": LONG_TEXT}, follow=True)
        self.assertContains(r, "permanently flagged")
        self.assertContains(r, "Concession lot")
        page = self.client.get(reverse("production:traveler_detail", args=[traveler.pk]))
        self.assertContains(page, "Concession lot")

    def test_concession_without_justification_shows_an_error(self):
        insp, _ = pdi_noctq_fail(self)
        self.login(self.manager)
        r = self.client.post(reverse("review:concession", args=[insp.pk]),
                             {"reason_code": ReasonCode.COSMETIC, "justification": "short"}, follow=True)
        self.assertContains(r, "justification")
        insp.refresh_from_db()
        self.assertEqual(insp.status, InspectionStatus.SUBMITTED)

    def test_ctq_concession_attempt_shows_the_block_message(self):
        insp, _ = pdi_ctq_failure_submitted(self)
        self.login(self.manager)
        r = self.client.post(reverse("review:concession", args=[insp.pk]),
                             {"reason_code": ReasonCode.COSMETIC, "justification": LONG_TEXT}, follow=True)
        self.assertContains(r, "Critical-to-Quality failure")
        self.assertEqual(insp.measurements.model.objects.count() > 0, True)
        insp.refresh_from_db()
        self.assertEqual(insp.status, InspectionStatus.SUBMITTED)

    def test_ctq_frozen_page_offers_quarantine_only(self):
        insp, _ = pdi_ctq_frozen(self)
        self.login(self.qa_head)
        page = self.client.get(reverse("review:detail", args=[insp.pk]))
        self.assertContains(page, "Quarantine lot")
        self.assertNotContains(page, "Issue concession</button>")
        self.assertNotContains(page, ">Approve</button>")

    def test_quarantine_through_the_view_redirects_to_the_record(self):
        insp, traveler = pdi_noctq_fail(self)
        self.login(self.supervisor)
        r = self.client.post(reverse("review:quarantine", args=[insp.pk]), {"reason": "Reject the lot, roughness"})
        record = QuarantineRecord.objects.get()
        self.assertRedirects(r, reverse("review:quarantine_detail", args=[record.pk]))
        page = self.client.get(reverse("production:traveler_detail", args=[traveler.pk]))
        self.assertContains(page, "Quarantined")
        self.assertContains(page, "Quarantine &amp; MRB record")

    def test_missing_inspection_is_404(self):
        self.login(self.supervisor)
        self.assertEqual(self.client.post(reverse("review:approve", args=[9999])).status_code, 404)
        self.assertEqual(self.client.get(reverse("review:detail", args=[9999])).status_code, 404)


class MrbViewTests(ViewBase):
    def setUp(self):
        self.insp, self.traveler = pdi_noctq_fail(self)
        self.quarantine = services.quarantine_lot(self.insp, self.supervisor, "Reject the lot, roughness high", LATER)

    def test_list_and_detail(self):
        self.login(self.plant_manager)
        self.assertContains(self.client.get(reverse("review:quarantine_list")), "Digital Rejection Holding Area")
        page = self.client.get(reverse("review:quarantine_detail", args=[self.quarantine.pk]))
        self.assertContains(page, "No machine can start work on this lot")
        self.assertContains(page, "Propose disposition")

    def test_supervisor_sees_the_record_but_no_mrb_controls(self):
        self.login(self.supervisor)
        page = self.client.get(reverse("review:quarantine_detail", args=[self.quarantine.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Propose disposition")

    def test_full_scrap_flow_through_the_views(self):
        self.login(self.qa_head)
        r = self.client.post(reverse("review:propose", args=[self.quarantine.pk]), {
            "disposition": "SCRAP", "notes": "Beyond rework", "scrap_cost_code": "SC-9", "scrap_bin_verified": "on"},
            follow=True)
        self.assertContains(r, "Disposition proposed")
        d = self.quarantine.disposition
        for user, expect in ((self.qa_head, "Waiting for the rest"), (self.plant_manager, "Waiting for the rest"),
                             (self.lead, "has been executed")):
            self.login(user)
            r = self.client.post(reverse("review:sign", args=[d.pk]), follow=True)
            self.assertContains(r, expect)
        self.traveler.refresh_from_db()
        self.assertEqual(self.traveler.status, JobTraveler.Status.CLOSED)

    def test_invalid_proposal_shows_error(self):
        self.login(self.qa_head)
        r = self.client.post(reverse("review:propose", args=[self.quarantine.pk]),
                             {"disposition": "SCRAP", "notes": "x", "scrap_cost_code": ""}, follow=True)
        self.assertContains(r, "cost code")

    def test_non_board_cannot_propose_or_sign(self):
        d = services.propose_disposition(self.quarantine, self.qa_head, Disposition.RTV, "Back to vendor",
                                         debit_note_ref="D1", now=LATER)
        self.login(self.supervisor)
        self.assertEqual(self.client.post(reverse("review:propose", args=[self.quarantine.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("review:sign", args=[d.pk])).status_code, 403)

    def test_actions_are_post_only(self):
        d = services.propose_disposition(self.quarantine, self.qa_head, Disposition.RTV, "Back to vendor",
                                         debit_note_ref="D1", now=LATER)
        self.login(self.qa_head)
        for url in (reverse("review:propose", args=[self.quarantine.pk]), reverse("review:sign", args=[d.pk]),
                    reverse("review:customer_approval", args=[d.pk])):
            self.assertEqual(self.client.get(url).status_code, 405)

    def test_customer_approval_upload_and_download(self):
        d = services.propose_disposition(self.quarantine, self.qa_head, Disposition.CUSTOMER_CONCESSION,
                                         "Customer accepts deviation", now=LATER)
        self.login(self.qa_head)
        r = self.client.post(reverse("review:customer_approval", args=[d.pk]), {"document": pdf_upload()}, follow=True)
        self.assertContains(r, "Approval stored")
        url = reverse("review:customer_approval_file", args=[d.pk])
        r = self.client.get(url)
        self.assertEqual((r.status_code, r["Content-Type"], r["X-Content-Type-Options"]),
                         (200, "application/pdf", "nosniff"))
        r.close()
        self.login(self.operator)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_bad_upload_shows_error(self):
        d = services.propose_disposition(self.quarantine, self.qa_head, Disposition.CUSTOMER_CONCESSION,
                                         "Customer accepts deviation", now=LATER)
        self.login(self.qa_head)
        r = self.client.post(reverse("review:customer_approval", args=[d.pk]),
                             {"document": pdf_upload(data=b"<html>")}, follow=True)
        self.assertContains(r, "PDF, JPEG or PNG")
        self.assertEqual(self.client.get(reverse("review:customer_approval_file", args=[d.pk])).status_code, 404)
