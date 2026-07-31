"""Run the dependency-CVE response loop (pip-audit) and report what changed.

    manage.py audit_dependencies                     # CI gate: exits 1 on findings
    manage.py audit_dependencies --ignore-unfixed    # ...only when a fix actually exists
    manage.py audit_dependencies --exit-zero --trigger deploy   # end-of-deploy refresh
    manage.py audit_dependencies --exit-zero --no-relay         # ephemeral DB (CI)

This is the ONE entry point outside Celery — the same
``admin_audit.audit_dependencies`` task body the daily beat runs, so a CI gate, a
manual check and the end-of-deploy refresh all produce identical state (stored scan
result, director Recommendation, human relay). There is deliberately no second
"deploy-only" command: a second way to do it is a second thing to drift.

**End-of-deploy invocation** (run inside the freshly-deployed web container, after the
new image is up)::

    manage.py audit_dependencies --exit-zero --trigger deploy

``--exit-zero`` matters there: the deploy has already happened, so failing the deploy
script on an advisory disclosed since the image was built helps nobody — the point of
the post-deploy run is to make the surface tell the truth (a release that fixes a CVE
closes its own finding in minutes instead of leaving a stale alarm standing).

**CI invocation.** ``--ignore-unfixed`` scopes the *exit code* to findings we can
actually action by bumping a version. An advisory with no fix released yet cannot be
fixed by us; letting it hard-fail every build until upstream ships gets the whole gate
switched off, and a permanently-red gate protects nothing. Unfixable advisories are
still scanned, still stored, and still raised on the director finding — they are just
not treated as a build failure.
"""
from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from apps.admin_audit.dependency_audit import (
    TRIGGER_DEPLOY,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULED,
)


class Command(BaseCommand):
    help = "Audit installed dependencies for known vulnerabilities (pip-audit)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--exit-zero", action="store_true",
            help="Always exit 0 (report only; don't fail the run on findings).",
        )
        parser.add_argument(
            "--ignore-unfixed", action="store_true",
            help="Only fail on vulnerabilities that have a fix version available "
                 "(an unfixable upstream advisory must not permanently red the gate).",
        )
        parser.add_argument(
            "--trigger", choices=[TRIGGER_SCHEDULED, TRIGGER_DEPLOY, TRIGGER_MANUAL],
            default=TRIGGER_MANUAL,
            help="Recorded on the stored result so ops can tell a nightly scan from the "
                 "one a deploy just produced. Default: manual.",
        )
        parser.add_argument(
            "--no-relay", action="store_true",
            help="Skip the human relay. For ephemeral databases (CI), where every run "
                 "looks like a brand-new finding and the transition signal is noise.",
        )

    def handle(self, *args, **options) -> None:
        from apps.admin_audit.tasks import audit_dependencies

        summary = audit_dependencies(
            trigger=options["trigger"], relay=not options["no_relay"],
        )
        status = summary.get("status")
        vulns = summary.get("vulns", [])

        if status == "ok":
            self.stdout.write(self.style.SUCCESS(
                f"No known vulnerabilities in {summary.get('package_count', '?')} packages."
            ))
        elif status == "vulnerable":
            self.stdout.write(self.style.ERROR(
                f"{summary.get('vuln_count', 0)} vulnerability(ies) found:"
            ))
            for v in vulns:
                fix = ", ".join(v["fix_versions"]) or "no fix available yet"
                self.stdout.write(f"  - {v['name']} {v['version']}: {v['id']} (fix: {fix})")
        else:
            self.stdout.write(self.style.ERROR(
                f"Dependency audit could not complete: {summary.get('error', 'unknown error')}"
            ))

        # What the run did to the standing director finding — the line a deploy log
        # needs, because "cleared" is the evidence that the fix actually retired its own
        # alarm rather than leaving it to cry wolf until the next scheduled scan.
        self.stdout.write(
            f"Finding: {summary.get('transition', '?')}"
            + (" (relayed to leadership)" if summary.get("relayed") else "")
        )

        if options["exit_zero"]:
            return

        # CI gate. A scan that could not run still fails: that is a transient tooling or
        # network problem worth seeing, not an unactionable upstream advisory.
        if status == "vulnerable" and options["ignore_unfixed"]:
            fixable = [v for v in vulns if v["fix_versions"]]
            if not fixable:
                self.stdout.write(self.style.WARNING(
                    "No fix is available for any finding yet — not failing the gate "
                    "(the director finding stays open and tracks them)."
                ))
                return
            self.stdout.write(self.style.ERROR(
                f"{len(fixable)} of them are fixable now — failing the gate."
            ))
        if status != "ok":
            sys.exit(1)
