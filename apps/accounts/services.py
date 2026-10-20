from django.contrib.auth.models import Group, Permission

from .models import Role
from .roles import ROLE_PERMISSIONS


def get_role_group(role):
    group, _ = Group.objects.get_or_create(name=Role(role).label)
    return group


def sync_user_group(user):
    """Make the user a member of exactly one role group (their current role).

    Groups unrelated to roles are left alone.
    """
    role_group_names = [r.label for r in Role]
    target = get_role_group(user.role)
    stale = user.groups.filter(name__in=role_group_names).exclude(pk=target.pk)
    if stale.exists():
        user.groups.remove(*stale)
    user.groups.add(target)


def sync_role_permissions():
    """Create every role group and set its permissions from ROLE_PERMISSIONS.

    Returns the list of permissions that do not exist yet (belonging to apps
    not built yet), so the caller can report them.
    """
    missing = []
    for role, perm_strings in ROLE_PERMISSIONS.items():
        group = get_role_group(role)
        permissions = []
        for perm in perm_strings:
            app_label, codename = perm.split(".")
            try:
                permissions.append(
                    Permission.objects.get(
                        content_type__app_label=app_label, codename=codename
                    )
                )
            except Permission.DoesNotExist:
                missing.append(perm)
        group.permissions.set(permissions)
    return missing
