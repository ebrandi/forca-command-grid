from django.contrib import admin

from .models import MiningPayout, MiningTaxConfig

admin.site.register(MiningTaxConfig)
admin.site.register(MiningPayout)
