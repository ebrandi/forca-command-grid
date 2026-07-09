from django.contrib import admin

from .models import Blueprint, BuildJob, CorpIndustryJob, Delivery


@admin.register(BuildJob)
class BuildJobAdmin(admin.ModelAdmin):
    list_display = ("output_type_id", "quantity", "status", "owner", "due_at")
    list_filter = ("status",)


@admin.register(Blueprint)
class BlueprintAdmin(admin.ModelAdmin):
    list_display = ("type_id", "product_type_id", "me", "te", "owner_type", "source")


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = ("job", "quantity", "stockpile", "delivered_by", "created_at")


@admin.register(CorpIndustryJob)
class CorpIndustryJobAdmin(admin.ModelAdmin):
    list_display = ("job_id", "activity_id", "blueprint_type_id", "product_type_id",
                    "runs", "status", "end_date")
    list_filter = ("status", "activity_id")
