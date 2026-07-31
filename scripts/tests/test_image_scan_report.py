"""Tests for the container-image scan: the judgement layer and the shell escape hatches.

Two things are covered here, and both are covered because they have already gone wrong in
this codebase or in ones like it:

**The judgement rules** (scripts/image_scan_report.py) — what counts as a finding, what
"we could not tell" looks like, which exit code follows. These are pure functions over
captured trivy output, so they need no docker, no trivy and no network.

**The shell flag parsing** — specifically that a security gate cannot be switched off by
accident. The image audit's escape hatch was once ``[ -n "$SKIP_DEPENDENCY_AUDIT" ]``,
which meant ``SKIP_DEPENDENCY_AUDIT=0`` — what an operator types to say "no, do not skip"
— silently DISABLED the gate. It shipped because nobody ever ran it with that value. So
these tests run it with that value.

These live under ``scripts/`` beside the code they test rather than in ``tests/``, and are
therefore outside the default ``testpaths``. Run them with ``make test-scripts``.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def _load_reporter():
    """Import scripts/image_scan_report.py by path — it is a script, not a package."""
    spec = importlib.util.spec_from_file_location(
        "image_scan_report", SCRIPTS / "image_scan_report.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rep = _load_reporter()


# --- fixtures: trimmed but structurally faithful trivy output ------------------------

def trivy_report(vulns, *, family="debian", name="13.5"):
    """One trivy report, in the shape trivy 0.70 actually emits (fields we read only)."""
    return {
        "SchemaVersion": 2,
        "Trivy": {"Version": "0.70.0"},
        "Metadata": {"OS": {"Family": family, "Name": name}},
        "Results": [{"Target": "img (debian 13.5)", "Class": "os-pkgs",
                     "Type": "debian", "Vulnerabilities": vulns}],
    }


def vuln(vid, pkg, version, fixed, severity="HIGH"):
    return {
        "VulnerabilityID": vid, "PkgName": pkg, "InstalledVersion": version,
        "FixedVersion": fixed, "Severity": severity,
        "PrimaryURL": f"https://avd.aquasec.com/nvd/{vid.lower()}",
        "Description": "x" * 5000,  # trivy ships kilobytes of prose we must not keep
    }


def target(key, image, *, services=(), report="r.json", error=""):
    return {"key": key, "image": image, "image_id": key, "services": list(services),
            "containers": [f"forca-{s}-1" for s in services], "report": report, "error": error}


def manifest(targets, **header):
    base = {"scanner": "trivy", "scanner_version": "0.70.0", "source": "running-containers",
            "trigger": "scheduled", "host": "forca-prod", "as_of": "2026-07-31T04:20:00Z",
            "severity_floor": "HIGH,CRITICAL", "pkg_types": "os"}
    return {**base, **header, "targets": targets}


# --- extract_vulns -------------------------------------------------------------------

def test_extract_keeps_coordinates_and_drops_the_prose():
    """We store package coordinates and the advisory id — never trivy's Description.

    A single advisory's description can run to several kilobytes. Keeping them would bloat
    the stored row and turn a director notification into a wall of text nobody reads.
    """
    got = rep.extract_vulns(trivy_report([vuln("CVE-2026-1", "libssl3", "3.3.3", "3.3.7", "CRITICAL")]))
    assert got == [{
        "id": "CVE-2026-1", "severity": "CRITICAL", "package": "libssl3", "version": "3.3.3",
        "fix_versions": ["3.3.7"], "url": "https://avd.aquasec.com/nvd/cve-2026-1",
    }]


def test_extract_splits_multiple_fix_versions_into_a_list():
    got = rep.extract_vulns(trivy_report([vuln("CVE-2026-2", "perl", "5.38", "5.38.2, 5.40.1")]))
    assert got[0]["fix_versions"] == ["5.38.2", "5.40.1"]


def test_extract_treats_an_empty_fixed_version_as_unfixable():
    got = rep.extract_vulns(trivy_report([vuln("CVE-2026-3", "util-linux", "2.40", "")]))
    assert got[0]["fix_versions"] == []


def test_extract_deduplicates_the_same_advisory_reported_twice():
    """Trivy lists a package once per ecosystem it was found under. Counting the same
    advisory twice would make a finding look like it doubled when nothing changed."""
    dupe = vuln("CVE-2026-4", "zlib", "1.3", "1.3.1")
    raw = trivy_report([dupe])
    raw["Results"].append({"Target": "other", "Class": "os-pkgs", "Vulnerabilities": [dict(dupe)]})
    assert len(rep.extract_vulns(raw)) == 1


def test_extract_skips_records_with_no_advisory_id():
    raw = trivy_report([{"PkgName": "ghost", "InstalledVersion": "1", "Severity": "HIGH"}])
    assert rep.extract_vulns(raw) == []


# --- build_document ------------------------------------------------------------------

def test_clean_scan_is_ok_and_complete():
    doc = rep.build_document(
        manifest([target("sha256:a", "nginx:1.30-alpine", services=["nginx"])]),
        {"sha256:a": trivy_report([], family="alpine", name="3.21.7")},
    )
    assert doc["status"] == rep.STATUS_OK
    assert doc["complete"] is True
    assert doc["vuln_count"] == 0 and doc["fixable_count"] == 0
    assert doc["images"][0]["os"] == "alpine 3.21.7"
    assert rep.verdict(doc) == rep.EXIT_OK


def test_fixable_and_unfixable_are_counted_separately():
    """The gate keys off fixable_count, never vuln_count. An advisory upstream has not
    fixed cannot be actioned by bumping a version, and failing every build on it produces
    permanent red — which gets the whole gate switched off."""
    doc = rep.build_document(
        manifest([target("sha256:a", "forca:prod", services=["web", "worker"])]),
        {"sha256:a": trivy_report([
            vuln("CVE-A", "libssl3", "3.3.3", "3.3.7", "CRITICAL"),
            vuln("CVE-B", "perl", "5.38", ""),
        ])},
    )
    assert doc["vuln_count"] == 2
    assert doc["fixable_count"] == 1
    assert doc["images"][0]["services"] == ["web", "worker"]
    assert rep.verdict(doc) == rep.EXIT_FINDINGS


def test_unfixable_only_findings_are_reported_but_do_not_fail():
    doc = rep.build_document(
        manifest([target("sha256:a", "forca:prod", services=["web"])]),
        {"sha256:a": trivy_report([vuln("CVE-B", "perl", "5.38", ""),
                                   vuln("CVE-C", "util-linux", "2.40", "")])},
    )
    assert doc["status"] == rep.STATUS_VULNERABLE
    assert doc["vuln_count"] == 2 and doc["fixable_count"] == 0
    assert rep.verdict(doc) == rep.EXIT_OK, "unfixable findings must not produce permanent red"
    assert "none fixable yet" in rep.render_text(doc)


def test_an_unscannable_target_makes_the_document_incomplete_not_clean():
    """A running container whose image was pruned cannot be scanned. Reporting the
    remaining images as a clean bill of health would be the exact intent-versus-reality
    lie this control exists to catch."""
    doc = rep.build_document(
        manifest([
            target("sha256:a", "nginx:1.30-alpine", services=["nginx"]),
            target("sha256:b", "forca:prod", services=["web"], report="", error="image gone"),
        ]),
        {"sha256:a": trivy_report([]), "sha256:b": None},
    )
    assert doc["status"] == rep.STATUS_OK  # about what we DID scan
    assert doc["complete"] is False        # ...but we did not scan everything
    assert doc["scanned_count"] == 1 and doc["image_count"] == 2
    assert doc["unscannable"][0]["services"] == ["web"]
    assert rep.verdict(doc) == rep.EXIT_INCOMPLETE


def test_nothing_scanned_at_all_is_an_error_never_ok():
    doc = rep.build_document(
        manifest([target("sha256:a", "forca:prod", services=["web"], report="", error="trivy failed")]),
        {"sha256:a": None},
    )
    assert doc["status"] == rep.STATUS_ERROR
    assert rep.verdict(doc) == rep.EXIT_INCOMPLETE
    assert "not a clean bill of health" in rep.render_text(doc)


def test_incompleteness_outranks_findings_in_the_exit_code():
    """"We could not tell" is the state that otherwise gets silently filed as clean, so it
    wins over "we found something" — which is visible in the report either way."""
    doc = rep.build_document(
        manifest([
            target("sha256:a", "forca:prod", services=["web"]),
            target("sha256:b", "nginx:1.30-alpine", services=["nginx"], report="", error="gone"),
        ]),
        {"sha256:a": trivy_report([vuln("CVE-A", "libssl3", "3.3.3", "3.3.7")]), "sha256:b": None},
    )
    assert doc["fixable_count"] == 1
    assert rep.verdict(doc) == rep.EXIT_INCOMPLETE


def test_critical_and_fixable_findings_sort_first():
    """The list is clipped, so the entries someone could action today must never be the
    ones truncated away."""
    doc = rep.build_document(
        manifest([target("sha256:a", "forca:prod", services=["web"])]),
        {"sha256:a": trivy_report([
            vuln("CVE-LOW-UNFIXED", "a", "1", "", "HIGH"),
            vuln("CVE-HIGH-FIXED", "b", "1", "2", "HIGH"),
            vuln("CVE-CRIT", "c", "1", "2", "CRITICAL"),
        ])},
    )
    assert [v["id"] for v in doc["vulns"]] == ["CVE-CRIT", "CVE-HIGH-FIXED", "CVE-LOW-UNFIXED"]


def test_the_vulnerability_list_is_clipped_but_the_counts_stay_exact():
    many = [vuln(f"CVE-{i:04d}", "pkg", "1", "2") for i in range(rep.MAX_REPORTED + 50)]
    doc = rep.build_document(
        manifest([target("sha256:a", "forca:prod", services=["web"])]),
        {"sha256:a": trivy_report(many)},
    )
    assert doc["vuln_count"] == rep.MAX_REPORTED + 50
    assert doc["fixable_count"] == rep.MAX_REPORTED + 50
    assert len(doc["vulns"]) == rep.MAX_REPORTED
    assert doc["truncated"] is True
    assert "clipped" in rep.render_text(doc)


def test_the_document_carries_the_scan_provenance():
    """A result is only as trustworthy as it is fresh and as its scope is known."""
    doc = rep.build_document(
        manifest([target("sha256:a", "forca:prod", services=["web"])], trigger="deploy"),
        {"sha256:a": trivy_report([])},
    )
    assert doc["schema"] == rep.SCHEMA_VERSION
    assert doc["trigger"] == "deploy"
    assert doc["as_of"] == "2026-07-31T04:20:00Z"
    assert doc["severity_floor"] == "HIGH,CRITICAL"
    assert doc["pkg_types"] == "os"
    assert doc["scanner_version"] == "0.70.0"


def test_render_text_names_the_unscannable_container_and_why():
    doc = rep.build_document(
        manifest([target("sha256:b", "forca:prod", services=["web"], report="", error="image was pruned")]),
        {"sha256:b": None},
    )
    text = rep.render_text(doc)
    assert "UNSCANNED" in text and "web" in text and "image was pruned" in text


# --- manifest / report loading -------------------------------------------------------

def test_manifest_from_tsv_round_trips_the_shell_halfs_rows(tmp_path):
    tsv = tmp_path / "targets.tsv"
    tsv.write_text(
        "sha256:a\tnginx:1.30-alpine\tsha256:a\tnginx\tforca-nginx-1\t1.json\t\n"
        "\n"  # blank lines are ignored
        "sha256:b\tforca:prod\tsha256:b\tweb,worker\tforca-web-1,forca-worker-1\t\tgone\n",
        encoding="utf-8",
    )
    man = rep.manifest_from_tsv(tsv, {"trigger": "manual"})
    assert man["trigger"] == "manual"
    assert [t["services"] for t in man["targets"]] == [["nginx"], ["web", "worker"]]
    assert man["targets"][1]["error"] == "gone"
    assert man["targets"][0]["report"] == "1.json"


def test_manifest_from_tsv_pads_a_short_row_rather_than_rejecting_it():
    """Forward compatibility: a writer that has not learned a new column yet must still
    produce a readable file, not a scan that hard-fails on parse."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.tsv"
        p.write_text("sha256:a\tnginx:1.30\n", encoding="utf-8")
        man = rep.manifest_from_tsv(p, {})
        assert man["targets"][0]["image"] == "nginx:1.30"
        assert man["targets"][0]["services"] == []


def test_an_unreadable_trivy_report_becomes_unscannable_not_empty(tmp_path):
    """A truncated report (trivy killed mid-write, disk full) parses as nothing found.
    Nothing-found and could-not-read must never be the same answer."""
    (tmp_path / "1.json").write_text('{"Results": [', encoding="utf-8")  # truncated
    man = manifest([target("sha256:a", "forca:prod", services=["web"], report="1.json")])
    reports = rep.load_reports(man, tmp_path)
    assert reports["sha256:a"] is None
    doc = rep.build_document(man, reports)
    assert doc["status"] == rep.STATUS_ERROR
    assert "unreadable trivy report" in doc["unscannable"][0]["error"]


def test_a_missing_trivy_report_file_becomes_unscannable(tmp_path):
    man = manifest([target("sha256:a", "forca:prod", services=["web"], report="nope.json")])
    assert rep.load_reports(man, tmp_path)["sha256:a"] is None


def test_cli_writes_the_document_and_returns_the_verdict(tmp_path):
    (tmp_path / "1.json").write_text(
        json.dumps(trivy_report([vuln("CVE-A", "libssl3", "3.3.3", "3.3.7", "CRITICAL")])),
        encoding="utf-8",
    )
    (tmp_path / "t.tsv").write_text(
        "sha256:a\tnginx:1.30-alpine\tsha256:a\tnginx\tforca-nginx-1\t1.json\t\n", encoding="utf-8"
    )
    out = tmp_path / "scan.json"
    rc = rep.main([str(tmp_path / "t.tsv"), "--json", str(out), "--source", "running-containers",
                   "--trigger", "scheduled", "--severity-floor", "HIGH,CRITICAL"])
    assert rc == rep.EXIT_FINDINGS
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["vulns"][0]["id"] == "CVE-A"
    assert doc["source"] == "running-containers"


def test_build_document_does_not_mutate_the_manifest_it_was_given():
    man = manifest([target("sha256:a", "forca:prod", services=["web"])])
    before = copy.deepcopy(man)
    rep.build_document(man, {"sha256:a": trivy_report([])})
    assert man == before


# --- the shell half: escape hatches and flag parsing ---------------------------------
# These are the tests the SKIP_DEPENDENCY_AUDIT=0 bug needed and never had.

def run_bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — fixed argv, no shell, test-local script text
        ["/bin/bash", "-c", script],  # noqa: S607
        cwd=REPO, capture_output=True, text=True, check=False,
        env={**os.environ, **(env or {})},
    )


SKIP_PROBE = '. scripts/lib.sh; if skip_gate PROBE; then echo SKIPPED; else echo RAN; fi'


def test_skip_gate_runs_the_gate_when_unset():
    assert run_bash(SKIP_PROBE).stdout.strip() == "RAN"


def test_skip_gate_does_not_disable_the_gate_when_set_to_zero():
    """THE regression test. `SKIP_DEPENDENCY_AUDIT=0` once turned the audit OFF, because
    "0" is a non-empty string. An operator typing 0 means "no". It must mean "no"."""
    for falsey in ("0", "no", "false", "off", "OFF", ""):
        assert run_bash(SKIP_PROBE, {"PROBE": falsey}).stdout.strip() == "RAN", falsey


def test_skip_gate_skips_only_on_an_affirmative_value():
    for truthy in ("1", "yes", "true", "on", "TRUE"):
        assert run_bash(SKIP_PROBE, {"PROBE": truthy}).stdout.strip() == "SKIPPED", truthy


def test_skip_gate_refuses_to_guess_at_an_unrecognised_value():
    """A security gate must not resolve "maybe" to "off"."""
    proc = run_bash(SKIP_PROBE, {"PROBE": "please"})
    assert proc.returncode != 0
    assert "SKIPPED" not in proc.stdout
    assert "neither on nor off" in proc.stderr


def test_the_os_image_gate_honours_its_documented_escape_hatch():
    proc = run_bash("bash scripts/audit-image-os.sh", {"SKIP_IMAGE_SCAN": "1"})
    assert proc.returncode == 0
    assert "SKIP_IMAGE_SCAN is set" in proc.stderr


def test_the_os_image_gate_is_not_disabled_by_skip_image_scan_zero():
    """Proves the gate can actually FAIL, and that "0" does not switch it off.

    Same invocation, same broken precondition (an unreadable compose file), only the flag
    differs: with 1 the script bows out cleanly, with 0 it walks into the precondition and
    aborts. If ``0`` were ever misread as "skip" again — the historical bug — both runs
    would exit 0 and this test would catch it.
    """
    env = {"COMPOSE_FILE": "/nonexistent.yml"}
    skipped = run_bash("bash scripts/audit-image-os.sh", {**env, "SKIP_IMAGE_SCAN": "1"})
    enforced = run_bash("bash scripts/audit-image-os.sh", {**env, "SKIP_IMAGE_SCAN": "0"})
    assert skipped.returncode == 0 and "SKIP_IMAGE_SCAN is set" in skipped.stderr
    assert enforced.returncode != 0
    assert "SKIP_IMAGE_SCAN is set" not in enforced.stderr


def test_the_running_scan_rejects_an_unknown_trigger():
    proc = run_bash("bash scripts/scan-running-images.sh --trigger weekly")
    assert proc.returncode != 0
    assert "--trigger must be one of" in proc.stderr


def test_the_running_scan_rejects_an_unknown_flag_instead_of_ignoring_it():
    """A typo'd flag that is silently ignored is a scan quietly running in the wrong mode."""
    proc = run_bash("bash scripts/scan-running-images.sh --exit-zerro")
    assert proc.returncode != 0
    assert "Unknown argument" in proc.stderr


def test_the_running_scan_prints_usage_on_help():
    proc = run_bash("bash scripts/scan-running-images.sh --help")
    assert proc.returncode == 0
    assert "--exit-zero" in proc.stdout


def test_every_shell_script_in_scripts_parses():
    """`bash -n` on the lot. Cheap, and it catches the class of error that otherwise only
    shows up at 04:20 on a production host with nobody watching."""
    scripts = sorted(SCRIPTS.rglob("*.sh"))
    assert len(scripts) >= 10, "expected to find the deployment scripts"
    for path in scripts:
        proc = subprocess.run(  # noqa: S603
            ["/bin/bash", "-n", str(path)],  # noqa: S607
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, f"{path.name}: {proc.stderr}"


def test_the_systemd_units_have_no_unsubstituted_placeholders_left_to_chance():
    """Both units carry __PLACEHOLDER__ tokens; every one of them must be something the
    installer actually substitutes, or the timer installs and never runs."""
    installer = (SCRIPTS / "install-image-scan-timer.sh").read_text(encoding="utf-8")
    # Derived from the installer, not hardcoded. A literal list here has to be edited by
    # hand every time a unit gains a placeholder, and a stale allow-list is the same class
    # of rot this test exists to catch — it would either fail on a placeholder that IS
    # substituted, or (worse, once someone "fixes" it by pasting the name in) stop noticing
    # one that is not.
    substituted = set(re.findall(r"s\|(__[A-Z_]+__)\|", installer))
    assert substituted, "no substitutions found in the installer — the parse above is wrong"

    for unit in (SCRIPTS / "systemd").glob("forca-image-scan.*"):
        text = unit.read_text(encoding="utf-8")
        leftovers = {w for w in text.split() if w.startswith("__") and w.endswith("__")}
        assert leftovers <= substituted, (
            f"{unit.name} has placeholders the installer never substitutes: "
            f"{leftovers - substituted} — the unit would install with the literal token in it"
        )
