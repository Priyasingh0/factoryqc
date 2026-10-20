from django.urls import path

from . import views

app_name = "materials"

urlpatterns = [
    path("materials/", views.IntakeListView.as_view(), name="list"),
    path("materials/new/", views.IntakeCreateView.as_view(), name="new"),
    path("materials/intakes/<int:pk>/", views.IntakeDetailView.as_view(), name="detail"),
    path("materials/intakes/<int:pk>/mtc/", views.MTCFileView.as_view(), name="mtc_file"),
    path("materials/intakes/<int:pk>/review/", views.ReviewMTCView.as_view(), name="review"),
    path("materials/intakes/<int:pk>/lab/", views.LabResultView.as_view(), name="lab"),
    path("materials/intakes/<int:pk>/reject/", views.RejectView.as_view(), name="reject"),
    path("materials/intakes/<int:pk>/recall/", views.RecallView.as_view(), name="recall"),
    path("materials/intakes/<int:pk>/resolve/", views.ResolveView.as_view(), name="resolve"),
    path("materials/intakes/<int:pk>/label/", views.LabelView.as_view(), name="label"),
    path("materials/sncr/", views.SNCRListView.as_view(), name="sncr_list"),
    path("materials/suppliers/", views.SupplierListView.as_view(), name="suppliers"),
    path("materials/work-orders/<int:pk>/issue/", views.IssueView.as_view(), name="issue"),
]
