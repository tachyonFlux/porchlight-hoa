from django.contrib import admin

from .models import TaxYearConfig, Worksheet

admin.site.register(TaxYearConfig)
admin.site.register(Worksheet)
