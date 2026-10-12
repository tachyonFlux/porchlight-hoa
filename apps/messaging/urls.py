from django.urls import path

from . import views

app_name = "messaging"

urlpatterns = [
    path("announcements/", views.announcement_list, name="announcements"),
    path("announcements/<int:pk>/", views.announcement_detail, name="announcement_detail"),
    path("board/announcements/new/", views.announcement_create, name="announcement_create"),
    path("board/announcements/<int:pk>/edit/", views.announcement_edit, name="announcement_edit"),
    path("notices/", views.notice_list, name="notices"),
    path("notices/<int:pk>/", views.notice_detail, name="notice_detail"),
    path("notices/<int:pk>/reply/", views.notice_reply, name="notice_reply"),
    path("board/notices/new/", views.notice_create, name="notice_create"),
    path("board/notices/<int:pk>/resolve/", views.notice_resolve, name="notice_resolve"),
    path("board/room/", views.board_room, name="board_room"),
    path("board/room/new/", views.thread_create, name="thread_create"),
    path("board/room/<int:pk>/", views.thread_detail, name="thread_detail"),
]
