from apps.accounts.models import Role, User

TEST_PASSWORD = "Str0ng-pass-123"


def make_user(username="user1", role=Role.QA_INSPECTOR, employee_id=None, **extra):
    return User.objects.create_user(
        username=username,
        password=TEST_PASSWORD,
        role=role,
        employee_id=employee_id or f"E-{username}",
        **extra,
    )
