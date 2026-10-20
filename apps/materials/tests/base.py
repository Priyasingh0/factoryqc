from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.checklists.tests.helpers import MediaTestCase

from .helpers import LATER, bad_readings, good_readings, make_grade, make_supplier, pdf
from apps.materials import services
from django.core.management import call_command
from io import StringIO


class MaterialsTestCase(MediaTestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles", stdout=StringIO())
        cls.dock = make_user("dock", Role.DOCK_CLERK)
        cls.dock2 = make_user("dock2", Role.DOCK_CLERK)
        cls.eng = make_user("eng", Role.QUALITY_ENGINEER)
        cls.qm = make_user("qm", Role.QUALITY_MANAGER)
        cls.qh = make_user("qh", Role.QUALITY_HEAD)
        cls.buyer = make_user("buyer", Role.PURCHASING)
        cls.lead = make_user("lead", Role.PRODUCTION_LEAD)
        cls.pm = make_user("pm", Role.PLANT_MANAGER)
        cls.sup = make_user("sup", Role.SHIFT_SUPERVISOR)
        cls.operator = make_user("op", Role.MACHINE_OPERATOR)
        cls.supplier = make_supplier()
        cls.grade = make_grade("EN8")
        cls.lab_grade = make_grade("EN24", lab=True)

    def receive(self, grade=None, heat="H-1001", po="PO-77", qty="500", user=None, **kw):
        grade = grade or self.grade
        args = dict(
            supplier=self.supplier, po_number=po, grade=grade, heat_number=heat, mtc_heat_number=heat,
            quantity=qty, unit="KG", mtc_upload=pdf(), now=LATER,
        )
        args.update(kw)
        return services.create_intake(user or self.dock, **args)

    def cleared(self, **kw):
        intake = self.receive(**kw)
        return services.review_mtc(intake, self.eng, good_readings(intake.grade), LATER)

    def in_lab(self, **kw):
        kw.setdefault("grade", self.lab_grade)
        return self.cleared(**kw)

    def quarantined(self, **kw):
        intake = self.receive(**kw)
        services.reject_intake(intake, self.dock, "DIMENSION", "Bars are bent beyond tolerance", LATER)
        intake.refresh_from_db()
        return intake
