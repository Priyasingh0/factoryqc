from django.urls import path

from . import views

app_name = "review"

urlpatterns = [
    path("review/", views.QueueView.as_view(), name="queue"),
    path("review/inspections/<int:pk>/", views.ReviewDetailView.as_view(), name="detail"),
    path("review/inspections/<int:pk>/approve/", views.ApproveView.as_view(), name="approve"),
    path("review/inspections/<int:pk>/concession/", views.ConcessionView.as_view(), name="concession"),
    path("review/inspections/<int:pk>/quarantine/", views.QuarantineView.as_view(), name="quarantine"),
    path("review/quarantine/", views.QuarantineListView.as_view(), name="quarantine_list"),
    path("review/quarantine/<int:pk>/", views.QuarantineDetailView.as_view(), name="quarantine_detail"),
    path("review/quarantine/<int:pk>/disposition/", views.ProposeView.as_view(), name="propose"),
    path("review/dispositions/<int:pk>/sign/", views.SignView.as_view(), name="sign"),
    path("review/dispositions/<int:pk>/customer-approval/", views.CustomerApprovalView.as_view(), name="customer_approval"),
    path("review/dispositions/<int:pk>/customer-approval/file/", views.CustomerApprovalFileView.as_view(), name="customer_approval_file"),
]
