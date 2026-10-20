from django.urls import path

from . import views

app_name = "checklists"

urlpatterns = [
    path("checklists/", views.TemplateListView.as_view(), name="list"),
    path("checklists/<int:pk>/", views.TemplateDetailView.as_view(), name="detail"),
    path("checklists/<int:pk>/drawing/", views.DrawingView.as_view(), name="drawing"),
    path("checklists/<int:pk>/submit/", views.SubmitView.as_view(), name="submit"),
    path("checklists/<int:pk>/approve/", views.ApproveView.as_view(), name="approve"),
    path("checklists/<int:pk>/reject/", views.RejectView.as_view(), name="reject"),
    path("checklists/<int:pk>/revise/", views.RevisionView.as_view(), name="revise"),
]
