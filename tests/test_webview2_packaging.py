"""The Windows build bundles, proves and re-verifies the pinned WebView2 Fixed Version runtime."""

import hashlib
import importlib.util
import json
import os
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "packaging" / "webview2-fixed-runtime.json"
VERSION = "156.0.4314.42"
FOLDER = f"Microsoft.WebView2.FixedVersionRuntime.{VERSION}.x64"


def windows_builder():
    spec = importlib.util.spec_from_file_location("webview2_build_windows", ROOT / "packaging/build_windows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pe_image(path, machine=0x8664):
    """A minimal DOS + PE header: enough for the machine check, nothing more."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytearray(0x80)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x40)
    header[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", header, 0x44, machine)
    path.write_bytes(bytes(header) + b"fixture body " + path.name.encode())
    return path


def runtime_folder(parent, name=FOLDER, machine=0x8664):
    folder = parent / name
    pe_image(folder / "msedgewebview2.exe", machine)
    pe_image(folder / "msedge.dll")
    (folder / "Locales").mkdir()
    (folder / "Locales" / "en-US.pak").write_bytes(b"locale fixture")
    return folder


def test_pin_names_the_exact_microsoft_cab():
    pin = json.loads(PIN.read_text(encoding="utf-8"))
    assert pin == {
        "version": VERSION,
        "architecture": "x64",
        "url": ("https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/"
                f"074c0930-36ad-4756-9d04-dfe0e503bd94/{FOLDER}.cab"),
        "bytes": 310135141,
        "sha256": "cb4612ce79a62a0be094301a72f73cf91bb250f5e83dc51886f66a083fc0694f",
        "folder": FOLDER,
    }
    assert windows_builder().load_webview2_pin() == pin


@pytest.mark.parametrize("change, message", [
    ({"version": "156.0"}, "four-part"),
    ({"architecture": "arm64"}, "x64"),
    ({"folder": "Microsoft.WebView2.FixedVersionRuntime.155.0.0.1.x64"}, "folder"),
    ({"url": "https://example.com/" + FOLDER + ".cab"}, "directly from Microsoft"),
    ({"bytes": "310135141"}, "size"),
    ({"sha256": "CB4612CE79A62A0BE094301A72F73CF91BB250F5E83DC51886F66A083FC0694F"}, "SHA-256"),
])
def test_pin_loader_rejects_an_inexact_pin(tmp_path, change, message):
    pin = {**json.loads(PIN.read_text(encoding="utf-8")), **change}
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(pin))
    with pytest.raises(ValueError, match=message):
        windows_builder().load_webview2_pin(path)


def test_pin_is_a_required_source_snapshot_input():
    module = windows_builder()
    license_hash = hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="omits build inputs") as error:
        module.verify_source_files({"files": {"LICENSE": license_hash}})
    assert "packaging/webview2-fixed-runtime.json" in str(error.value)


def test_stage_copies_the_unmodified_runtime_into_the_app_root(tmp_path):
    module = windows_builder()
    pin = module.load_webview2_pin()
    source = runtime_folder(tmp_path / "expanded")
    bundle = tmp_path / "nd2wsi-viewer"
    (bundle / "_internal").mkdir(parents=True)
    info = module.stage_webview2_runtime(source, bundle, pin)
    target = bundle / "_internal" / "webview2-runtime"
    names = ["Locales/en-US.pak", "msedge.dll", "msedgewebview2.exe"]
    for name in names:
        assert (target / name).read_bytes() == (source / name).read_bytes()
    digest = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names}
    assert info == {
        "distribution": "fixed-version-bundled",
        "version": VERSION,
        "architecture": "x64",
        "path": "_internal/webview2-runtime",
        "file_count": 3,
        "total_bytes": sum((source / name).stat().st_size for name in names),
        "files": digest,
        "browser_executable_sha256": digest["msedgewebview2.exe"],
        "cab": {"url": pin["url"], "bytes": pin["bytes"], "sha256": pin["sha256"]},
        "default_mode": "auto",
    }
    assert module.webview2_target(bundle) == target
    module.verify_staged_webview2_runtime(bundle, info)
    with pytest.raises(FileExistsError):
        module.stage_webview2_runtime(source, bundle, pin)
    (target / "Locales" / "en-US.pak").write_bytes(b"changed by the app")
    with pytest.raises(RuntimeError, match="changed after staging.*Locales/en-US.pak"):
        module.verify_staged_webview2_runtime(bundle, info)
    (target / "Locales" / "en-US.pak").write_bytes(b"locale fixture")
    (target / "debug.log").write_text("written at run time")
    with pytest.raises(RuntimeError, match="changed after staging.*debug.log"):
        module.verify_staged_webview2_runtime(bundle, info)


def test_runtime_notice_carries_microsofts_redistribution_terms():
    notice = windows_builder().WEBVIEW2_NOTICE.format(version=VERSION, architecture="x64")
    for phrase in (f"Microsoft Edge WebView2 Runtime (Fixed Version) {VERSION} (x64)",
                   "(c) Microsoft Corporation", "WebView2 Runtime license terms",
                   "not updated automatically", "network share", "Microsoft Defender SmartScreen",
                   "https://go.microsoft.com/fwlink/?LinkId=521839", "_internal\\webview2-runtime"):
        assert phrase in notice


def test_stage_accepts_a_renamed_folder_that_still_names_the_version(tmp_path):
    module = windows_builder()
    source = runtime_folder(tmp_path, name=f"webview2-{VERSION}")
    assert module.check_webview2_runtime(source, module.load_webview2_pin()) == source


def test_stage_rejects_a_missing_or_wrong_runtime(tmp_path):
    module = windows_builder()
    pin = module.load_webview2_pin()
    bundle = tmp_path / "bundle"
    (bundle / "_internal").mkdir(parents=True)

    empty = tmp_path / "missing" / FOLDER
    empty.mkdir(parents=True)
    with pytest.raises(ValueError, match="directly contain msedgewebview2.exe"):
        module.stage_webview2_runtime(empty, bundle, pin)
    with pytest.raises(ValueError, match="directly contain msedgewebview2.exe"):
        module.stage_webview2_runtime(empty.parent, bundle, pin)

    arm64 = runtime_folder(tmp_path / "arm64", machine=0xAA64)
    with pytest.raises(ValueError, match="not an x64.*0xaa64"):
        module.stage_webview2_runtime(arm64, bundle, pin)

    not_pe = runtime_folder(tmp_path / "text")
    (not_pe / "msedgewebview2.exe").write_text("not an executable")
    with pytest.raises(ValueError, match="Not a Windows PE image"):
        module.stage_webview2_runtime(not_pe, bundle, pin)

    other_version = runtime_folder(tmp_path / "other", name="Microsoft.WebView2.FixedVersionRuntime.155.0.0.1.x64")
    with pytest.raises(ValueError, match="not the pinned"):
        module.stage_webview2_runtime(other_version, bundle, pin)

    no_internal = tmp_path / "no-internal"
    no_internal.mkdir()
    with pytest.raises(RuntimeError, match="no _internal folder"):
        module.stage_webview2_runtime(runtime_folder(tmp_path / "good"), no_internal, pin)
    assert not (bundle / "_internal" / "webview2-runtime").exists()


def proof(executable, *, version=VERSION, bundled=True):
    return {"bundled": bundled, "browser_executable": str(executable), "browser_version": version,
            "browser_process_id": 4242, "requested_folder": str(Path(executable).parent),
            "expected_folder": str(Path(executable).parent), "user_data_folder": "udf",
            "system_version": VERSION, "error": None}


def bundled_report(executable):
    return {
        "ok": True,
        "webview2_runtime": {"selection": {"kind": "bundled", "mode": "bundled"}, "required": True,
                             "main": proof(executable), "translucent": proof(executable)},
        "independent_window": {"ok": True, "child_gui": {
            "ok": True, "webview2_runtime": {"required": True, "main": proof(executable)}}},
    }


def run_bundled_smoke(module, monkeypatch, tmp_path, report):
    runtime = tmp_path / "bundle" / "_internal" / "webview2-runtime"
    calls = []

    def run(command, *, env, timeout):
        calls.append((command, env, timeout))
        Path(env["ND2WSI_LOG_FILE"]).write_text("smoke ok: SDPC\nsmoke ok: Astromap JPEG, TXT and PDF")
        if "--gui-smoke" in command:
            Path(command[-1]).write_text(json.dumps(report))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    module.smoke(tmp_path / "viewer.exe", tmp_path / "input.nd2", tmp_path / "evidence",
                 bundled_webview2=runtime)
    return calls


@pytest.fixture
def bundled_browser(tmp_path, monkeypatch):
    monkeypatch.delenv("ND2WSI_WEBVIEW2_RUNTIME", raising=False)
    return pe_image(tmp_path / "bundle" / "_internal" / "webview2-runtime" / "msedgewebview2.exe")


def test_bundled_smoke_forces_the_runtime_and_accepts_full_proof(monkeypatch, tmp_path, bundled_browser):
    module = windows_builder()
    calls = run_bundled_smoke(module, monkeypatch, tmp_path, bundled_report(bundled_browser))
    assert [command[1] for command, _, _ in calls] == ["--smoke", "--gui-smoke"]
    assert [env["ND2WSI_WEBVIEW2_RUNTIME"] for _, env, _ in calls] == ["bundled", "bundled"]
    assert [timeout for _, _, timeout in calls] == [600, 600]


def test_unbundled_smoke_keeps_the_old_environment_and_budget(monkeypatch, tmp_path):
    module = windows_builder()
    monkeypatch.delenv("ND2WSI_WEBVIEW2_RUNTIME", raising=False)
    calls = []

    def run(command, *, env, timeout):
        calls.append((env, timeout))
        Path(env["ND2WSI_LOG_FILE"]).write_text("smoke ok: SDPC\nsmoke ok: Astromap JPEG, TXT and PDF")
        if "--gui-smoke" in command:
            Path(command[-1]).write_text('{"ok":true}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    module.smoke(tmp_path / "viewer.exe", tmp_path / "input.nd2", tmp_path / "evidence")
    assert [timeout for _, timeout in calls] == [600, 180]
    assert all("ND2WSI_WEBVIEW2_RUNTIME" not in env for env, _ in calls)


def _drop(*keys):
    def change(report, _system):
        parent = report
        for key in keys[:-1]:
            parent = parent[key]
        del parent[keys[-1]]
    return change


def _set(field, value, *keys):
    def change(report, system):
        target = report
        for key in keys:
            target = target[key]
        target[field] = system if value == "system" else value
    return change


@pytest.mark.parametrize("change, message", [
    (_drop("webview2_runtime"), "no WebView2 runtime evidence for the main window"),
    (_drop("webview2_runtime", "main"), "no WebView2 runtime evidence for the main window"),
    (_drop("webview2_runtime", "translucent"), "no WebView2 runtime evidence for the translucent viewer"),
    (_drop("independent_window"), "no WebView2 runtime evidence for the independent window"),
    (_drop("independent_window", "child_gui"), "no WebView2 runtime evidence for the independent window"),
    (_drop("independent_window", "child_gui", "webview2_runtime"),
     "no WebView2 runtime evidence for the independent window"),
    (_set("bundled", False, "webview2_runtime", "main"), "main window was not rendered by the bundled"),
    (_set("bundled", False, "webview2_runtime", "translucent"), "translucent viewer was not rendered"),
    (_set("bundled", False, "independent_window", "child_gui", "webview2_runtime", "main"),
     "independent window was not rendered"),
    (_set("browser_executable", "system", "webview2_runtime", "main"), "main window's WebView2 browser process"),
    (_set("browser_executable", "system", "webview2_runtime", "translucent"),
     "translucent viewer's WebView2 browser process"),
    (_set("browser_executable", "system", "independent_window", "child_gui", "webview2_runtime", "main"),
     "independent window's WebView2 browser process"),
    (_set("browser_executable", None, "webview2_runtime", "main"), "main window's WebView2 browser process"),
    (_set("browser_version", "155.0.3000.1", "webview2_runtime", "main"), "main window reported WebView2"),
    (_set("browser_version", None, "independent_window", "child_gui", "webview2_runtime", "main"),
     "independent window reported WebView2"),
])
def test_bundled_smoke_rejects_missing_or_foreign_runtime_proof(monkeypatch, tmp_path, bundled_browser,
                                                               change, message):
    module = windows_builder()
    # A same-named browser elsewhere, as the system Evergreen runtime would have.
    system = pe_image(tmp_path / "EdgeWebView" / "Application" / VERSION / "msedgewebview2.exe")
    report = bundled_report(bundled_browser)
    change(report, str(system))
    with pytest.raises(RuntimeError, match=message):
        run_bundled_smoke(module, monkeypatch, tmp_path, report)


def test_bundled_smoke_still_requires_overall_success(monkeypatch, tmp_path, bundled_browser):
    module = windows_builder()
    report = {**bundled_report(bundled_browser), "ok": False}
    with pytest.raises(RuntimeError, match="did not record successful completion"):
        run_bundled_smoke(module, monkeypatch, tmp_path, report)


def _job(workflow_text, name):
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(workflow_text)["jobs"][name]


def _step(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)


def test_workflow_fetches_verifies_and_bundles_the_pinned_runtime():
    workflow = (ROOT / ".github/workflows/windows.yml").read_text(encoding="utf-8")
    pin = json.loads(PIN.read_text(encoding="utf-8"))
    for value in (pin["version"], pin["url"], str(pin["bytes"]), pin["sha256"]):
        assert value in workflow
    assert "packaging/webview2-fixed-runtime.json" in workflow
    assert "ConvertFrom-Json" in workflow
    assert '"$env:SystemRoot\\System32\\curl.exe"' in workflow
    assert "--continue-at -" in workflow
    assert '"$env:SystemRoot\\System32\\expand.exe"' in workflow
    assert "O=Microsoft Corporation" in workflow
    # The signer subject is matched as a whole O= RDN, not as a substring.
    assert "(^|,\\s*)O=Microsoft Corporation(,|$)" in workflow
    assert "'msedgewebview2.exe', 'msedge.dll'" in workflow
    assert "ND2WSI_WEBVIEW2_FIXED_RUNTIME=$runtime" in workflow
    assert '--webview2-fixed-runtime "$env:ND2WSI_WEBVIEW2_FIXED_RUNTIME"' in workflow
    fetch = workflow.index("Fetch and verify the pinned WebView2 Fixed Version runtime")
    assert workflow.index("Build and install the source-verified LGPL SDPC decoder") < fetch
    assert fetch < workflow.index("Build executable and require packaged headless and GUI smoke")
    assert workflow.index("Windows unit and integration tests") < fetch

    build_text, verify_text = workflow.split("\n  verify:\n", 1)
    assert "ND2WSI_WEBVIEW2_RUNTIME: bundled" in build_text
    assert "ND2WSI_WEBVIEW2_RUNTIME: bundled" in verify_text
    assert "-RequireBundledWebView2" in verify_text
    assert "./packaging/ensure_webview2.ps1" in build_text and "./packaging/ensure_webview2.ps1" in verify_text


def test_workflow_forces_bundled_mode_only_on_the_packaged_steps():
    workflow = (ROOT / ".github/workflows/windows.yml").read_text(encoding="utf-8")
    build, verify = _job(workflow, "build"), _job(workflow, "verify")
    # Job-level env would leak into the pytest steps, which must keep the auto default.
    assert "ND2WSI_WEBVIEW2_RUNTIME" not in build.get("env", {})
    assert "ND2WSI_WEBVIEW2_RUNTIME" not in verify.get("env", {})
    forced = [step["name"] for job in (build, verify) for step in job["steps"]
              if step.get("env", {}).get("ND2WSI_WEBVIEW2_RUNTIME") == "bundled"]
    assert forced == ["Build executable and require packaged headless and GUI smoke",
                      "Verify the shipped EXE with Korean and space-containing paths"]
    assert "--webview2-fixed-runtime" in _step(build, "Build executable and require packaged headless and GUI smoke")["run"]
    assert "-RequireBundledWebView2" in _step(verify, "Verify the shipped EXE with Korean and space-containing paths")["run"]
    portable = next(step for step in build["steps"] if step.get("with", {}).get("name") == "windows-x64-portable")
    assert portable["with"]["compression-level"] == 0
    assert build["timeout-minutes"] == 80 and verify["timeout-minutes"] == 45
    fetch = _step(build, "Fetch and verify the pinned WebView2 Fixed Version runtime")
    assert fetch["timeout-minutes"] == 25
    # A stalled transfer must end the attempt so the resume loop gets control again.
    for flag in ("--speed-limit", "--speed-time", "--max-time", "--continue-at -"):
        assert flag in fetch["run"]
    restore = _step(build, "Restore the pinned WebView2 Fixed Version cab")
    save = _step(build, "Save the pinned WebView2 Fixed Version cab")
    assert restore["uses"] == "actions/cache/restore@v4" and save["uses"] == "actions/cache/save@v4"
    assert restore["with"]["key"] == save["with"]["key"] == (
        "webview2-fixed-${{ hashFiles('packaging/webview2-fixed-runtime.json') }}")
    assert save["if"] == "steps.webview2-cab.outputs.cache-hit != 'true'"
    names = [step.get("name") for step in build["steps"]]
    assert names.index("Restore the pinned WebView2 Fixed Version cab") < names.index(
        "Fetch and verify the pinned WebView2 Fixed Version runtime") < names.index(
        "Save the pinned WebView2 Fixed Version cab") < names.index(
        "Build executable and require packaged headless and GUI smoke")
    # A restored cab is used only after its size and SHA-256 match the pin.
    assert "Using the cached WebView2 runtime cab" in fetch["run"]


def test_verifier_proves_the_bundled_runtime_from_the_extracted_zip():
    verifier = (ROOT / "packaging/verify_windows.ps1").read_text(encoding="utf-8")
    assert "[switch]$RequireBundledWebView2" in verifier
    # Expand-Archive ignores the script's progress preference; .NET extraction is used instead.
    assert "[System.IO.Compression.ZipFile]::ExtractToDirectory($archivePath, $testRoot)" in verifier
    assert "Expand-Archive -LiteralPath" not in verifier
    assert "webview2_runtime" in verifier and "'fixed-version-bundled'" in verifier
    assert "nd2wsi-viewer\\_internal\\webview2-runtime" in verifier
    assert "browser_executable_sha256" in verifier and "file_count" in verifier
    assert "(^|,\\s*)O=Microsoft Corporation(,|$)" in verifier
    assert "$env:ND2WSI_WEBVIEW2_RUNTIME = $previousWebView2Mode" in verifier
    assert "@('independent_window', 'child_gui', 'webview2_runtime', 'main')" in verifier
    assert "@('webview2_runtime', 'translucent')" in verifier
    assert '-notlike "*$RunId*"' in verifier
    assert "'*\\nd2wsi-viewer\\_internal\\webview2-runtime\\msedgewebview2.exe'" in verifier
    assert "webview2_bundled_runtime_proven" in verifier


def test_verifier_runs_a_system_runtime_negative_control():
    verifier = (ROOT / "packaging/verify_windows.ps1").read_text(encoding="utf-8")
    assert "@{ name = 'gui-smoke-system'; flag = '--gui-smoke'; webview2 = 'system' }" in verifier
    assert "if ($null -ne $step.webview2) { $env:ND2WSI_WEBVIEW2_RUNTIME = $step.webview2 }" in verifier
    assert "(Get-JsonMember $controlProof @('bundled')) -ne $false" in verifier
    assert '$controlExecutable -like "*$runId*"' in verifier
    assert "webview2_negative_control_browser_executable" in verifier
    assert "elseif ($RequireBundledWebView2) { 600000 }" in verifier


def test_bundled_smoke_rejects_a_same_file_link_outside_the_staged_runtime(monkeypatch, tmp_path, bundled_browser):
    # samefile alone accepts a hard link; the reported path must also name the staged copy.
    outside = tmp_path / "elsewhere" / "msedgewebview2.exe"
    outside.parent.mkdir()
    os.link(bundled_browser, outside)
    module = windows_builder()
    with pytest.raises(RuntimeError, match="main window's WebView2 browser process"):
        report = bundled_report(bundled_browser)
        report["webview2_runtime"]["main"]["browser_executable"] = str(outside)
        run_bundled_smoke(module, monkeypatch, tmp_path, report)
