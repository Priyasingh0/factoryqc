from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.helpers import make_user
from apps.accounts.models import Role
from apps.core.models import AuditLog
from apps.production import services
from apps.production.admin import WorkOrderAdmin, RoutingOperationAdmin
from apps.production.models import JobTraveler, PartSerial, ProductionRun, RoutingOperation, TravelerOperation, WorkOrder
from django.contrib import admin as dj_admin

from .helpers import ProductionTestCase, make_ready_part, make_work_order


class AdminTests(ProductionTestCase):
    def setUp(self):
        self.superuser = make_user("root", Role.PLANT_MANAGER, is_staff=True, is_superuser=True)
        self.client.force_login(self.superuser)
        self.part = make_ready_part(self.engineer, self.manager)

    def test_created_by_is_set_from_admin(self):
        r = self.client.post(reverse("admin:production_workorder_add"), {
            "number": "WO-ADM", "part": self.part.pk, "quantity": 4, "customer_po": "", "heat_number": "H1",
        })
        self.assertEqual(r.status_code, 302)
        wo = WorkOrder.objects.get(number="WO-ADM")
        self.assertEqual(wo.created_by, self.superuser)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.CREATE).exists())

    def test_released_work_order_cannot_be_changed_or_deleted_in_admin(self):
        wo = make_work_order(self.lead, self.part)
        services.release_work_order(wo, self.lead)
        wo.refresh_from_db()
        ma = dj_admin.site._registry[WorkOrder]
        request = type("R", (), {"user": self.superuser})()
        self.assertFalse(ma.has_change_permission(request, wo))
        self.assertFalse(ma.has_delete_permission(request, wo))
        self.assertEqual(self.client.post(reverse("admin:production_workorder_delete", args=[wo.pk]), {"post": "yes"}).status_code, 403)
        self.assertTrue(WorkOrder.objects.filter(pk=wo.pk).exists())

    def test_used_routing_step_locked_in_admin(self):
        wo = make_work_order(self.lead, self.part)
        services.release_work_order(wo, self.lead)
        step = RoutingOperation.objects.get(part=self.part, op_number=10)
        ma = dj_admin.site._registry[RoutingOperation]
        request = type("R", (), {"user": self.superuser})()
        self.assertFalse(ma.has_change_permission(request, step))
        self.assertFalse(ma.has_delete_permission(request, step))

    def test_shopfloor_records_are_read_only_in_admin(self):
        for model in (JobTraveler, TravelerOperation, ProductionRun, PartSerial):
            ma = dj_admin.site._registry[model]
            request = type("R", (), {"user": self.superuser})()
            self.assertFalse(ma.has_add_permission(request), model)
            self.assertFalse(ma.has_change_permission(request), model)
            self.assertFalse(ma.has_delete_permission(request), model)
            self.assertEqual(self.client.get(reverse(f"admin:production_{model._meta.model_name}_add")).status_code, 403)
