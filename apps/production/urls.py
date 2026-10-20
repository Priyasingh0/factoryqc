from django.urls import path

from . import views

app_name = "production"

urlpatterns = [
    path("production/work-orders/", views.WorkOrderListView.as_view(), name="work_order_list"),
    path("production/work-orders/<int:pk>/", views.WorkOrderDetailView.as_view(), name="work_order_detail"),
    path("production/work-orders/<int:pk>/release/", views.ReleaseWorkOrderView.as_view(), name="release"),
    path("production/travelers/", views.TravelerListView.as_view(), name="traveler_list"),
    path("production/travelers/<int:pk>/", views.TravelerDetailView.as_view(), name="traveler_detail"),
    path("production/operations/<int:pk>/start/", views.StartOperationView.as_view(), name="start"),
    path("production/operations/<int:pk>/parts/", views.RecordPartsView.as_view(), name="parts"),
    path("production/operations/<int:pk>/complete/", views.CompleteOperationView.as_view(), name="complete"),
    path("production/operations/<int:pk>/first-piece/", views.ClearFirstPieceView.as_view(), name="first_piece"),
    path("production/operations/<int:pk>/periodic-check/", views.PeriodicCheckView.as_view(), name="periodic_check"),
    path("production/trace/", views.TraceView.as_view(), name="trace"),
]
