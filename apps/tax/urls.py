from django.urls import path

from . import views

app_name = "tax"

urlpatterns = [
    path("board/tax/", views.index, name="index"),
    path("board/tax/rules/", views.rules, name="rules"),
    path("board/tax/rules/new/", views.rule_form, name="rule_create"),
    path("board/tax/rules/<int:pk>/edit/", views.rule_form, name="rule_edit"),
    path("board/tax/<int:year>/", views.worksheet, name="worksheet"),
    path("board/tax/<int:year>/print/", views.worksheet_print, name="worksheet_print"),
]
