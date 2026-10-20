from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm

from .models import User


class FactoryUserCreationForm(AdminUserCreationForm):
    # AdminUserCreationForm (Django 5.1+) is what the admin "add user" page expects.
    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ("username", "employee_id", "role", "can_grant_concession")


class FactoryUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"
