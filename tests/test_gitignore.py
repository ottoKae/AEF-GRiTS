"""Check publishing rules with Git itself, independent of the user's excludes."""

from pathlib import Path
import os
import shutil
import subprocess


def test_gitignore_keeps_public_resources_and_excludes_local_artifacts(tmp_path):
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    shutil.copyfile(root / ".gitignore", tmp_path / ".gitignore")
    private_paths = [
        ".env", ".env.local", "credentials", "credentials.json",
        "application_default_credentials.json", "client_secret_demo.json",
        "private.key", "private.pem", ".plan_secret", ".session_secret",
        "outputs/catalog.parquet", "data/points.csv", "data/tile.zarr/zarr.json",
        "webapp/runs/task.json", "webapp/output/tile.tif", "webapp/server.pid",
        "webapp/acceptance/demo_20261104/result.json",
        "webapp/acceptance/demo_20261104/source.zip",
        ".hallmark/preflight.json", ".test-tmp/run/report.json",
        "tooling/diagnostics/demo_20261104/tests.xml",
        "webapp/acceptance/ACCEPTANCE_RESULTS_20260813.md",
        "webapp/acceptance/completed_tasks.png",
        "docs/code-plan-download-hardening-20260815.md",
        "docs/local-web-gui-execution-plan-20261030.zh-CN.md",
        "docs/map-browser-execution-plan-20261101.zh-CN.md",
        "docs/public-gui-plan-20261030.zh-CN.md",
        "docs/ntfs3-incident-20260815.md",
        "docs/download-hardening-validation-20260816.md",
        "docs/linux-download-validation-20260815.md",
        "docs/resource-bounded-validation-20260815.md",
        "docs/first-use-validation-20261104.zh-CN.md",
        "docs/web-onboarding-acceptance-20261102.zh-CN.md",
        "docs/web-onboarding-optimization-plan-20261102.zh-CN.md",
        "tooling/diagnostics/20260923_nan_strategy/README_ZH.md",
        "tooling/diagnostics/20260923_wuhan2024_coverage/query_coverage.py",
        "tooling/diagnostics/first_run_20261104/prepare.py",
        "tooling/diagnostics/onboarding_20261102/run_live.py",
        "tooling/diagnostics/source_auth_20261030/check_public.py",
        "tooling/diagnostics/gui_live_20261030/check_rejections.py",
        "tooling/diagnostics/gui_live_20261030/run_gui.py",
    ]
    public_paths = [
        ".env.example", "webapp/static/example-points.csv",
        "aef_grits/data/mgrs.parquet", "aef_grits/data/README.md",
        "website/index.html", "webapp/static/index.html",
        "docs/report.zh-CN.md", "docs/assets/map.png",
        "webapp/acceptance/README.md", "webapp/acceptance/anhui_small_aoi.geojson",
        "tests/test_gitignore.py", "tooling/diagnostics/demo_20261104/check.py",
        ".agents/skills/aef-grits-download/SKILL.md", ".github/workflows/cross-platform.yml",
        ".agents/skills/aef-grits-download/references/commands.md",
        "docs/native-alignment-audit-20261101.zh-CN.md",
        "docs/native-alignment-implementation-20261101.zh-CN.md",
        "docs/native-utm-release-20261102.md",
        "docs/ntfs-safe-download.zh-CN.md",
        "tooling/diagnostics/existing_grids_20261101/inventory.py",
        "tooling/diagnostics/existing_grids_20261101/spotcheck.py",
        "tooling/diagnostics/gui_live_20261030/verify_outputs.py",
        "tooling/diagnostics/native_alignment_20261101/audit.py",
        "tooling/diagnostics/native_alignment_20261101/finish_audit.py",
        "tooling/diagnostics/native_delivery_20261101/verify.py",
    ]
    for relative in private_paths + public_paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    result = subprocess.run(
        ["git", "-c", f"core.excludesFile={os.devnull}", "check-ignore", "--stdin", "-z"],
        cwd=tmp_path, input=("\0".join(private_paths + public_paths) + "\0").encode(),
        capture_output=True, check=True,
    )
    assert set(result.stdout.decode().rstrip("\0").split("\0")) == set(private_paths)
