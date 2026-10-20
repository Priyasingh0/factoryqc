from django.urls import path

from . import views

app_name = "plant"

urlpatterns = [
    path("plant/machines/", views.MachineListView.as_view(), name="machine_list"),
    path("plant/machines/<int:pk>/", views.MachineDetailView.as_view(), name="machine_detail"),
    path(
        "plant/machines/<int:pk>/pm-check/",
        views.PMCheckCreateView.as_view(),
        name="pm_check_create",
    ),
    path("plant/gauges/", views.GaugeListView.as_view(), name="gauge_list"),
    path("plant/parts/", views.PartListView.as_view(), name="part_list"),
]
