from django.urls import path

from . import views

app_name = "handover"

urlpatterns = [
    path("handover/", views.BoardView.as_view(), name="board"),
    path("handover/machines/<int:pk>/new/", views.NewHandoverView.as_view(), name="new"),
    path("handover/<int:pk>/", views.DetailView.as_view(), name="detail"),
    path("handover/<int:pk>/sign/", views.SignView.as_view(), name="sign"),
    path("handover/<int:pk>/acknowledge/", views.AcknowledgeView.as_view(), name="acknowledge"),
    path("flow/", views.FlowListView.as_view(), name="flow_list"),
    path("flow/monitor/", views.MonitorView.as_view(), name="flow_monitor"),
    path("flow/travelers/<int:pk>/", views.FlowDetailView.as_view(), name="flow_detail"),
]
