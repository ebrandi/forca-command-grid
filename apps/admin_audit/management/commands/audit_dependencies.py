"""Run a dependency-vulnerability audit (pip-audit) and report findings.

    manage.py audit_dependencies            # exits 1 if vulnerabilities are found
    manage.py audit_dependencies --exit-zero  # report only (never fails)

Usable in CI as a gate (non-zero exit on findings) and shares its logic with the
weekly ``admin_audit.audit_dependencies`` Celery task.
"""
from __future__ import annotations

import sys

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Audit installed dependencies for known vulnerabilities (pip-audit)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--exit-zero", action="store_true",
            help="Always exit 0 (report only; don't fail the run on findings).",
        )

    def handle(self, *args, **options) -> None:
        from apps.admin_audit.dependency_audit import run_dependency_audit

        summary = run_dependency_audit()
        status = summary.get("status")

        if status == "ok":
            self.stdout.write(self.style.SUCCESS(
                f"No known vulnerabilities in {summary.get('package_count', '?')} packages."
            ))
        elif status == "vulnerable":
            self.stdout.write(self.style.ERROR(
                f"{summary.get('vuln_count', 0)} vulnerability(ies) found:"
            ))
            for v in summary.get("vulns", []):
                fix = ", ".join(v["fix_versions"]) or "no fix available yet"
                self.stdout.write(f"  - {v['name']} {v['version']}: {v['id']} (fix: {fix})")
        else:
            self.stdout.write(self.style.ERROR(
                f"Dependency audit could not complete: {summary.get('error', 'unknown error')}"
            ))

        # CI gate: fail on findings or on a tool error, unless explicitly told to report only.
        if not options["exit_zero"] and status != "ok":
            sys.exit(1)
