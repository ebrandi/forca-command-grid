from django.contrib import admin

from .models import Task, TaskEvent


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "type", "status", "priority", "assignee", "due_at")
    list_filter = ("type", "status")
    search_fields = ("title", "description")


@admin.register(TaskEvent)
class TaskEventAdmin(admin.ModelAdmin):
    list_display = ("task", "actor", "from_status", "to_status", "at")
