from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import Role
from apps.core.models import AuditLog, Notification
from apps.inspections import services
from apps.inspections.exceptions import (
    InspectionFrozen, InspectionLocked, InvalidMeasurement, InvalidPhoto, NotAuthorized, SubmitBlocked,
)
from apps.inspections.models import DefectPhoto, InspectionStatus
from apps.plant.models import Gauge

from .helpers import (
    NOW, InspectionTestCase, begin_pdi, enter, field_named, lot_ready_for_pdi, tiny_png,
)


def upload(data=None, name="defect.png"):
    return SimpleUploadedFile(name, data if data is not None else tiny_png(), content_type="image/png")


class PdiCase(InspectionTestCase):
    """A PDI in progress: Shaft diameter, Bore (CTQ), Roughness Ra (max), Coating (visual)."""

    def setUp(self):
        self.traveler, self.station = lot_ready_for_pdi(self)
        self.insp = begin_pdi(self, self.traveler, self.station)

    def good_rest(self, skip=()):
        readings = {
            "Shaft diameter": ("25.0000", "MIC-1"), "Bore": ("10.0000", "VC-104"),
            "Roughness Ra": ("0.4000", "RT-9"), "Coating applied": (True, ""),
        }
        for label, (value, gauge) in readings.items():
            if label not in skip:
                enter(self, self.insp, label, value, gauge=gauge)

    def photo(self, measurement, n=1, user=None):
        for i in range(n):
            services.add_defect_photo(measurement, upload(tiny_png(bytes([i + 1]) * 4)), user or self.qa, NOW)


class DefectPhotoTests(PdiCase):
    """PRD 5.4: an out-of-tolerance reading needs at least two photos."""

    def fail_roughness(self):
        return enter(self, self.insp, "Roughness Ra", "0.9000", gauge="RT-9")

    def test_out_of_tolerance_non_ctq_does_not_freeze(self):
        m = self.fail_roughness()
        self.assertFalse(m.is_within_tolerance)
        self.insp.refresh_from_db()
        self.assertEqual(self.insp.status, InspectionStatus.IN_PROGRESS)

    def test_cannot_submit_without_photos(self):
        self.good_rest(skip=("Roughness Ra",))
        self.fail_roughness()
        with self.assertRaisesMessage(SubmitBlocked, "add 2 more defect photo"):
            services.submit_inspection(self.insp, self.qa, now=NOW)

    def test_one_photo_is_not_enough(self):
        self.good_rest(skip=("Roughness Ra",))
        m = self.fail_roughness()
        self.photo(m, 1)
        with self.assertRaisesMessage(SubmitBlocked, "add 1 more defect photo"):
            services.submit_inspection(self.insp, self.qa, now=NOW)

    def test_two_photos_allow_submission(self):
        self.good_rest(skip=("Roughness Ra",))
        m = self.fail_roughness()
        self.photo(m, 2)
        done = services.submit_inspection(self.insp, self.qa, now=NOW)
        self.assertEqual(done.status, InspectionStatus.SUBMITTED)
        self.assertFalse(done.all_within_tolerance)

    def test_same_picture_twice_counts_once(self):
        m = self.fail_roughness()
        services.add_defect_photo(m, upload(), self.qa, NOW)
        with self.assertRaisesMessage(InvalidPhoto, "already uploaded"):
            services.add_defect_photo(m, upload(), self.qa, NOW)
        self.assertEqual(m.photos.count(), 1)

    def test_in_tolerance_reading_takes_no_photo(self):
        m = enter(self, self.insp, "Shaft diameter", "25")
        with self.assertRaisesMessage(InvalidPhoto, "out-of-tolerance"):
            services.add_defect_photo(m, upload(), self.qa, NOW)

    def test_only_real_images_accepted(self):
        m = self.fail_roughness()
        for data in (b"%PDF-1.4 not an image", b"<script>alert(1)</script>", b"GIF89a....", b"\x00" * 50):
            with self.assertRaisesMessage(InvalidPhoto, "JPEG, PNG or WebP"):
                services.add_defect_photo(m, upload(data, "x.png"), self.qa, NOW)

    def test_jpeg_and_webp_signatures_accepted(self):
        m = self.fail_roughness()
        services.add_defect_photo(m, upload(b"\xff\xd8\xff\xe0" + b"1" * 10, "a.jpg"), self.qa, NOW)
        services.add_defect_photo(m, upload(b"RIFF\x00\x00\x00\x00WEBPVP8 ", "b.webp"), self.qa, NOW)
        self.assertEqual(m.photos.count(), 2)

    def test_empty_and_missing_files_refused(self):
        m = self.fail_roughness()
        with self.assertRaises(InvalidPhoto):
            services.add_defect_photo(m, upload(b""), self.qa, NOW)
        with self.assertRaises(InvalidPhoto):
            services.add_defect_photo(m, None, self.qa, NOW)

    def test_oversized_photo_refused(self):
        m = self.fail_roughness()
        with self.assertRaisesMessage(InvalidPhoto, "8 MB"):
            services.add_defect_photo(m, upload(tiny_png() + b"0" * (8 * 1024 * 1024)), self.qa, NOW)

    def test_stored_name_ignores_client_filename(self):
        m = self.fail_roughness()
        photo = services.add_defect_photo(m, upload(name="../../evil.php"), self.qa, NOW)
        self.assertNotIn("evil", photo.image.name)
        self.assertTrue(photo.image.name.endswith(".png"))

    def test_photo_limit_per_reading(self):
        m = self.fail_roughness()
        self.photo(m, services.MAX_DEFECT_PHOTOS)
        with self.assertRaisesMessage(InvalidPhoto, "At most"):
            services.add_defect_photo(m, upload(tiny_png(b"zzzz")), self.qa, NOW)

    def test_only_owner_inspector_uploads(self):
        m = self.fail_roughness()
        with self.assertRaises(NotAuthorized):
            services.add_defect_photo(m, upload(), self.qa2, NOW)
        with self.assertRaises(NotAuthorized):
            services.add_defect_photo(m, upload(), self.operator, NOW)

    def test_photo_upload_is_audited(self):
        m = self.fail_roughness()
        photo = services.add_defect_photo(m, upload(), self.qa, NOW)
        self.assertTrue(AuditLog.objects.filter(
            model_name="inspections.DefectPhoto", object_id=str(photo.pk), user=self.qa).exists())

    def test_no_photos_after_submission(self):
        self.good_rest(skip=("Roughness Ra",))
        m = self.fail_roughness()
        self.photo(m, 2)
        services.submit_inspection(self.insp, self.qa, now=NOW)
        with self.assertRaises(InspectionLocked):
            services.add_defect_photo(m, upload(tiny_png(b"late")), self.qa, NOW)


class CtqFreezeTests(PdiCase):
    """PRD 4.3: a CTQ failure freezes the checklist and alerts the Quality Head."""

    def fail_bore(self):
        return enter(self, self.insp, "Bore", "10.2000", gauge="VC-104")

    def test_ctq_failure_freezes_immediately(self):
        m = self.fail_bore()
        self.assertFalse(m.is_within_tolerance)
        self.insp.refresh_from_db()
        self.assertEqual(self.insp.status, InspectionStatus.FROZEN)
        self.assertIn("Bore", self.insp.frozen_reason)
        self.assertEqual(self.insp.frozen_at, NOW)

    def test_ctq_in_tolerance_does_not_freeze(self):
        enter(self, self.insp, "Bore", "10.1000", gauge="VC-104")  # exactly on the limit
        self.insp.refresh_from_db()
        self.assertEqual(self.insp.status, InspectionStatus.IN_PROGRESS)

    def test_just_over_ctq_limit_freezes(self):
        enter(self, self.insp, "Bore", "10.1001", gauge="VC-104")
        self.insp.refresh_from_db()
        self.assertEqual(self.insp.status, InspectionStatus.FROZEN)

    def test_quality_head_is_alerted_once(self):
        self.fail_bore()
        notes = Notification.objects.filter(recipient=self.qa_head, subject__contains="CTQ failure")
        self.assertEqual(notes.count(), 1)
        self.assertIn("Bore", notes.get().subject)
        self.assertEqual(notes.get().recipient_role, Role.QUALITY_HEAD)

    def test_freeze_is_audited_as_a_decision(self):
        self.fail_bore()
        row = AuditLog.objects.get(action=AuditLog.Action.DECISION, field_name="status")
        self.assertEqual((row.previous_value, row.new_value), ("IN_PROGRESS", "FROZEN"))
        self.assertEqual(row.user, self.qa)

    def test_frozen_checklist_accepts_no_more_readings(self):
        self.fail_bore()
        with self.assertRaises(InspectionFrozen):
            enter(self, self.insp, "Shaft diameter", "25")

    def test_frozen_checklist_cannot_be_submitted(self):
        self.fail_bore()
        with self.assertRaises(InspectionFrozen):
            services.submit_inspection(self.insp, self.qa, now=NOW)

    def test_photos_still_allowed_on_a_frozen_checklist(self):
        m = self.fail_bore()
        self.photo(m, 2)
        self.assertEqual(m.photos.count(), 2)

    def test_frozen_inspection_cannot_be_changed_by_save(self):
        self.fail_bore()
        self.insp.refresh_from_db()
        self.insp.status = InspectionStatus.IN_PROGRESS
        with self.assertRaises(InspectionLocked):
            self.insp.save()

    def test_new_pdi_cannot_start_while_frozen(self):
        from apps.inspections.exceptions import CannotStart
        self.fail_bore()
        with self.assertRaises(CannotStart):
            begin_pdi(self, self.traveler, self.station, user=self.qa2)

    def test_non_ctq_failure_never_alerts_the_quality_head(self):
        enter(self, self.insp, "Shaft diameter", "26", gauge="MIC-1")
        self.assertFalse(Notification.objects.filter(subject__contains="CTQ failure").exists())
