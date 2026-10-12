from django.urls import path

from . import views

app_name = "compliance"

urlpatterns = [
    path("board/compliance/", views.deadline_list, name="list"),
    path("board/compliance/new/", views.deadline_create, name="create"),
    path("board/compliance/suggested/", views.add_suggestions, name="suggestions"),
    path("board/compliance/<int:pk>/edit/", views.deadline_edit, name="edit"),
    path("board/compliance/<int:pk>/complete/", views.deadline_complete, name="complete"),
    path("board/compliance/<int:pk>/reopen/", views.deadline_reopen, name="reopen"),
]
