from django.urls import path

from . import views

app_name = "dispatch"

urlpatterns = [
    path("dispatch/", views.BoardView.as_view(), name="board"),
    path("dispatch/<int:pk>/check/", views.CheckView.as_view(), name="check"),
    path("dispatch/<int:pk>/manifests/new/", views.ManifestCreateView.as_view(), name="manifest_new"),
    path("dispatch/manifests/<int:pk>/", views.ManifestDetailView.as_view(), name="manifest"),
    path("dispatch/manifests/<int:pk>/issue/<slug:what>/", views.IssueView.as_view(), name="issue"),
    path("dispatch/manifests/<int:pk>/print/<slug:what>/", views.PrintView.as_view(), name="print"),
    path("dispatch/labels/<int:pk>/", views.LabelView.as_view(), name="label"),
    path("verify/<str:token>/", views.verify, name="verify"),
]
