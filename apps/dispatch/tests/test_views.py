from django.urls import reverse

from apps.accounts.tests.helpers import TEST_PASSWORD
from apps.core.models import AuditLog
from apps.dispatch import services
from apps.dispatch.models import GatePass, ShipmentManifest, ShippingDocument

from .helpers import DispatchTestCase, approved_lot, conceded_lot, make_red, quarantined_lot
from .test_documents import KIND


class ViewBase(DispatchTestCase):
    def login(self, user):
        self.client.login(username=user.username, password=TEST_PASSWORD)

    def post_manifest(self, lot, **data):
        data.setdefault("vehicle_number", "MH-12-AB-1234")
        return self.client.post(reverse("dispatch:manifest_new", args=[lot.pk]), data)


class AccessTests(ViewBase):
    def test_anonymous_is_sent_to_login_but_the_certificate_page_is_public(self):
        lot = approved_lot(self)
        for url in (reverse("dispatch:board"), reverse("dispatch:check", args=[lot.pk])):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302)
            self.assertIn("login", r["Location"])

    def test_roles_without_dispatch_access_get_403(self):
        lot = approved_lot(self)
        for user in (self.qa, self.operator, self.dock, self.engineer):
            self.login(user)
            self.assertEqual(self.client.get(reverse("dispatch:board")).status_code, 403, user.role)
            self.assertEqual(self.client.get(reverse("dispatch:check", args=[lot.pk])).status_code, 403)

    def test_viewers_cannot_post_anything(self):
        lot = approved_lot(self)
        self.login(self.clerk)
        self.post_manifest(lot)
        manifest = ShipmentManifest.objects.get()
        self.login(self.supervisor)
        self.assertEqual(self.client.get(reverse("dispatch:check", args=[lot.pk])).status_code, 200)
        self.assertEqual(self.post_manifest(lot).status_code, 403)
        for what in ("gate-pass", "packing-slip", "invoice", "label"):
            r = self.client.post(reverse("dispatch:issue", args=[manifest.pk, what]))
            self.assertEqual(r.status_code, 403, what)
        self.assertEqual(ShipmentManifest.objects.count(), 1)

    def test_nav_link_follows_permission(self):
        self.login(self.clerk)
        self.assertContains(self.client.get(reverse("accounts:home")), 'href="/dispatch/"')
        self.login(self.qa)
        self.assertNotContains(self.client.get(reverse("accounts:home")), 'href="/dispatch/"')

    def test_issue_endpoints_reject_get(self):
        lot = approved_lot(self)
        self.login(self.clerk)
        self.post_manifest(lot)
        m = ShipmentManifest.objects.get()
        self.assertEqual(self.client.get(reverse("dispatch:issue", args=[m.pk, "gate-pass"])).status_code, 405)

    def test_unknown_document_slug_is_404(self):
        lot = approved_lot(self)
        self.login(self.clerk)
        self.post_manifest(lot)
        m = ShipmentManifest.objects.get()
        self.assertEqual(self.client.post(reverse("dispatch:issue", args=[m.pk, "receipt"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("dispatch:print", args=[m.pk, "receipt"])).status_code, 404)


class BoardAndCheckTests(ViewBase):
    def test_board_shows_green_and_red_lots(self):
        lot = approved_lot(self)
        self.login(self.supervisor)
        page = self.client.get(reverse("dispatch:board"))
        self.assertContains(page, "Cleared")
        make_red(lot, 20)
        page = self.client.get(reverse("dispatch:board"))
        self.assertContains(page, "Locked")
        self.assertContains(page, "Op 20 Heat treatment is pending")

    def test_check_page_green_shows_the_form_to_clerks_only(self):
        lot = approved_lot(self)
        self.login(self.clerk)
        page = self.client.get(reverse("dispatch:check", args=[lot.pk]))
        self.assertContains(page, "Shipping interlock GREEN")
        self.assertContains(page, "Create manifest")
        self.login(self.supervisor)
        page = self.client.get(reverse("dispatch:check", args=[lot.pk]))
        self.assertContains(page, "Shipping interlock GREEN")
        self.assertNotContains(page, "Create manifest")

    def test_check_page_red_names_the_exact_requirement_and_hides_the_form(self):
        lot = quarantined_lot(self)
        self.login(self.clerk)
        page = self.client.get(reverse("dispatch:check", args=[lot.pk]))
        self.assertContains(page, "SHIPPING INTERLOCK")
        self.assertContains(page, "Digital Rejection Holding Area")
        self.assertContains(page, "rejected and the lot quarantined")
        self.assertNotContains(page, "Create manifest")

    def test_concession_banner(self):
        lot = conceded_lot(self)
        self.login(self.clerk)
        self.assertContains(self.client.get(reverse("dispatch:check", args=[lot.pk])), "Released on concession")

    def test_traveler_page_links_to_the_check_when_completed(self):
        lot = approved_lot(self)
        self.login(self.supervisor)
        page = self.client.get(reverse("production:traveler_detail", args=[lot.pk]))
        self.assertContains(page, reverse("dispatch:check", args=[lot.pk]))
        self.login(self.lead)
        self.assertContains(self.client.get(reverse("production:traveler_detail", args=[lot.pk])),
                            reverse("dispatch:check", args=[lot.pk]))
        self.login(self.operator)
        self.assertNotContains(self.client.get(reverse("production:traveler_detail", args=[lot.pk])),
                               "Dispatch clearance")


class FlowTests(ViewBase):
    def test_full_green_flow_through_the_views(self):
        lot = approved_lot(self)
        self.login(self.clerk)
        r = self.post_manifest(lot)
        manifest = ShipmentManifest.objects.get()
        self.assertRedirects(r, reverse("dispatch:manifest", args=[manifest.pk]))

        r = self.client.post(reverse("dispatch:issue", args=[manifest.pk, "gate-pass"]))
        self.assertRedirects(r, reverse("dispatch:print", args=[manifest.pk, "gate-pass"]))
        page = self.client.get(r["Location"])
        self.assertContains(page, f"GP-{manifest.pk:06d}")

        for slug, prefix in (("packing-slip", "PS"), ("invoice", "INV")):
            r = self.client.post(reverse("dispatch:issue", args=[manifest.pk, slug]))
            self.assertContains(self.client.get(r["Location"]), f"{prefix}-{manifest.pk:06d}")

        r = self.client.post(reverse("dispatch:issue", args=[manifest.pk, "label"]))
        label_page = self.client.get(r["Location"])
        self.assertContains(label_page, "/verify/")
        label = manifest.labels.get()
        self.assertContains(self.client.get(reverse("dispatch:manifest", args=[manifest.pk])), "Crate 1")

        # the customer scans the crate: no login
        self.client.logout()
        cert = self.client.get(reverse("dispatch:verify", args=[label.token]))
        self.assertEqual(cert.status_code, 200)
        self.assertContains(cert, "Certified inspection report")
        self.assertContains(cert, lot.work_order.part.part_number)
        self.assertContains(cert, "Shaft diameter")
        self.assertNotContains(cert, "not currently valid")
        self.assertContains(cert, lot.serials.first().serial_number)

    def test_manifest_form_errors_and_partial_shipment(self):
        lot = approved_lot(self)
        self.login(self.clerk)
        r = self.client.post(reverse("dispatch:manifest_new", args=[lot.pk]), {"vehicle_number": ""}, follow=True)
        self.assertContains(r, "Enter the vehicle number")
        self.assertFalse(ShipmentManifest.objects.exists())
        one = lot.serials.first()
        r = self.post_manifest(lot, serials=[one.pk])
        self.assertEqual(ShipmentManifest.objects.get().batch_size, 1)

    def test_wrong_po_is_shown_as_an_error(self):
        lot = approved_lot(self)
        self.login(self.clerk)
        r = self.post_manifest(lot, customer_po="WRONG")
        r = self.client.get(reverse("dispatch:check", args=[lot.pk]))
        self.assertContains(r, "does not match")
        self.assertFalse(ShipmentManifest.objects.exists())

    def test_public_certificate_rejects_a_forged_token_with_404(self):
        self.assertEqual(self.client.get(reverse("dispatch:verify", args=["forged-token"])).status_code, 404)

    def test_public_certificate_marks_a_withdrawn_lot(self):
        lot = approved_lot(self)
        m = services.create_manifest(lot, self.clerk, vehicle_number="V1")
        label = services.issue_label(m, self.clerk)
        make_red(lot, 20)
        page = self.client.get(reverse("dispatch:verify", args=[label.token]))
        self.assertContains(page, "not currently valid")

    def test_certificate_page_is_not_cached(self):
        lot = approved_lot(self)
        m = services.create_manifest(lot, self.clerk, vehicle_number="V1")
        label = services.issue_label(m, self.clerk)
        self.assertIn("no-cache", self.client.get(reverse("dispatch:verify", args=[label.token]))["Cache-Control"])


class LockoutTests(ViewBase):
    def setUp(self):
        super().setUp()
        self.lot = approved_lot(self)
        services.create_manifest(self.lot, self.clerk, vehicle_number="V1")
        self.manifest = ShipmentManifest.objects.get()
        self.login(self.clerk)

    def test_post_issue_when_red_flashes_the_reason_and_creates_nothing(self):
        make_red(self.lot, 20)
        for what in ("gate-pass", "packing-slip", "invoice", "label"):
            r = self.client.post(reverse("dispatch:issue", args=[self.manifest.pk, what]), follow=True)
            self.assertContains(r, "Shipping interlock")
            self.assertContains(r, "Op 20 Heat treatment is pending")
        self.assertFalse(GatePass.objects.exists())
        self.assertFalse(ShippingDocument.objects.exists())
        self.assertFalse(self.manifest.labels.exists())
        self.assertEqual(AuditLog.objects.filter(field_name="shipping_interlock").count(), 4)

    def test_print_pages_show_the_lockout_when_the_lot_turns_red_after_issue(self):
        for what in ("gate-pass", "packing-slip", "invoice"):
            self.client.post(reverse("dispatch:issue", args=[self.manifest.pk, what]))
        make_red(self.lot, 20)
        for what in ("gate-pass", "packing-slip", "invoice"):
            r = self.client.get(reverse("dispatch:print", args=[self.manifest.pk, what]))
            self.assertEqual(r.status_code, 403, what)
            self.assertContains(r, "locked", status_code=403)
            self.assertContains(r, "Op 20 Heat treatment is pending", status_code=403)
            self.assertNotContains(r, "Issued", status_code=403)
        self.assertEqual(AuditLog.objects.filter(field_name="shipping_interlock").count(), 3)

    def test_label_page_shows_the_lockout(self):
        label = services.issue_label(self.manifest, self.clerk)
        make_red(self.lot, 20)
        r = self.client.get(reverse("dispatch:label", args=[label.pk]))
        self.assertEqual(r.status_code, 403)
        self.assertContains(r, "QR label locked", status_code=403)

    def test_print_before_issue_is_404(self):
        self.assertEqual(self.client.get(reverse("dispatch:print", args=[self.manifest.pk, "invoice"])).status_code, 404)

    def test_manifest_page_warns_when_locked(self):
        make_red(self.lot, 20)
        page = self.client.get(reverse("dispatch:manifest", args=[self.manifest.pk]))
        self.assertContains(page, "documents are locked")
