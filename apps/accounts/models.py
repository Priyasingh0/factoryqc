from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.core.exceptions import ValidationError
from django.db import models


class Role(models.TextChoices):
    """Plant user roles (PRD section 12, plus roles named in 2.2, 3.1, 4.3, 6.3, 10.2)."""

    PLANT_MANAGER = "PLANT_MANAGER", "Plant Manager"
    QUALITY_ENGINEER = "QUALITY_ENGINEER", "Quality Engineer"
    QUALITY_MANAGER = "QUALITY_MANAGER", "Quality Manager"
    QUALITY_HEAD = "QUALITY_HEAD", "Quality Head"  # 4.3 alerts, 6.3 MRB
    SHIFT_SUPERVISOR = "SHIFT_SUPERVISOR", "Shift Supervisor"
    QA_INSPECTOR = "QA_INSPECTOR", "QA Floor Inspector"
    PRODUCTION_LEAD = "PRODUCTION_LEAD", "Production Lead"  # 6.3 MRB
    MACHINE_OPERATOR = "MACHINE_OPERATOR", "Machine Operator"
    MAINTENANCE_SUPERVISOR = "MAINTENANCE_SUPERVISOR", "Maintenance Supervisor"  # 2.2
    DOCK_CLERK = "DOCK_CLERK", "Dock Clerk"  # 3.1
    WAREHOUSE_CLERK = "WAREHOUSE_CLERK", "Warehouse Clerk"  # 10.2
    PURCHASING = "PURCHASING", "Purchasing Officer"  # 3.3 SNCR alerts, vendor returns


class UserManager(DjangoUserManager):
    """Kept importable at apps.accounts.models.UserManager: migrations reference it."""

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        # A superuser created from the command line needs a role too.
        extra_fields.setdefault("role", Role.PLANT_MANAGER)
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    employee_id = models.CharField(max_length=20, unique=True)
    role = models.CharField(
        max_length=30, choices=Role.choices, default=Role.MACHINE_OPERATOR
    )
    can_grant_concession = models.BooleanField(
        default=False,
        help_text="Shift Supervisors only: authorised to issue concessions (PRD 6.1).",
    )

    objects = UserManager()
    REQUIRED_FIELDS = ["email", "employee_id"]

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    def clean(self):
        super().clean()
        if self.can_grant_concession and self.role != Role.SHIFT_SUPERVISOR:
            raise ValidationError(
                {"can_grant_concession": "Only Shift Supervisors can be authorised."}
            )

    @property
    def may_issue_concession(self):
        """PRD 6.1: Quality Manager, or an authorised Shift Supervisor."""
        if self.role == Role.QUALITY_MANAGER:
            return True
        return self.role == Role.SHIFT_SUPERVISOR and self.can_grant_concession

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Keep the Django Group in step with the role (see services.py).
        # Skip when only unrelated fields are saved (e.g. last_login on login).
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "role" in update_fields:
            from .services import sync_user_group

            sync_user_group(self)
