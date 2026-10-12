from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_request, name="login"),
    path("login/sent/", views.login_sent, name="login_sent"),
    path("login/link/<str:token>/", views.magic_link, name="magic_link"),
    path("login/password/", views.password_login, name="password_login"),
    path("login/verify/", views.otp_verify, name="otp_verify"),
    path("logout/", views.logout_view, name="logout"),
    path("account/", views.account, name="account"),
    path("account/password/", views.password_change, name="password_change"),
    path("account/two-step/", views.totp_setup, name="totp_setup"),
    path("account/two-step/disable/", views.totp_disable, name="totp_disable"),
    path("board/members/", views.board_list, name="board_list"),
    path("board/members/add/", views.board_add, name="board_add"),
    path("board/members/<int:pk>/end/", views.board_end, name="board_end"),
]
