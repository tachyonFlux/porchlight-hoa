from django.urls import path

from . import views

app_name = "meetings"

urlpatterns = [
    path("meetings/", views.meeting_list, name="list"),
    path("meetings/calendar/", views.calendar_subscribe, name="calendar"),
    path("meetings/<int:pk>/", views.meeting_detail, name="meeting_detail"),
    path("meetings/<int:pk>/event.ics", views.meeting_ics, name="meeting_ics"),
    path("meetings/<int:pk>/minutes/", views.minutes_file, name="minutes_file"),
    path("calendar/<str:token>.ics", views.calendar_feed, name="calendar_feed"),
    path("polls/<int:pk>/", views.poll_detail, name="poll_detail"),
    path("board/polls/new/", views.poll_create, name="poll_create"),
    path("board/polls/<int:pk>/close/", views.poll_close, name="poll_close"),
    path("board/polls/<int:pk>/delete/", views.poll_delete, name="poll_delete"),
    path("board/meetings/new/", views.meeting_create, name="meeting_create"),
    path("board/meetings/<int:pk>/edit/", views.meeting_edit, name="meeting_edit"),
    path("board/meetings/<int:pk>/cancel/", views.meeting_cancel, name="meeting_cancel"),
]
