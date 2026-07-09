from django.contrib import admin

from .models import Candidate, CandidateConsent, CandidateEvidence


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ("name", "character_id", "status", "evidence_refreshed_at")
    list_filter = ("status",)


@admin.register(CandidateEvidence)
class CandidateEvidenceAdmin(admin.ModelAdmin):
    list_display = ("candidate", "theme", "confidence", "source", "is_flag")
    list_filter = ("theme", "confidence", "source", "is_flag")


@admin.register(CandidateConsent)
class CandidateConsentAdmin(admin.ModelAdmin):
    list_display = ("candidate", "expires_at", "granted_at", "revoked_at")
