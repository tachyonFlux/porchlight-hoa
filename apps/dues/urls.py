from django.urls import path

from . import views

app_name = "dues"

urlpatterns = [
    path("dues/", views.my_account, name="my_account"),
    path("dues/invoices/<int:pk>/", views.invoice_detail, name="invoice_detail"),
    path("dues/invoices/<int:pk>/pdf/", views.invoice_pdf, name="invoice_pdf"),
    path("i/<str:token>/", views.invoice_public, name="invoice_public"),
    path("board/dues/", views.overview, name="overview"),
    path("board/dues/report.csv", views.report_csv, name="report_csv"),
    path("board/dues/settings/", views.dues_settings, name="settings"),
    path("board/dues/payments/new/", views.payment_new, name="payment_new"),
    path("board/dues/payments/<int:pk>/void/", views.payment_void, name="payment_void"),
    path("board/dues/homes/<int:pk>/", views.home_ledger, name="home_ledger"),
    path("board/dues/homes/<int:pk>/adjust/", views.adjustment, name="adjustment"),
    path("board/dues/invoices/<int:pk>/waive/", views.waive_late_fee, name="waive_late_fee"),
    path("board/dues/periods/", views.period_list, name="period_list"),
    path("board/dues/periods/<int:pk>/", views.period_detail, name="period_detail"),
    path("board/dues/periods/<int:pk>/print/", views.period_print, name="period_print"),
]
