"""
Django settings for the Zapuza Factory Operations & Quality Control System.

Secrets and deployment switches come from environment variables, with
development-friendly defaults.
"""

import os
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: set DJANGO_SECRET_KEY in production. QR tokens are signed
# with this key, so changing it invalidates every printed QR label.
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-gg-nx%6jhh^nm$g0sty+p$k!5fil2jwc_9+_2(s!(merqrocpj",
)

# SECURITY WARNING: run with DJANGO_DEBUG=0 in production.
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Project apps (Step 1)
    "apps.accounts.apps.AccountsConfig",
    "apps.core.apps.CoreConfig",
    # Step 2
    "apps.plant.apps.PlantConfig",
    # Step 3
    "apps.checklists.apps.ChecklistsConfig",
    # Step 4
    "apps.production.apps.ProductionConfig",
    # Step 5
    "apps.inspections.apps.InspectionsConfig",
    # Step 6
    "apps.review.apps.ReviewConfig",
    # Step 7
    "apps.dispatch.apps.DispatchConfig",
    # Step 8
    "apps.materials.apps.MaterialsConfig",
    "apps.handover.apps.HandoverConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # PRD 13.3: capture IP address + device serial for every audit entry.
    "apps.core.middleware.AuditContextMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.shell",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}


# Custom user model. Must be set BEFORE the first migrate.
AUTH_USER_MODEL = "accounts.User"


# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "accounts:home"
LOGOUT_REDIRECT_URL = "accounts:login"


# Internationalization

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True


# Static and media files

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"


# PRD 4.3: Quality Head email alerts. Console backend during development.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = "qc-system@factory.local"


# Default primary key field type

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
