from django.urls import path

from . import views

app_name = "inspections"

urlpatterns = [
    path("inspections/", views.InspectionListView.as_view(), name="list"),
    path("inspections/start/", views.StartInspectionView.as_view(), name="start"),
    path("inspections/<int:pk>/", views.InspectionDetailView.as_view(), name="detail"),
    path("inspections/<int:pk>/measure/<int:field_pk>/", views.MeasureView.as_view(), name="measure"),
    path("inspections/<int:pk>/submit/", views.SubmitView.as_view(), name="submit"),
    path("inspections/measurements/<int:pk>/photo/", views.PhotoUploadView.as_view(), name="photo_upload"),
    path("inspections/photos/<int:pk>/", views.PhotoView.as_view(), name="photo"),
]
