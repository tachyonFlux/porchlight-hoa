from django.urls import path

from . import views

app_name = "budget"

urlpatterns = [
    path("board/budget/", views.home, name="home"),
    path("board/budget/look-back/", views.lookback, name="lookback"),
    path("board/budget/history/", views.history, name="history"),
    path("board/budget/history/import/", views.history_upload, name="history_upload"),
    path("board/budget/history/import/columns/", views.history_map, name="history_map"),
    path("board/budget/reserves/", views.reserves, name="reserves"),
    path("board/budget/reserves/new/", views.component_form, name="component_create"),
    path("board/budget/reserves/<int:pk>/edit/", views.component_form, name="component_edit"),
    path("board/budget/compare/", views.compare, name="compare"),
    path("board/budget/scenarios/new/", views.scenario_form, name="scenario_create"),
    path("board/budget/scenarios/<int:pk>/", views.scenario_detail, name="scenario"),
    path("board/budget/scenarios/<int:pk>/edit/", views.scenario_form, name="scenario_edit"),
    path("board/budget/scenarios/<int:pk>/lines/", views.scenario_lines, name="scenario_lines"),
    path("board/budget/scenarios/<int:pk>/one-time/", views.one_time_item, name="one_time_item"),
    path("board/budget/scenarios/<int:pk>/delete/", views.scenario_delete, name="scenario_delete"),
    path("board/budget/scenarios/<int:pk>/print/", views.scenario_print, name="scenario_print"),
    path("board/budget/scenarios/<int:pk>/adopt/", views.scenario_adopt, name="scenario_adopt"),
    path("board/budget/scenarios/<int:pk>/publish/", views.scenario_publish, name="scenario_publish"),
    path("budget/", views.published_list, name="published_list"),
    path("budget/<int:year>/", views.published, name="published"),
]
