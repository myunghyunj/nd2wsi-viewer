"""Pin the SDPC decoder cache in windows.yml.

Releases take their Windows assets from the main push run, so only pull requests may
reuse a cached decoder. Main pushes build from source and seed the cache, tags and
workflow_dispatch never touch it, and only a decoder whose packaged smoke passed is
saved. The inline Python steps run here against a synthetic codec build.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CODEC_INPUTS = ("build_sdpc_codec.py", "verify_sdpc_codec.py", "SDPC-CODEC-NOTICE.txt")
BUILD_STEP = "Build and install the source-verified LGPL SDPC decoder"
PACKAGED_SMOKE_STEP = "Build executable and require packaged headless and GUI smoke"
CACHE_KEY = "sdpc-codec-v1-Windows-X64-py3.13.14-0123abcd"
FAKE_SOURCES = {"ffmpeg-8.1.3.tar.xz": b"ffmpeg source", "av-16.0.1.tar.gz": b"pyav source"}
NATIVE_FILES = {"av/_core.cp313-win_amd64.pyd": b"core", "av.libs/avcodec-62.dll": b"avcodec"}


def load_steps(workflow, job="build"):
    document = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    return document["jobs"][job]["steps"]


def windows_build_steps():
    steps = load_steps("windows.yml")
    return steps, {step["id"]: step for step in steps if "id" in step}


def step_index(steps, *, name=None, step_id=None):
    matches = [index for index, step in enumerate(steps)
               if (name is not None and step.get("name") == name)
               or (step_id is not None and step.get("id") == step_id)]
    assert len(matches) == 1, (name, step_id, matches)
    return matches[0]


TOKEN = re.compile(r"\s*(?:'(?P<text>(?:[^']|'')*)'|(?P<op>==|!=|&&|\|\||\(|\))|(?P<name>[A-Za-z_][\w.-]*))")


def evaluate(expression, context):
    """Evaluate the subset of GitHub expression syntax the cache steps use.

    Unsupported syntax and unknown context names fail the test instead of guessing.
    """
    if isinstance(expression, bool):
        return expression
    expression = expression.strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    python, position = [], 0
    while position < len(expression):
        match = TOKEN.match(expression, position)
        assert match and match.end() > position, f"unsupported expression syntax: {expression[position:]!r}"
        position = match.end()
        if match["text"] is not None:
            python.append(repr(match["text"].replace("''", "'")))
        elif match["op"]:
            python.append({"&&": " and ", "||": " or "}.get(match["op"], match["op"]))
        elif match["name"] in ("true", "false"):
            python.append(match["name"].capitalize())
        elif match["name"] in context:
            python.append(repr(context[match["name"]]))
        elif re.fullmatch(r"steps\.[\w-]+\.outputs\.[\w-]+", match["name"]):
            python.append("''")  # an output a skipped or silent step never set
        elif re.fullmatch(r"steps\.[\w-]+\.outcome", match["name"]):
            python.append("'skipped'")
        else:
            raise AssertionError(f"unknown context name {match['name']!r} in {expression!r}")
    return bool(eval("".join(python), {"__builtins__": {}}))  # noqa: S307 - operators and literals only


def simulate(event, ref_type, entry):
    """Walk the decoder cache steps for one run.

    entry is None when no cache entry exists for the key, "intact" when the restored
    build passes re-verification and "rejected" when it does not.
    """
    _, by_id = windows_build_steps()
    context = {"github.event_name": event, "github.ref_type": ref_type}
    run = {"looked_up": False, "downloaded": False, "reused": False, "msys2": False, "saved": False}
    restore = by_id["sdpc-codec-cache"]
    if evaluate(restore["if"], context):
        lookup_only = evaluate(restore["with"].get("lookup-only", False), context)
        context["steps.sdpc-codec-cache.outcome"] = "success"
        context["steps.sdpc-codec-cache.outputs.cache-hit"] = "true" if entry else "false"
        run["looked_up"] = True
        run["downloaded"] = bool(entry) and not lookup_only
    if evaluate(by_id["sdpc-codec-reuse"]["if"], context):
        assert run["downloaded"], "re-verification ran without a downloaded cache entry"
        context["steps.sdpc-codec-reuse.outcome"] = "success"
        if entry == "intact":
            context["steps.sdpc-codec-reuse.outputs.reuse"] = "true"
    run["msys2"] = evaluate(by_id["msys2"].get("if", True), context)
    run["reused"] = context.get("steps.sdpc-codec-reuse.outputs.reuse") == "true"
    if evaluate(by_id["sdpc-codec-stage"]["if"], context):
        context["steps.sdpc-codec-stage.outcome"] = "success"
        context["steps.sdpc-codec-stage.outputs.staged"] = "true"
    save = next(step for step in windows_build_steps()[0] if str(step.get("uses", "")).startswith("actions/cache/save@")
                and step["with"]["key"] == "${{ steps.sdpc-codec-cache.outputs.cache-primary-key }}")
    run["saved"] = evaluate(save["if"], context)
    return run


FROM_SOURCE_WITHOUT_CACHE = {"looked_up": False, "downloaded": False, "reused": False, "msys2": True, "saved": False}


@pytest.mark.parametrize(("event", "ref_type", "entry", "expected"), [
    ("pull_request", "branch", None,
     {"looked_up": True, "downloaded": False, "reused": False, "msys2": True, "saved": True}),
    ("pull_request", "branch", "intact",
     {"looked_up": True, "downloaded": True, "reused": True, "msys2": False, "saved": False}),
    ("pull_request", "branch", "rejected",
     {"looked_up": True, "downloaded": True, "reused": False, "msys2": True, "saved": False}),
    # Releases take their Windows assets from this run: always build from source.
    ("push", "branch", None,
     {"looked_up": True, "downloaded": False, "reused": False, "msys2": True, "saved": True}),
    ("push", "branch", "intact",
     {"looked_up": True, "downloaded": False, "reused": False, "msys2": True, "saved": False}),
    ("push", "tag", None, FROM_SOURCE_WITHOUT_CACHE),
    ("push", "tag", "intact", FROM_SOURCE_WITHOUT_CACHE),
    ("workflow_dispatch", "branch", None, FROM_SOURCE_WITHOUT_CACHE),
    ("workflow_dispatch", "branch", "intact", FROM_SOURCE_WITHOUT_CACHE),
])
def test_only_pull_requests_reuse_the_cached_decoder(event, ref_type, entry, expected):
    assert simulate(event, ref_type, entry) == expected


def test_cache_entry_matches_this_checkouts_pinned_inputs_exactly():
    steps, by_id = windows_build_steps()
    restore = by_id["sdpc-codec-cache"]
    assert restore["uses"].startswith("actions/cache/restore@")
    # A prefix match through restore-keys would restore a build of other inputs and
    # leave cache-primary-key pointing at an entry this run never checked.
    assert set(restore["with"]) <= {"path", "key", "lookup-only"}
    key = restore["with"]["key"]
    assert "${{ steps.python.outputs.python-version }}" in key
    assert "${{ runner.os }}" in key and "${{ runner.arch }}" in key
    hashed = re.search(r"hashFiles\(([^)]*)\)", key)
    assert hashed and sorted(re.findall(r"'packaging/([^']+)'", hashed[1])) == sorted(CODEC_INPUTS)
    saves = [step for step in steps if str(step.get("uses", "")).startswith("actions/cache/save@")
             and step["with"]["path"] == restore["with"]["path"]]
    assert len(saves) == 1
    assert saves[0]["with"] == {"path": restore["with"]["path"],
                                "key": "${{ steps.sdpc-codec-cache.outputs.cache-primary-key }}"}
    build = steps[step_index(steps, name=BUILD_STEP)]
    assert build["env"]["SDPC_CODEC_REUSE"] == "${{ steps.sdpc-codec-reuse.outputs.reuse }}"
    assert "if ($env:SDPC_CODEC_REUSE -ceq 'true')" in build["run"]


def test_decoder_is_cached_only_after_the_packaged_smoke_passed():
    steps, by_id = windows_build_steps()
    order = [step_index(steps, step_id="sdpc-codec-cache"), step_index(steps, step_id="sdpc-codec-reuse"),
             step_index(steps, step_id="msys2"), step_index(steps, name=BUILD_STEP),
             step_index(steps, name=PACKAGED_SMOKE_STEP), step_index(steps, step_id="sdpc-codec-stage")]
    save = next(index for index, step in enumerate(steps)
                if str(step.get("uses", "")).startswith("actions/cache/save@")
                and step["with"]["key"] == "${{ steps.sdpc-codec-cache.outputs.cache-primary-key }}")
    assert order + [save] == sorted(order + [save])
    # The smoke step and everything before it run without always(), so a failed
    # smoke skips staging; the stage copies the source-built output only.
    assert "always()" not in by_id["sdpc-codec-stage"]["if"]
    assert "build/sdpc-codec" in by_id["sdpc-codec-stage"]["run"]
    assert "Join-Path $env:GITHUB_WORKSPACE 'build\\sdpc-codec'" in steps[step_index(steps, name=BUILD_STEP)]["run"]


def test_macos_release_never_restores_the_decoder_from_a_cache():
    document = yaml.safe_load((WORKFLOWS / "macos-release.yml").read_text(encoding="utf-8"))
    for job in document["jobs"].values():
        for step in job.get("steps", []):
            assert not str(step.get("uses", "")).startswith("actions/cache"), step


def load_packaging_script(name):
    spec = importlib.util.spec_from_file_location(f"cache_test_{name}", ROOT / "packaging" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def make_checkout(tmp_path):
    """A checkout whose verifier pins FAKE_SOURCES instead of the reviewed releases."""
    packaging = tmp_path / "checkout" / "packaging"
    packaging.mkdir(parents=True)
    pinned = {name: sha256(data) for name, data in FAKE_SOURCES.items()}
    verifier = (ROOT / "packaging" / "verify_sdpc_codec.py").read_text(encoding="utf-8")
    verifier, count = re.subn(r"^EXPECTED_SOURCES = \{.*?^\}", f"EXPECTED_SOURCES = {pinned!r}",
                              verifier, flags=re.S | re.M)
    assert count == 1
    (packaging / "verify_sdpc_codec.py").write_text(verifier, encoding="utf-8")
    for name in ("build_sdpc_codec.py", "SDPC-CODEC-NOTICE.txt"):
        shutil.copyfile(ROOT / "packaging" / name, packaging / name)
    return packaging.parent


def make_codec_build(checkout, output):
    """Write what build_sdpc_codec.py leaves in its output folder, plus build leftovers."""
    builder = load_packaging_script("build_sdpc_codec")
    verifier = load_packaging_script("verify_sdpc_codec")
    wheel = output / "wheelhouse" / "av-16.0.1-cp313-cp313-win_amd64.whl"
    wheel.parent.mkdir(parents=True)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in NATIVE_FILES.items():
            archive.writestr(name, data)
        archive.writestr("av/__init__.py", b"")
    licenses = output / "licenses"
    licenses.mkdir()
    (licenses / "FFmpeg-LGPL-2.1.txt").write_text("LGPL\n", encoding="utf-8")
    (licenses / "PyAV-BSD-3-Clause.txt").write_text("BSD\n", encoding="utf-8")
    shutil.copyfile(checkout / "packaging" / "SDPC-CODEC-NOTICE.txt", licenses / "SDPC-CODEC-NOTICE.txt")
    (output / "downloads").mkdir()
    (output / "downloads" / "ffmpeg-8.1.3.tar.xz").write_bytes(FAKE_SOURCES["ffmpeg-8.1.3.tar.xz"])
    source_archive = output / "sdpc-codec-sources-windows-x64.zip"
    with zipfile.ZipFile(source_archive, "w") as archive:
        for name, data in FAKE_SOURCES.items():
            archive.writestr("sources/" + name, data)
        for name in CODEC_INPUTS:
            archive.write(checkout / "packaging" / name, "packaging/" + name)
    manifest = {
        "schema_version": 1, "platform": "windows-x64", "python_version": platform.python_version(),
        "ffmpeg_version": builder.FFMPEG_VERSION, "pyav_version": builder.PYAV_VERSION,
        "license": "LGPL-2.1-or-later", "configure_args": sorted(verifier.REQUIRED_FLAGS),
        "sources": [{"filename": name, "sha256": sha256(data)} for name, data in FAKE_SOURCES.items()],
        "native_files": {name: sha256(data) for name, data in NATIVE_FILES.items()},
        "wheel_file": "wheelhouse/" + wheel.name, "wheel_sha256": sha256(wheel.read_bytes()),
        "source_archive_file": source_archive.name,
        "source_archive_sha256": sha256(source_archive.read_bytes()),
        "licenses_directory": "licenses",
    }
    (output / "sdpc-codec-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def run_step(step_id, checkout, runner_temp):
    _, by_id = windows_build_steps()
    step = by_id[step_id]
    assert step["shell"] == "python"
    script = runner_temp.parent / f"{step_id}.py"
    script.write_text(step["run"], encoding="utf-8")
    github_output = runner_temp.parent / f"{step_id}-output.txt"
    github_output.write_text("", encoding="utf-8")
    env = {**os.environ, "RUNNER_TEMP": str(runner_temp), "GITHUB_OUTPUT": str(github_output),
           "SDPC_CODEC_KEY": CACHE_KEY}
    result = subprocess.run([sys.executable, "-I", str(script)], cwd=checkout, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    outputs = dict(line.split("=", 1) for line in github_output.read_text(encoding="utf-8").splitlines() if line)
    return outputs, result.stdout + result.stderr


@pytest.fixture
def runner(tmp_path):
    checkout = make_checkout(tmp_path)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    return checkout, runner_temp


def test_staged_decoder_is_reused_by_a_later_pull_request(runner):
    checkout, runner_temp = runner
    make_codec_build(checkout, checkout / "build" / "sdpc-codec")
    outputs, log = run_step("sdpc-codec-stage", checkout, runner_temp)
    assert outputs == {"staged": "true"}, log
    cache = runner_temp / "sdpc-codec"
    assert sorted(path.relative_to(cache).as_posix() for path in cache.rglob("*") if path.is_file()) == [
        "licenses/FFmpeg-LGPL-2.1.txt", "licenses/PyAV-BSD-3-Clause.txt", "licenses/SDPC-CODEC-NOTICE.txt",
        "sdpc-codec-manifest.json", "sdpc-codec-sources-windows-x64.zip",
        "wheelhouse/av-16.0.1-cp313-cp313-win_amd64.whl"]
    shutil.rmtree(checkout / "build")  # a later run starts from a clean checkout
    outputs, log = run_step("sdpc-codec-reuse", checkout, runner_temp)
    assert outputs == {"reuse": "true"}, log
    assert "::notice title=SDPC decoder restored from cache::" in log


def test_stage_never_merges_into_a_leftover_cache_folder(runner):
    checkout, runner_temp = runner
    make_codec_build(checkout, checkout / "build" / "sdpc-codec")
    (runner_temp / "sdpc-codec").mkdir()
    outputs, log = run_step("sdpc-codec-stage", checkout, runner_temp)
    assert outputs == {}
    assert "::warning title=SDPC decoder not cached::" in log


def test_stage_refuses_a_build_the_verifier_rejects(runner):
    checkout, runner_temp = runner
    output = checkout / "build" / "sdpc-codec"
    make_codec_build(checkout, output)
    wheel = output / "wheelhouse" / "av-16.0.1-cp313-cp313-win_amd64.whl"
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    outputs, log = run_step("sdpc-codec-stage", checkout, runner_temp)
    assert outputs == {}
    assert "::warning title=SDPC decoder not cached::" in log


def append(path, data=b"\n# changed\n"):
    path.write_bytes(path.read_bytes() + data)


def built_for_another_python(checkout, cache):
    manifest_path = cache / "sdpc-codec-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["python_version"] = "3.12.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("change", [
    pytest.param(lambda checkout, cache: append(checkout / "packaging" / "build_sdpc_codec.py"),
                 id="changed-build-script-in-checkout"),
    pytest.param(lambda checkout, cache: append(checkout / "packaging" / "verify_sdpc_codec.py"),
                 id="changed-verifier-in-checkout"),
    pytest.param(lambda checkout, cache: append(checkout / "packaging" / "SDPC-CODEC-NOTICE.txt"),
                 id="changed-notice-in-checkout"),
    pytest.param(lambda checkout, cache: append(cache / "wheelhouse" / "av-16.0.1-cp313-cp313-win_amd64.whl",
                                                b"x"), id="tampered-cached-wheel"),
    pytest.param(lambda checkout, cache: append(cache / "licenses" / "SDPC-CODEC-NOTICE.txt"),
                 id="tampered-cached-notice"),
    pytest.param(lambda checkout, cache: (cache / "sdpc-codec-manifest.json").unlink(),
                 id="missing-cached-manifest"),
    pytest.param(lambda checkout, cache: (cache / "sdpc-codec-sources-windows-x64.zip").unlink(),
                 id="missing-cached-source-archive"),
    pytest.param(built_for_another_python, id="built-for-another-python"),
])
def test_restored_decoder_that_does_not_match_this_checkout_is_rebuilt(runner, change):
    checkout, runner_temp = runner
    cache = runner_temp / "sdpc-codec"
    make_codec_build(checkout, cache)
    change(checkout, cache)
    outputs, log = run_step("sdpc-codec-reuse", checkout, runner_temp)
    assert outputs == {}
    assert "::warning title=SDPC decoder cache rejected::" in log
    assert CACHE_KEY in log
