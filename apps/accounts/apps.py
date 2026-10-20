from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    # Full dotted path because the apps live inside the `apps/` package.
    name = "apps.accounts"
    label = "accounts"
