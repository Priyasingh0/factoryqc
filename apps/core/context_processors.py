from django.utils.dateparse import parse_datetime

from .navigation import build_nav

SEEN_KEY = "notifications_seen_at"


def shell(request):
    """Navigation and the unseen-notification count for the application shell.

    "Unseen" means created after the user last opened the notifications page; the time is
    kept in the session so no model change is needed.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    from .models import Notification

    qs = Notification.objects.filter(recipient=user)
    seen = parse_datetime(request.session.get(SEEN_KEY, "") or "")
    if seen:
        qs = qs.filter(created_at__gt=seen)
    return {"nav_sections": build_nav(request), "unread_notifications": qs.count()}
