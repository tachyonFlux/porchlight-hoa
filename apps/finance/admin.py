from django.contrib import admin

from .models import Account, BankAccount, CategoryRule, Transaction


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "type", "tax_class", "is_active"]


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ["name", "last4", "is_active"]


@admin.register(CategoryRule)
class CategoryRuleAdmin(admin.ModelAdmin):
    list_display = ["priority", "match", "pattern", "account"]


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ["date", "description", "amount_cents", "account"]
    search_fields = ["description"]
