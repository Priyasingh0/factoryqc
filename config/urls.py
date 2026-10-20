"""URL configuration for the Factory QC project."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.accounts.urls")),
    path("", include("apps.core.urls")),
    path("", include("apps.plant.urls")),
    path("", include("apps.checklists.urls")),
    path("", include("apps.production.urls")),
    path("", include("apps.inspections.urls")),
    path("", include("apps.review.urls")),
    path("", include("apps.dispatch.urls")),
    path("", include("apps.materials.urls")),
    path("", include("apps.handover.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
