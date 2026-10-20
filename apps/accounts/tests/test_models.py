from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.accounts.models import Role, User

from .helpers import TEST_PASSWORD, make_user


class UserRoleTests(TestCase):
    def test_new_user_joins_group_matching_role(self):
        user = make_user("insp", Role.QA_INSPECTOR)
        self.assertEqual(
            list(user.groups.values_list("name", flat=True)), [Role.QA_INSPECTOR.label]
        )

    def test_changing_role_moves_user_between_groups(self):
        user = make_user("a", Role.QA_INSPECTOR)
        user.role = Role.SHIFT_SUPERVISOR
        user.save()
        self.assertEqual(
            list(user.groups.values_list("name", flat=True)),
            [Role.SHIFT_SUPERVISOR.label],
        )

    def test_unrelated_groups_are_left_alone(self):
        from django.contrib.auth.models import Group

        user = make_user("a", Role.QA_INSPECTOR)
        extra = Group.objects.create(name="Canteen committee")
        user.groups.add(extra)
        user.role = Role.SHIFT_SUPERVISOR
        user.save()
        names = set(user.groups.values_list("name", flat=True))
        self.assertEqual(names, {Role.SHIFT_SUPERVISOR.label, "Canteen committee"})

    def test_superuser_defaults_to_plant_manager(self):
        boss = User.objects.create_superuser(
            username="boss", email="b@x.com", password=TEST_PASSWORD, employee_id="E-boss"
        )
        self.assertTrue(boss.is_superuser)
        self.assertEqual(boss.role, Role.PLANT_MANAGER)

    def test_concession_flag_only_allowed_for_shift_supervisors(self):
        user = make_user("insp", Role.QA_INSPECTOR, can_grant_concession=True)
        with self.assertRaises(ValidationError):
            user.full_clean()

    def test_may_issue_concession_rule(self):
        qm = make_user("qm", Role.QUALITY_MANAGER)
        sup = make_user("sup", Role.SHIFT_SUPERVISOR)
        auth_sup = make_user("sup2", Role.SHIFT_SUPERVISOR, can_grant_concession=True)
        insp = make_user("insp", Role.QA_INSPECTOR)
        self.assertTrue(qm.may_issue_concession)
        self.assertFalse(sup.may_issue_concession)
        self.assertTrue(auth_sup.may_issue_concession)
        self.assertFalse(insp.may_issue_concession)

    def test_employee_id_must_be_unique(self):
        make_user("one", employee_id="E-100")
        duplicate = User(username="two", employee_id="E-100", role=Role.DOCK_CLERK)
        with self.assertRaises(ValidationError):
            duplicate.full_clean()
