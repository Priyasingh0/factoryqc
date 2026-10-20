from django.conf import settings
from django.db import models

from .exceptions import ImmutableRecordError


class TimeStampedModel(models.Model):
    """Abstract base: created/updated timestamps for later apps to reuse."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditLogQuerySet(models.QuerySet):
    """Blocks every bulk way of changing or removing audit rows."""

    def update(self, **kwargs):
        raise ImmutableRecordError("Audit log entries cannot be updated.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ImmutableRecordError("Audit log entries cannot be updated.")

    def delete(self):
        raise ImmutableRecordError("Audit log entries cannot be deleted.")


class AuditLog(models.Model):
    """Append-only activity log (PRD 13.1 permanent log, 13.3 tamper-proof audit).

    Protection layers: save() allows inserts only, delete() is blocked, the
    queryset blocks update/delete/bulk_update, and the change/delete
    permissions are never created.
    """

    class Action(models.TextChoices):
        CREATE = "CREATE", "Create"
        UPDATE = "UPDATE", "Update"
        DELETE = "DELETE", "Delete"
        LOGIN = "LOGIN", "Login"
        LOGOUT = "LOGOUT", "Logout"
        LOGIN_FAILED = "LOGIN_FAILED", "Login failed"
        SIGN_OFF = "SIGN_OFF", "Sign-off"
        DECISION = "DECISION", "Decision"
        OTHER = "OTHER", "Other"

    # PROTECT: a user with audit history can be deactivated, never deleted.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="audit_entries",
    )
    # Snapshot so the entry stays readable even for failed logins (no user row).
    user_label = models.CharField(max_length=150, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_serial = models.CharField(max_length=64, blank=True)
    action = models.CharField(max_length=20, choices=Action.choices)
    model_name = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    field_name = models.CharField(max_length=100, blank=True)
    previous_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    remarks = models.TextField(blank=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        ordering = ["-timestamp", "-id"]
        indexes = [models.Index(fields=["model_name", "object_id"])]
        # No "change" or "delete" permission rows are ever created.
        default_permissions = ("add", "view")

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M:%S} {self.action} {self.model_name}#{self.object_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecordError("Audit log entries cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError("Audit log entries cannot be deleted.")


class Notification(TimeStampedModel):
    """Alert record for email/SMS (PRD 2.2 PM alerts, 4.3 CTQ alerts).

    Only the data model exists in Step 1; the sending service is added with
    the first feature that raises an alert.
    """

    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        SMS = "SMS", "SMS"
        IN_APP = "IN_APP", "In-app"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="notifications",
    )
    recipient_role = models.CharField(max_length=30, blank=True)
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.EMAIL)
    subject = models.CharField(max_length=200)
    body = models.TextField()
    related_model = models.CharField(max_length=100, blank=True)
    related_object_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.channel}] {self.subject}"
