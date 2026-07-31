"""Receive a container-image vulnerability scan from the host and act on it.

    scripts/scan-running-images.sh ... | manage.py ingest_image_scan --trigger scheduled
    manage.py ingest_image_scan --trigger deploy --exit-zero       # end-of-deploy refresh
    manage.py ingest_image_scan --no-relay < scan.json             # ephemeral DB (CI)
    manage.py ingest_image_scan /tmp/scan.json                     # replay a saved run

This is the app-side end of a one-directional pipe. Scanning the images production is
*actually running* means talking to the Docker daemon, and mounting the daemon socket
into this container would hand every code-execution foothold in the application root on
the host — strictly worse than any advisory the scan could report. So the scan runs on
the host (:file:`scripts/scan-running-images.sh`), and hands its result down to here; the
app never reaches up. Everything this command owns is therefore on the receiving side:
validate an untrusted document, store it, raise or retire the director finding, and push
a *change* at a human — the same response loop ``audit_dependencies`` runs for the Python
layer, sharing one implementation so the two cannot drift.

EXIT CODES
----------
``0`` ingested, nothing fixable · ``1`` fixable findings · ``2`` the document was rejected,
or the scan could not read every running image. Matching :file:`scripts/image_scan_report.py`,
where "we could not tell" outranks "we found something", because it is the state that
otherwise gets silently filed as clean.

Only **fixable** findings fail. An advisory upstream has not fixed cannot be actioned by
bumping a base image, so gating on it produces permanent red — and a gate that fails every
day gets switched off, at which point the deployment still *looks* covered and is not. The
findings are still stored and still raised on the director finding; they are simply not
treated as a build failure. Same split the dependency audit makes (``--ignore-unfixed``),
for the same reason, so the two layers behave alike.

``--exit-zero`` is for a caller that already carries its own verdict. Note that
:file:`scripts/scan-running-images.sh` currently invokes this command *without* it and
reads any non-zero exit as "could not deliver the scan result to the app" — so on a
findings run it will report a delivery failure that did not happen. Passing ``--exit-zero``
there is the fix (that file is the host-side lane's to change); the ingest is unaffected
either way, since the document is stored and the finding raised before the exit code is
chosen.
"""
from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from apps.admin_audit.dependency_audit import TRIGGER_MANUAL
from apps.admin_audit.image_scan import TRIGGERS, read_document

EXIT_FINDINGS = 1
EXIT_INCOMPLETE = 2


class Command(BaseCommand):
    help = "Ingest a container-image OS-package vulnerability scan document (from the host)."
    # Lets the tests (and any programmatic caller) drive the pipe through call_command.
    stealth_options = ("stdin",)

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "path", nargs="?", default="-",
            help="Scan document to read; '-' (the default) reads stdin, which is how the "
                 "host scanner delivers it. A path is for replaying a saved run.",
        )
        parser.add_argument(
            "--trigger", choices=list(TRIGGERS), default=TRIGGER_MANUAL,
            help="Why this scan ran. Recorded on the stored result so ops can tell a "
                 "nightly scan from the one a deploy just produced. Default: manual.",
        )
        parser.add_argument(
            "--exit-zero", action="store_true",
            help="Always exit 0 (report only; don't fail the run on findings).",
        )
        parser.add_argument(
            "--no-relay", action="store_true",
            help="Skip the human relay. For ephemeral databases (CI), where every run "
                 "looks like a brand-new finding and the transition signal is noise.",
        )

    def handle(self, *args, **options) -> None:
        from apps.admin_audit.tasks import ingest_image_scan

        # A read failure is handed down rather than raised: the ingest records the
        # rejected delivery with its reason, so an operator sees "arriving and being
        # refused, because X" instead of a row indistinguishable from "never ran".
        document, read_error = self._read(options)

        summary = ingest_image_scan(
            document, trigger=options["trigger"], relay=not options["no_relay"],
            error=read_error,
        )
        self._report(summary)

        if options["exit_zero"]:
            return
        if summary.get("status") == "error":
            # Rejected document, or a scan that could not read every running image: the
            # control itself needs attention before its output can be trusted again, and
            # that outranks findings because it is the state that otherwise reads as clean.
            sys.exit(EXIT_INCOMPLETE)
        if summary.get("fixable_count"):
            sys.exit(EXIT_FINDINGS)

    def _read(self, options) -> tuple[str, str]:
        path = options["path"]
        if path == "-":
            stdin = options.get("stdin") or sys.stdin
            return read_document(stdin)
        try:
            with open(path, encoding="utf-8") as handle:
                return read_document(handle)
        except OSError as exc:
            return "", f"could not open the scan document {path!r}: {exc}"

    def _report(self, summary: dict) -> None:
        """Print the outcome. The host script prints the findings themselves, so this is
        deliberately about what the *app* did with them — an operator reading the deploy
        log needs to see that the result landed and what it did to the standing finding."""
        status = summary.get("status")
        scope = f"{summary.get('scanned_count', 0)}/{summary.get('image_count', 0)} image(s)"

        if status == "ok":
            self.stdout.write(self.style.SUCCESS(
                f"Image scan clean: {scope} scanned, no OS-package findings."
            ))
        elif status == "vulnerable":
            self.stdout.write(self.style.ERROR(
                f"Image scan: {summary.get('vuln_count', 0)} finding(s), "
                f"{summary.get('fixable_count', 0)} fixable, across {scope}."
            ))
            for v in summary.get("vulns", [])[:20]:
                fix = ", ".join(v["fix_versions"]) or "no fix released yet"
                self.stdout.write(
                    f"  - {v['severity']:8s} {v['id']}  {v['package']} {v['version']} "
                    f"-> {fix}   [{v['image']}]"
                )
            if summary.get("truncated"):
                self.stdout.write("  ... list clipped; the counts above are exact.")
        else:
            self.stdout.write(self.style.ERROR(
                f"Image scan not usable: {summary.get('error') or 'unknown error'}"
            ))

        for u in summary.get("unscannable", []):
            who = ", ".join(u.get("services") or []) or u.get("image", "?")
            self.stdout.write(self.style.WARNING(f"  NOT SCANNED  {who} — {u.get('error', '?')}"))

        # What the run did to the standing director finding. "cleared" is the line that
        # proves a rebuild retired its own alarm instead of leaving it to cry wolf until
        # the next scheduled scan.
        self.stdout.write(
            f"Finding: {summary.get('transition', '?')}"
            + (" (relayed to leadership)" if summary.get("relayed") else "")
        )
