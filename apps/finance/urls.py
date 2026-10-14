from django.urls import path

from . import views

app_name = "finance"

urlpatterns = [
    path("board/finance/", views.overview, name="overview"),
    path("board/finance/banks/new/", views.bank_account_form, name="bank_create"),
    path("board/finance/banks/<int:pk>/edit/", views.bank_account_form, name="bank_edit"),
    path("board/finance/banks/<int:pk>/statements/", views.statements, name="statements"),
    path("board/finance/categories/", views.chart, name="chart"),
    path("board/finance/categories/new/", views.account_form, name="account_create"),
    path("board/finance/categories/<int:pk>/edit/", views.account_form, name="account_edit"),
    path("board/finance/rules/", views.rules, name="rules"),
    path("board/finance/import/", views.import_upload, name="import_upload"),
    path("board/finance/import/columns/", views.import_map, name="import_map"),
    path("board/finance/import/<int:pk>/", views.import_done, name="import_done"),
    path("board/finance/transactions/", views.transactions, name="transactions"),
    path("board/finance/transactions/<int:pk>/category/", views.categorize, name="categorize"),
    path("board/finance/reconcile/", views.reconcile, name="reconcile"),
    path("board/finance/reconcile/<int:pk>/", views.reconcile_action, name="reconcile_action"),
    path("board/finance/ledger/", views.ledger, name="ledger"),
    path("board/finance/ledger.csv", views.ledger_csv, name="ledger_csv"),
    path("board/finance/ledger/print/", views.ledger_print, name="ledger_print"),
    path("board/finance/simplefin/", views.simplefin_settings, name="simplefin"),
]
