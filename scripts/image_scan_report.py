#!/usr/bin/env python3
"""Turn raw trivy reports into ONE scan document, and decide what it means.

This is the aggregation half of the container-image vulnerability scan. The shell half
(:file:`scan-running-images.sh`, :file:`audit-image-os.sh`) owns docker and trivy — the
things that must run on the HOST — and writes a small manifest describing what it
scanned. This module owns the judgement: what counts as a finding, what "we could not
tell" looks like, and which exit code the caller should carry.

Splitting it there is deliberate on two counts. Shelling JSON around with jq is where
these scripts historically go wrong, and — more importantly — the judgement is the part
worth testing. ``scripts/tests/test_image_scan_report.py`` exercises every rule below
against captured trivy output; none of it needs docker, trivy, or a network.

THE TWO THINGS THIS MODULE IS OPINIONATED ABOUT
-----------------------------------------------
**1. Fixable and unfixable findings are both reported; only fixable ones gate.**
Trivy has an ``--ignore-unfixed`` flag and we deliberately do NOT pass it. An advisory
with no released fix cannot be actioned by bumping a version, so failing a deploy on it
produces permanent red, and a gate that fails every single day gets switched off — at
which point the repo still *looks* covered and is not. But dropping those findings at
the scanner would also hide them from the humans who might mitigate another way. So the
scan sees everything at the severity floor, the document carries everything, and
``fixable_count`` — not ``vuln_count`` — is what fails a build. Same split the Python
dependency audit makes (``manage.py audit_dependencies --ignore-unfixed``), for the same
reason, so the two layers behave alike.

**2. "Could not scan" is never "clean".**
A target that could not be scanned makes the document *incomplete*, and an incomplete
result is reported as such rather than quietly shrinking the denominator. This is not
hypothetical: a running container's image can vanish from the local store (a rebuild
moves the tag, ``docker image prune`` reaps the untagged original) while the container
happily keeps serving from its already-unpacked rootfs. Scanning "the tag it was built
from" instead would audit a *different* image and report the answer as if it described
production — exactly the intent-vs-reality lie this whole control exists to correct. So
we say we don't know, loudly, and name the container.

DOCUMENT SHAPE (schema 1) — also the contract with the app-side ingest command::

    {
      "schema": 1, "scanner": "trivy", "scanner_version": "0.70.0",
      "source": "running-containers" | "pending-images",
      "trigger": "scheduled" | "deploy" | "manual",
      "host": "forca-prod", "as_of": "2026-07-31T16:00:00Z",
      "severity_floor": "HIGH", "pkg_types": "os",
      "status": "ok" | "vulnerable" | "error",
      "complete": true,
      "image_count": 4, "scanned_count": 4,
      "vuln_count": 7, "fixable_count": 3, "truncated": false,
      "images":      [{image, image_id, services, containers, os, vuln_count, fixable_count}],
      "unscannable": [{image, image_id, services, containers, error}],
      "vulns":       [{id, severity, package, version, fix_versions, image, services, url}]
    }

``status`` describes what we learned about the images we *did* scan: ``error`` means we
learned nothing at all (no target scanned), not "some target failed" — that is what
``complete`` is for. Keeping those separate matters, because a consumer that treats a
partial scan as an error would throw away real findings, and one that treats it as a
success would report a shrunken denominator as the whole truth.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCHEMA_VERSION = 1

# Cap the vulnerability list so one pathologically stale base image cannot produce a
# megabyte of JSON, blow up the stored row, and turn a notification into a wall of text
# nobody reads. The counts stay exact; only the itemised list is clipped, and
# ``truncated`` says so. Mirrors the same cap in apps/admin_audit/dependency_audit.py.
MAX_REPORTED = 200

# Highest first, so the worst thing is the first thing a human sees.
_SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}

STATUS_OK = "ok"
STATUS_VULNERABLE = "vulnerable"
STATUS_ERROR = "error"

# Exit codes, in order of precedence (see `verdict`). Kept distinct because systemd and
# a deploy script want to react differently: "we found something" is a queue of work,
# "we could not tell" is a broken control, and only the second one means the scan itself
# needs fixing before its output can be trusted again.
EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_INCOMPLETE = 2


def extract_vulns(raw: dict) -> list[dict]:
    """Flatten one trivy report into our vulnerability records.

    Trivy groups findings by "result" (one per package ecosystem it recognised in the
    image). We keep the package coordinates and the advisory id and throw away the rest
    — trivy's ``Description`` field alone can run to several kilobytes of prose per
    advisory, which is useful in a terminal and actively harmful in a stored row or a
    chat message.

    Duplicates are collapsed on ``(id, package, version)``: the same advisory legitimately
    appears once per result when an image ships a package under two ecosystems, and
    counting it twice would make a finding look like it doubled when nothing changed.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for result in raw.get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            vid = str(v.get("VulnerabilityID") or "")
            pkg = str(v.get("PkgName") or "")
            ver = str(v.get("InstalledVersion") or "")
            if not vid:
                continue
            key = (vid, pkg, ver)
            if key in seen:
                continue
            seen.add(key)
            fixed = str(v.get("FixedVersion") or "").strip()
            out.append({
                "id": vid,
                "severity": str(v.get("Severity") or "UNKNOWN").upper(),
                "package": pkg,
                "version": ver,
                # A list, not a string: trivy comma-separates candidate fixes, and the
                # dependency audit's records use a list, so the two layers render alike.
                "fix_versions": [p.strip() for p in fixed.split(",") if p.strip()],
                "url": str(v.get("PrimaryURL") or ""),
            })
    return out


def _os_label(raw: dict) -> str:
    """"debian 13.5" — the base an operator has to bump to fix anything found here."""
    os_meta = (raw.get("Metadata") or {}).get("OS") or {}
    family = str(os_meta.get("Family") or "").strip()
    name = str(os_meta.get("Name") or "").strip()
    return " ".join(part for part in (family, name) if part)


def _sort_key(v: dict) -> tuple:
    """CRITICAL before HIGH, fixable before unfixable, then stable by image/id.

    Fixable-first is the load-bearing part: the list is clipped at ``MAX_REPORTED`` and
    truncating away the findings someone could actually action today would be the worst
    possible thing to drop.
    """
    return (
        _SEVERITY_RANK.get(v["severity"], 9),
        0 if v["fix_versions"] else 1,
        v.get("image", ""),
        v["id"],
        v["package"],
    )


def build_document(manifest: dict, reports: dict[str, dict | None]) -> dict:
    """Assemble the scan document from a manifest plus the trivy report per target.

    ``manifest["targets"]`` is what the shell half decided to scan — already deduplicated
    by image, because web/worker/beat share one image and scanning it three times would
    treble the runtime and treble every finding. ``reports`` maps a target's ``key`` to
    its parsed trivy report, or to ``None`` when the scan failed; a target may also carry
    its own ``error`` string (set when the shell half could not even attempt a scan, e.g.
    the running image is no longer in the local store).
    """
    targets = manifest.get("targets") or []
    images: list[dict] = []
    unscannable: list[dict] = []
    vulns: list[dict] = []

    for target in targets:
        ident = {
            "image": target.get("image", ""),
            "image_id": target.get("image_id", ""),
            "services": list(target.get("services") or []),
            "containers": list(target.get("containers") or []),
        }
        raw = reports.get(target.get("key", ""))
        error = target.get("error")
        if error or raw is None:
            unscannable.append({**ident, "error": str(error or "trivy did not produce a report")})
            continue

        found = extract_vulns(raw)
        for v in found:
            vulns.append({**v, "image": ident["image"], "services": ident["services"]})
        images.append({
            **ident,
            "os": _os_label(raw),
            "vuln_count": len(found),
            "fixable_count": sum(1 for v in found if v["fix_versions"]),
        })

    vulns.sort(key=_sort_key)
    fixable = sum(1 for v in vulns if v["fix_versions"])

    if not images:
        # Nothing was scanned at all: we learned nothing about the deployment. Never
        # "ok" — a consumer must be able to tell this apart from a clean result, or a
        # broken scanner silently reads as an all-clear for as long as it stays broken.
        status = STATUS_ERROR
    else:
        status = STATUS_VULNERABLE if vulns else STATUS_OK

    return {
        "schema": SCHEMA_VERSION,
        "scanner": manifest.get("scanner", "trivy"),
        "scanner_version": manifest.get("scanner_version", ""),
        "source": manifest.get("source", ""),
        "trigger": manifest.get("trigger", "manual"),
        "host": manifest.get("host", ""),
        "as_of": manifest.get("as_of", ""),
        "severity_floor": manifest.get("severity_floor", ""),
        "pkg_types": manifest.get("pkg_types", ""),
        "status": status,
        "complete": not unscannable and bool(images),
        "image_count": len(targets),
        "scanned_count": len(images),
        "vuln_count": len(vulns),
        "fixable_count": fixable,
        "truncated": len(vulns) > MAX_REPORTED,
        "images": images,
        "unscannable": unscannable,
        "vulns": vulns[:MAX_REPORTED],
    }


def verdict(doc: dict) -> int:
    """Map a document to a process exit code.

    Incompleteness outranks findings, and that ordering is the whole point. "We could not
    scan the web container's image" is the condition that otherwise gets silently filed
    as clean; a found CVE is at least visible in the report either way. So a partial scan
    exits non-zero even when everything it *did* manage to read was spotless.
    """
    if doc.get("status") == STATUS_ERROR or not doc.get("complete", False):
        return EXIT_INCOMPLETE
    if doc.get("fixable_count", 0) > 0:
        return EXIT_FINDINGS
    return EXIT_OK


def _who(entry: dict) -> str:
    """"web, worker: forca:prod" when we know the services, just the image otherwise.

    The running-image scan knows which containers share an image and says so — that is
    what an operator acts on. The deploy gate scans images that have no containers yet, so
    naming the image twice would be noise.
    """
    services = ", ".join(entry.get("services") or [])
    image = entry.get("image", "?")
    return f"{services}: {image}" if services else image


def render_text(doc: dict) -> str:
    """A human-readable rendering, for the deploy log and the systemd failure mail.

    This exists because the notification path can itself fail — the app may be down, or
    the ingest command may not be reachable — and when it does, the operator's only view
    of the finding is whatever the script printed. A scan whose findings live exclusively
    inside a delivery mechanism has the same defect as the Recommendation nobody read.
    """
    lines: list[str] = []
    scope = f"{doc.get('scanned_count', 0)}/{doc.get('image_count', 0)} image(s)"
    lines.append(
        f"Image scan ({doc.get('source', '?')}, trigger={doc.get('trigger', '?')}): "
        f"{scope} scanned at severity {doc.get('severity_floor', '?')}, "
        f"packages={doc.get('pkg_types', '?')}."
    )

    for item in doc.get("unscannable") or []:
        lines.append(f"  UNSCANNED  {_who(item)} — {item.get('error', '?')}")

    if doc.get("status") == STATUS_ERROR:
        lines.append("  RESULT: nothing could be scanned — this is not a clean bill of health.")
        return "\n".join(lines)

    for img in doc.get("images") or []:
        lines.append(
            f"  {_who(img)} [{img.get('os') or 'unknown base'}] "
            f"{img.get('vuln_count', 0)} finding(s), {img.get('fixable_count', 0)} fixable"
        )

    for v in doc.get("vulns") or []:
        fix = ", ".join(v.get("fix_versions") or []) or "no fix released yet"
        lines.append(
            f"    {v.get('severity', '?'):8s} {v.get('id', '?')}  "
            f"{v.get('package', '?')} {v.get('version', '?')} -> {fix}   [{v.get('image', '?')}]"
        )
    if doc.get("truncated"):
        lines.append(f"    ... list clipped at {MAX_REPORTED}; the counts above are exact.")

    if doc.get("fixable_count", 0):
        lines.append(
            f"  RESULT: {doc['fixable_count']} fixable finding(s) — rebuild on a newer base "
            "image (or bump the pinned tag in docker-compose.prod.yml) and redeploy."
        )
    elif doc.get("vuln_count", 0):
        lines.append(
            f"  RESULT: {doc['vuln_count']} finding(s), none fixable yet — recorded, not gated. "
            "Upstream has released no fix, so there is nothing to bump today."
        )
    else:
        lines.append("  RESULT: no fixable HIGH/CRITICAL findings.")
    return "\n".join(lines)


# The columns the shell half writes, one target per line. TSV rather than JSON because
# the alternative is quoting JSON out of bash — where a stray quote in a trivy error
# message produces a parse failure that looks exactly like a clean scan. Tabs and
# newlines are stripped from every field before it is written (see `scan_targets_row` in
# lib-trivy.sh), so the format cannot be broken by scanner output.
TSV_FIELDS = ("key", "image", "image_id", "services", "containers", "report", "error")


def manifest_from_tsv(path: Path, header: dict) -> dict:
    """Read the shell half's target list into a manifest dict.

    Blank lines are skipped; a short row is padded rather than rejected, so a future
    column can be added to the writer without this refusing to read older files.
    """
    targets: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        cells = line.split("\t")
        cells += [""] * (len(TSV_FIELDS) - len(cells))
        row = dict(zip(TSV_FIELDS, cells, strict=False))
        targets.append({
            "key": row["key"],
            "image": row["image"],
            "image_id": row["image_id"],
            "services": [s for s in row["services"].split(",") if s],
            "containers": [c for c in row["containers"].split(",") if c],
            "report": row["report"],
            "error": row["error"],
        })
    return {**header, "targets": targets}


def load_reports(manifest: dict, base_dir: Path) -> dict[str, dict | None]:
    """Read each target's trivy JSON. Unreadable or unparseable becomes ``None``.

    A truncated report (trivy killed mid-write, disk full) must not crash the run and
    must not be mistaken for an empty — i.e. clean — result, so it degrades to the same
    "unscannable" path as a missing file.
    """
    reports: dict[str, dict | None] = {}
    for target in manifest.get("targets") or []:
        key = target.get("key", "")
        path = target.get("report")
        if target.get("error") or not path:
            reports[key] = None
            continue
        try:
            reports[key] = json.loads((base_dir / path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            target["error"] = f"unreadable trivy report: {exc}"
            reports[key] = None
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("targets", help="TSV of scan targets written by the shell half.")
    parser.add_argument("--source", default="", help="running-containers | pending-images")
    parser.add_argument("--trigger", default="manual", choices=["scheduled", "deploy", "manual"])
    parser.add_argument("--host", default="")
    parser.add_argument("--as-of", dest="as_of", default="")
    parser.add_argument("--scanner-version", dest="scanner_version", default="")
    parser.add_argument("--severity-floor", dest="severity_floor", default="")
    parser.add_argument("--pkg-types", dest="pkg_types", default="")
    parser.add_argument("--json", dest="json_out", default="-",
                        help="Where to write the scan document ('-' for stdout).")
    parser.add_argument("--text", dest="text_out", default="",
                        help="Also write the human-readable rendering here ('-' for stderr).")
    args = parser.parse_args(argv)

    targets_path = Path(args.targets)
    manifest = manifest_from_tsv(targets_path, {
        "scanner": "trivy",
        "scanner_version": args.scanner_version,
        "source": args.source,
        "trigger": args.trigger,
        "host": args.host,
        "as_of": args.as_of,
        "severity_floor": args.severity_floor,
        "pkg_types": args.pkg_types,
    })
    doc = build_document(manifest, load_reports(manifest, targets_path.parent))

    payload = json.dumps(doc, indent=2, sort_keys=False)
    if args.json_out == "-":
        sys.stdout.write(payload + "\n")
    else:
        Path(args.json_out).write_text(payload + "\n", encoding="utf-8")

    if args.text_out:
        text = render_text(doc) + "\n"
        if args.text_out == "-":
            sys.stderr.write(text)
        else:
            Path(args.text_out).write_text(text, encoding="utf-8")

    return verdict(doc)


if __name__ == "__main__":
    raise SystemExit(main())
