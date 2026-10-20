from .context import get_request_context
from .models import AuditLog


def _text(value):
    return "" if value is None else str(value)


def log_event(
    *,
    action,
    user=None,
    instance=None,
    model_name="",
    object_id="",
    field_name="",
    previous_value=None,
    new_value=None,
    remarks="",
    user_label="",
):
    """Append one row to the audit log (PRD 13.3).

    The single entry point every app uses to write audit rows. IP address and
    device serial are taken from the current request.
    """
    if instance is not None:
        model_name = model_name or instance._meta.label
        object_id = object_id or _text(instance.pk)

    if user is not None and not user.is_authenticated:
        user = None  # AnonymousUser
    if user is not None and not user_label:
        user_label = f"{getattr(user, 'employee_id', '')} / {user.get_username()}"

    ctx = get_request_context()
    return AuditLog.objects.create(
        user=user,
        user_label=user_label[:150],
        ip_address=ctx.ip_address,
        device_serial=ctx.device_serial,
        action=action,
        model_name=model_name,
        object_id=object_id,
        field_name=field_name,
        previous_value=_text(previous_value),
        new_value=_text(new_value),
        remarks=remarks,
    )
