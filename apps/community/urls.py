from django.urls import path

from . import views

app_name = "community"

urlpatterns = [
    path("homes/", views.directory, name="directory"),
    path("homes/<int:pk>/", views.home_detail, name="home_detail"),
    path("homes/<int:home_pk>/details/add/", views.detail_add, name="detail_add"),
    path("details/<int:pk>/edit/", views.detail_edit, name="detail_edit"),
    path("claim/", views.claim_start, name="claim_start"),
    path("claim/sent/", views.claim_sent, name="claim_sent"),
    path("claim/confirm/<str:token>/", views.claim_confirm, name="claim_confirm"),
    path("claim/<str:code>/", views.claim_code, name="claim_code"),
    path("board/homes/", views.board_homes, name="board_homes"),
    path("board/homes/new/", views.home_create, name="home_create"),
    path("board/homes/<int:pk>/edit/", views.home_edit, name="home_edit"),
    path("board/homes/<int:pk>/residents/add/", views.resident_add, name="resident_add"),
    path("board/homes/<int:pk>/invite/regenerate/", views.invite_regenerate, name="invite_regenerate"),
    path("board/residencies/<int:pk>/end/", views.residency_end, name="residency_end"),
    path("board/residencies/<int:pk>/primary/", views.residency_primary, name="residency_primary"),
    path("board/invites/print/", views.invite_letters, name="invite_letters"),
    path("board/import/", views.resident_import, name="import"),
    path("board/people/", views.people, name="people"),
]
