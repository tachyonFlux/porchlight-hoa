from django.urls import path

from . import views

app_name = "exterior"

urlpatterns = [
    path("requests/", views.request_list, name="list"),
    path("requests/new/", views.request_new, name="new"),
    path("requests/<int:pk>/", views.request_detail, name="detail"),
    path("requests/<int:pk>/comment/", views.request_comment, name="comment"),
    path("requests/<int:pk>/status/", views.request_status, name="status"),
    path("requests/<int:pk>/withdraw/", views.request_withdraw, name="withdraw"),
    path("requests/photos/<int:pk>/", views.photo, name="photo"),
]
