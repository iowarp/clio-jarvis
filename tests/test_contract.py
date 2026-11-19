"""RL40 contract checks for every clio-jarvis package (no solver runs here)."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from clio_jarvis._contract.spec import (
    PARTS,
    REPO_PACKAGES_DIR,
    ContractError,
    find_packages,
    load_contract,
    validate_view_descriptor,
)

PACKAGES = [p.name for p in find_packages()]
CONFIG = {"shared_dir": "/s", "private_dir": "/p"}


def test_three_packages_are_found():
    assert {"gray_scott", "pvserver", "xcompact3d"} <= set(PACKAGES)


@pytest.mark.parametrize("package", PACKAGES)
def test_contract_loads(package):
    contract = load_contract(REPO_PACKAGES_DIR / package)
    assert contract.pkg_type == f"clio_jarvis.{package}"
    assert len(contract.sha256) == 64
    document = contract.describe({}, {**CONFIG, "pkg_dir": str(contract.pkg_dir)})
    assert document["resources"]["process_count"] >= 1
    # walltime has no default: absent means no limit, so no duration is priced.
    assert "duration" not in document["resources"]


@pytest.mark.parametrize("package", PACKAGES)
def test_none_defaults_are_not_required_by_jarvis(package):
    # jarvis-cd's 'ppl append' treats default None as required unless the entry says otherwise.
    contract = load_contract(REPO_PACKAGES_DIR / package)
    required = set(contract.data["parameters"].get("required") or [])
    for entry in contract.menu:
        if entry.get("default") is None and entry["name"] not in required:
            assert entry.get("required") is False, entry["name"]


@pytest.mark.parametrize("package", PACKAGES)
def test_walltime_is_optional_uncapped_and_positive(package):
    contract = load_contract(REPO_PACKAGES_DIR / package)
    names = {entry["name"] for entry in contract.menu}
    base = {k: v for k, v in contract.resolve_config({}, {**CONFIG, "pkg_dir": str(contract.pkg_dir)}).items() if k in names}
    assert base["walltime"] is None
    contract.check_parameters(base)
    contract.check_parameters({**base, "walltime": 10 * 365 * 86400})
    assert contract.resources({**base, "walltime": 7})["duration"] == 7
    for bad in (0, -5):
        with pytest.raises(ValueError, match="walltime"):
            contract.check_parameters({**base, "walltime": bad})


def test_pvserver_max_lifetime_is_optional_uncapped_and_positive():
    contract = load_contract(REPO_PACKAGES_DIR / "pvserver")
    assert "max_lifetime" not in (contract.data["parameters"].get("required") or [])
    names = {entry["name"] for entry in contract.menu}
    base = {k: v for k, v in contract.resolve_config({}, {**CONFIG, "pkg_dir": str(contract.pkg_dir)}).items() if k in names}
    assert base["max_lifetime"] is None and base["hold"] is False
    contract.check_parameters(base)
    assert contract.service(base)["max_lifetime"] is None
    contract.check_parameters({**base, "max_lifetime": 10 * 365 * 86400})
    for bad in (0, -1):
        with pytest.raises(ValueError, match="max_lifetime"):
            contract.check_parameters({**base, "max_lifetime": bad})


def test_supervisor_lifetime_flag():
    sys.path.insert(0, str(REPO_PACKAGES_DIR / "pvserver"))
    try:
        import supervisor
    finally:
        sys.path.pop(0)
    assert supervisor.parse(["--state-dir", "x"]).max_lifetime is None
    assert supervisor.parse(["--state-dir", "x", "--max-lifetime", "5"]).max_lifetime == 5
    with pytest.raises(SystemExit):
        supervisor.parse(["--state-dir", "x", "--max-lifetime", "0"])


def _copy(package: str, tmp_path: Path) -> Path:
    target = tmp_path / package
    shutil.copytree(REPO_PACKAGES_DIR / package, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


@pytest.mark.parametrize("package", PACKAGES)
@pytest.mark.parametrize("part", PARTS)
def test_missing_part_is_refused_by_name(package, part, tmp_path):
    """LQ34: a package without one RL40 part is refused, and the error names the part."""
    pkg_dir = _copy(package, tmp_path)
    contract_path = pkg_dir / "contract.yaml"
    data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if part not in data:
        pytest.skip(f"{package} has no {part} part ({data['kind']})")
    del data[part]
    contract_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(ContractError) as caught:
        load_contract(pkg_dir)
    assert caught.value.part == part
    assert repr(part) in str(caught.value)


def test_service_part_is_required_for_a_service(tmp_path):
    pkg_dir = _copy("pvserver", tmp_path)
    data = yaml.safe_load((pkg_dir / "contract.yaml").read_text(encoding="utf-8"))
    assert data["kind"] == "service" and "service" in data


def test_missing_provider_is_refused(tmp_path):
    pkg_dir = _copy("gray_scott", tmp_path)
    (pkg_dir / "progress.py").unlink()
    with pytest.raises(ContractError) as caught:
        load_contract(pkg_dir)
    assert caught.value.part == "progress"


def test_unknown_parameter_in_output_path_is_refused(tmp_path):
    pkg_dir = _copy("xcompact3d", tmp_path)
    path = pkg_dir / "contract.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["outputs"][0]["path"] = "{nowhere}/data.bp5"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(ContractError) as caught:
        load_contract(pkg_dir)
    assert caught.value.part == "outputs"


@pytest.mark.parametrize("package", ["gray_scott", "xcompact3d"])
def test_view_descriptor_has_the_fields_the_paraview_view_reads(package):
    contract = load_contract(REPO_PACKAGES_DIR / package)
    config = contract.resolve_config({}, {**CONFIG, "pkg_dir": str(contract.pkg_dir)})
    for output in contract.data["outputs"]:
        view = contract.view_descriptor(output, config, growing=True)
        validate_view_descriptor(view)
        assert view["path_kind"] == "directory"
        assert view["format"] == "adios2-bp5"
        assert view["reader"] == "ADIOS2VTXReader"
        assert view["growing"] is True
        assert view["follow"]["signal"] == "reader-time-steps"
        assert view["follow"]["expected_steps"] >= 100  # the headline run lasts minutes
        assert view["path"].startswith("/s/run/")


def test_headline_defaults():
    gs = load_contract(REPO_PACKAGES_DIR / "gray_scott").defaults()
    assert gs["steps"] // gs["plotgap"] == 100
    x3 = load_contract(REPO_PACKAGES_DIR / "xcompact3d").defaults()
    assert x3["ilast"] // x3["ioutput"] == 120
    assert x3["ilast"] * x3["dt"] == pytest.approx(12.0)


def test_bad_parameter_is_refused():
    contract = load_contract(REPO_PACKAGES_DIR / "gray_scott")
    config = contract.resolve_config({"plotgap": 0}, {**CONFIG, "pkg_dir": "/x"})
    with pytest.raises(ValueError, match="plotgap"):
        contract.check_parameters(config)


# -- runtime adapters ------------------------------------------------------------------

jarvis_cd = pytest.importorskip("jarvis_cd")


def _context(package: str, **params):
    contract = load_contract(REPO_PACKAGES_DIR / package)
    config = contract.resolve_config(params, {**CONFIG, "pkg_dir": str(contract.pkg_dir)})
    return {**config, "pkg_type": contract.pkg_type, "pkg_id": package, "shared_dir": "/s"}


def test_gray_scott_progress_from_stdout():
    from clio_jarvis._contract.runtime import progress_adapter

    adapter = progress_adapter(REPO_PACKAGES_DIR / "gray_scott", _context("gray_scott", steps=60, plotgap=20))
    text = "".join(
        f"Simulation at step {20 * k} writing output step     {k}\n" for k in range(1, 4)
    )
    observations = adapter.observe_progress("========================================\n" + text)
    clio = [o.metadata["clio"] for o in observations]
    assert [c["step"] for c in clio] == [1, 2, 3]
    assert [c["visible_steps"] for c in clio] == [0, 1, 2]
    assert all(c["expected_steps"] == 3 for c in clio)
    assert clio[0]["view"]["path"] == "/s/run/gs.bp"
    final = adapter.finalize_progress_for_exit(0)
    assert final[-1].state.value == "completed"
    assert final[-1].metadata["clio"]["view"]["growing"] is False


def test_xcompact3d_progress_and_artifact_from_stdout(tmp_path):
    from clio_jarvis._contract.runtime import artifact_adapter, progress_adapter

    context = _context("xcompact3d", ilast=40, ioutput=20, run_dir=str(tmp_path))
    progress = progress_adapter(REPO_PACKAGES_DIR / "xcompact3d", context)
    artifacts = artifact_adapter(REPO_PACKAGES_DIR / "xcompact3d", context)
    text = (
        " clio: vtk.xml attribute defined (587 bytes)\n"
        "Writing snapshots =>           1\n"
        "Time for writing snapshots (s):   0.1\n"
        "Writing snapshots =>           2\n"
    )
    steps = progress.observe_progress(text)
    assert [o.metadata["clio"]["step"] for o in steps] == [1, 2]
    states = [o.state.value for o in artifacts.observe_artifacts(text)]
    assert states == ["producing", "available"]
    (tmp_path / "data.bp5").mkdir()
    (tmp_path / "data.bp5" / "md.idx").write_bytes(b"x" * 10)
    final = artifacts.finalize_artifacts_for_exit(0)
    assert final[-1].state.value == "finalized" and final[-1].size_bytes == 10
    failed = progress_adapter(REPO_PACKAGES_DIR / "xcompact3d", context)
    failed.observe_progress(text)
    assert failed.finalize_progress_for_exit(3)[-1].state.value == "failed"


def test_pvserver_ready_reports_the_chosen_port():
    from clio_jarvis._contract.runtime import artifact_adapter, progress_adapter

    context = _context("pvserver", port=0)
    adapter = progress_adapter(REPO_PACKAGES_DIR / "pvserver", context)
    observations = adapter.observe_progress("Waiting for client...\nclio-pvserver ready port=40123 url=cs://127.0.0.1:40123\n")
    assert len(observations) == 1 and observations[0].state.value == "ready"
    service = observations[0].metadata["clio"]["service"]
    assert service["ports"][0]["port"] == 40123
    assert service["ports"][0]["protection"]["bind"] == "loopback"
    assert service["stop"]["writes"] == "/s/pvserver/pvserver-stop.json"
    assert artifact_adapter(REPO_PACKAGES_DIR / "pvserver", context) is None


def test_adapter_ignores_other_packages():
    from clio_jarvis._contract.runtime import progress_adapter

    assert progress_adapter(REPO_PACKAGES_DIR / "gray_scott", {"pkg_type": "builtin.gray_scott"}) is None


# -- package helpers --------------------------------------------------------------------


def test_xcompact3d_input_rewrite_keeps_comments():
    from clio_jarvis.xcompact3d.pkg import i3d_value, set_i3d, vtk_xml

    template = (REPO_PACKAGES_DIR / "xcompact3d" / "config" / "input_DNS_Re1600.i3d").read_text(encoding="utf-8")
    text = set_i3d(template, "ilast", 2400)
    text = set_i3d(text, "nx", 129)
    text = set_i3d(text, "dt", 0.005)
    assert "ilast = 2400          ! Last iteration" in text
    assert i3d_value(text, "nx") == "129" and i3d_value(text, "ny") == "65"
    xml = vtk_xml((65, 65, 65), (0.05, 0.05, 0.05), ("ux", "vort"))
    assert 'WholeExtent="0 64 0 64 0 64"' in xml and '<DataArray Name="TIME">time</DataArray>' in xml


@pytest.mark.skipif(not Path("/proc/net/tcp").exists(), reason="needs Linux /proc/net/tcp")
def test_supervisor_ready_and_stop_with_a_fake_server(tmp_path):
    fake = tmp_path / "fake-pvserver"
    fake.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import socket, sys, time
            port = int(next(a for a in sys.argv if a.startswith("--server-port=")).split("=")[1])
            s = socket.socket(); s.bind(("127.0.0.1", port)); s.listen(1)
            print("Waiting for client...", flush=True)
            time.sleep(600)
            """
        )
    )
    fake.chmod(0o755)
    state = tmp_path / "state"
    supervisor = subprocess.Popen(
        [sys.executable, str(REPO_PACKAGES_DIR / "pvserver" / "supervisor.py"),
         "--state-dir", str(state), "--executable", str(fake), "--grace", "5"],
        stdout=subprocess.PIPE, text=True,
    )
    line = ""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not line.startswith("clio-pvserver ready"):
        line = supervisor.stdout.readline().strip()
    assert line.startswith("clio-pvserver ready port=")
    assert (state / "pvserver-ready.json").is_file()
    supervisor.send_signal(signal.SIGTERM)
    assert supervisor.wait(timeout=30) == 0
    record = yaml.safe_load((state / "pvserver-stop.json").read_text())
    assert record["schema"] == "clio.service-stop.v1"
    assert record["reason"] == "signal" and record["signal"] == "SIGTERM"
    assert not (state / "pvserver-supervisor.pid").exists()
    assert os.path.isfile(record["log"])


@pytest.mark.skipif(not Path("/proc/net/tcp").exists(), reason="needs Linux /proc/net/tcp")
def test_supervisor_restarts_after_a_client_session(tmp_path):
    """pvserver exits 0 after its client leaves; with --persist a new one takes the port."""
    fake = tmp_path / "fake-pvserver"
    counter = tmp_path / "launches"
    fake.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import pathlib, socket, sys, time
            counter = pathlib.Path({str(counter)!r})
            n = int(counter.read_text()) + 1 if counter.exists() else 1
            counter.write_text(str(n))
            port = int(next(a for a in sys.argv if a.startswith("--server-port=")).split("=")[1])
            s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port)); s.listen(1)
            print("Accepting connection(s): 127.0.0.1:%d" % port, flush=True)
            if n == 1:
                time.sleep(1); print("Client connected.", flush=True); print("Exiting...", flush=True)
                sys.exit(0)
            time.sleep(600)
            """
        )
    )
    fake.chmod(0o755)
    state = tmp_path / "state"
    supervisor = subprocess.Popen(
        [sys.executable, str(REPO_PACKAGES_DIR / "pvserver" / "supervisor.py"),
         "--state-dir", str(state), "--executable", str(fake), "--grace", "5", "--persist"],
        stdout=subprocess.PIPE, text=True,
    )
    lines = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not any(l.startswith("clio-pvserver restarted") for l in lines):
        lines.append(supervisor.stdout.readline().strip())
    assert any(l.startswith("clio-pvserver restarted port=") for l in lines), lines
    ready = yaml.safe_load((state / "pvserver-ready.json").read_text())
    assert ready["restarts"] == 1 and supervisor.poll() is None
    supervisor.send_signal(signal.SIGTERM)
    assert supervisor.wait(timeout=30) == 0
    record = yaml.safe_load((state / "pvserver-stop.json").read_text())
    assert record["reason"] == "signal" and record["restarts"] == 1
    assert counter.read_text() == "2"


def test_xcompact3d_body_case_plans_the_selftest_cylinder():
    from clio_jarvis.xcompact3d import body

    stl = REPO_PACKAGES_DIR / "xcompact3d" / "config" / "cylinder.stl"
    params = dict(resolution=12, angle=0.0, reference_length=0.0, span="auto", upstream=3.0,
                  downstream=10.0, lateral=3.0, nprocs=4, inflow=1.0, re=300.0)
    case = body.plan_body(stl, dt=0.005, **params)
    assert case.span == "periodic" and case.grid == (181, 90, 8) and case.decomposition == (2, 2)
    # A disc of diameter 12 points on each of the 8 planes: close to pi 6^2.
    assert 100 * 8 < case.mask.solid < 125 * 8
    text = body.input_i3d(case, inflow=1.0, re=300.0, dt=0.005, ilast=200, ioutput=20)
    assert "itype = 5" in text and "iibm = 1" in text and "i2dsim = 1" in text
    with pytest.raises(body.CaseError, match="use dt"):
        body.plan_body(stl, dt=0.05, **params)