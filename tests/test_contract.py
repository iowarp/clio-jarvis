"""RL40 contract checks for every clio-jarvis package (no solver runs here)."""

from __future__ import annotations

import json
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

from clio_jarvis._contract import spec
from clio_jarvis._contract.spec import (
    PARTS,
    REPO_PACKAGES_DIR,
    ContractError,
    find_packages,
    is_view,
    load_contract,
    validate_view_descriptor,
)

PACKAGES = [p.name for p in find_packages()]
CONFIG = {"shared_dir": "/s", "private_dir": "/p"}


def test_packages_are_found():
    assert {"gray_scott", "gromacs", "lammps", "pvserver", "warpx", "wrf"} <= set(PACKAGES)


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
    # A valid run: required inputs (script, mdp, ...) come from the selftest, walltime is left out.
    given = {k: v for k, v in (contract.data.get("selftest") or {}).get("params", {}).items() if k != "walltime"}
    base = {k: v for k, v in contract.resolve_config(given, {**CONFIG, "pkg_dir": str(contract.pkg_dir)}).items() if k in names}
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


def test_supervisor_runs_the_launch_jarvis_built(tmp_path):
    sys.path.insert(0, str(REPO_PACKAGES_DIR / "pvserver"))
    try:
        import supervisor
    finally:
        sys.path.pop(0)
    with pytest.raises(SystemExit):
        supervisor.parse(["--state-dir", "x", "--nprocs", "2"])
    launch = tmp_path / "launch.json"
    launch.write_text(json.dumps({"command": "mpiexec -n 2 pvserver --server-port=1", "env": {}}))
    with pytest.raises(SystemExit):
        supervisor.parse(["--state-dir", "x", "--nprocs", "2", "--launch", str(launch)])
    launch.write_text(json.dumps({
        "command": f"mpiexec -x A=\"b\" -n 2 pvserver --server-port={supervisor.PORT_PLACEHOLDER}",
        "env": {"OMPI_MCA_btl_tcp_if_include": "10.0.0.0/8"},
    }))
    args = supervisor.parse(["--state-dir", "x", "--nprocs", "2", "--launch", str(launch)])
    command, environment = supervisor.server_command(args, 4242)
    assert command == 'mpiexec -x A="b" -n 2 pvserver --server-port=4242'
    assert environment["OMPI_MCA_btl_tcp_if_include"] == "10.0.0.0/8"
    single, own = supervisor.server_command(supervisor.parse(["--state-dir", "x"]), 7)
    assert single[0] == "pvserver" and "--server-port=7" in single and own is None


def test_a_start_failure_carries_the_launch_advisories(tmp_path):
    sys.path.insert(0, str(REPO_PACKAGES_DIR / "pvserver"))
    try:
        import supervisor
    finally:
        sys.path.pop(0)
    advisory = {"code": "site_fact_uncertain", "reason_codes": ["ucx_device_unusable"], "severity": "advisory"}
    launch = tmp_path / "launch.json"
    launch.write_text(json.dumps({
        "schema_version": "jarvis.mpi-launch.v1",
        "command": f"mpiexec -n 2 pvserver --server-port={supervisor.PORT_PLACEHOLDER}",
        "env": {},
        "hosts": ["a", "b"],
        "network": {"applied": {"launcher_env": {"X": "1"}}},
        "advisories": [advisory, "not-a-dict"],
    }))
    args = supervisor.parse(["--state-dir", str(tmp_path), "--nprocs", "2", "--launch", str(launch)])
    record = supervisor.Supervisor(args).diagnosis("exited_before_ready")
    assert record["advisories"] == [advisory, "not-a-dict"]
    assert record["reason_codes"] == ["ucx_device_unusable"]
    assert record["hosts"] == ["a", "b"]
    plain = supervisor.Supervisor(supervisor.parse(["--state-dir", str(tmp_path)])).diagnosis("start_timeout")
    assert "advisories" not in plain and "reason_codes" not in plain


def test_pvserver_keeps_jarvis_after_steps_default():
    # A view that outlives the solver is the person's choice; the package sets no after_steps of its own.
    contract = load_contract(REPO_PACKAGES_DIR / "pvserver")
    assert "after_steps" not in {entry["name"] for entry in contract.menu}
    assert "after_steps" not in (contract.data.get("parameters") or {}).get("properties", {})


def _copy(package: str, tmp_path: Path) -> Path:
    target = tmp_path / package
    shutil.copytree(REPO_PACKAGES_DIR / package, target, ignore=shutil.ignore_patterns("__pycache__"))
    if not (tmp_path / "_spack").exists():
        shutil.copytree(REPO_PACKAGES_DIR / "_spack", tmp_path / "_spack")
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
    pkg_dir = _copy("gray_scott", tmp_path)
    path = pkg_dir / "contract.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["outputs"][0]["path"] = "{nowhere}/gs.bp"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(ContractError) as caught:
        load_contract(pkg_dir)
    assert caught.value.part == "outputs"


def test_view_descriptor_has_the_fields_the_paraview_view_reads():
    contract = load_contract(REPO_PACKAGES_DIR / "gray_scott")
    config = contract.resolve_config({}, {**CONFIG, "pkg_dir": str(contract.pkg_dir)})
    views = [output for output in contract.data["outputs"] if is_view(output)]
    assert len(views) == 1
    for output in views:
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


def test_revealing_lists_the_parent_until_the_output_shows(tmp_path, monkeypatch):
    from clio_jarvis._contract import files

    output = tmp_path / "run" / "gs.bp"
    output.parent.mkdir()
    listed = []
    monkeypatch.setattr(files, "_relist_parents", lambda path: listed.append(str(path)))
    with files.revealing(output, interval=0.05):
        time.sleep(0.3)
        output.mkdir()
        time.sleep(0.3)
        seen = len(listed)
        time.sleep(0.3)
        assert len(listed) == seen
    assert seen >= 2 and set(listed) == {str(output)}


def test_fresh_size_relists_parents_when_a_name_looks_missing(tmp_path, monkeypatch):
    from clio_jarvis._contract import files

    index = tmp_path / "run" / "data.bp5" / "md.idx"
    index.parent.mkdir(parents=True)
    index.write_bytes(b"x" * 547)
    real_open, real_listdir = os.open, os.listdir
    listed = []

    def cached_miss(path, *args, **kwargs):
        # What a node that looked before the file existed gets until a directory listing.
        if Path(path) == index and Path(index.parent) not in listed:
            raise FileNotFoundError(path)
        return real_open(path, *args, **kwargs)

    def listdir(path):
        listed.append(Path(path))
        return real_listdir(path)

    monkeypatch.setattr(files.os, "open", cached_miss)
    monkeypatch.setattr(files.os, "listdir", listdir)
    assert files.fresh_size(index) == 547
    assert listed[0] == index.parent
    with pytest.raises(FileNotFoundError):
        files.fresh_size(tmp_path / "run" / "absent" / "md.idx")
    assert tmp_path / "run" in listed


UCX_SKIP = (
    "[1763766000.123456] [comp-05:4242:0]  ucp_context.c:1081 UCX  WARN  network device 'mlx5_0:1' "
    "is not available, please use one or more of: 'eno1'(tcp), 'lo'(tcp)"
)


def test_transport_warnings_are_told_from_errors():
    from clio_jarvis._contract import transport

    assert transport.library(UCX_SKIP) == "ucx"
    assert transport.library("libfabric:4242:1763766000::verbs:fabric:vrb_get_info():123<warn> no RDMA devices found") == "libfabric"
    assert transport.library("No OpenFabrics connection schemes reported that they were able to be used on a specific port.") == "openfabrics"
    assert transport.library("[comp-05:4242:0] ucp_ep.c:1500 UCX  ERROR  failed to connect: Connection refused") is None
    assert transport.library("Time step =  20/900, Time unit =   0.2000") is None
    assert transport.TransportNotes().report() is None


def test_gray_scott_ranks_start_through_the_node_flusher(tmp_path, monkeypatch):
    import shlex

    from clio_jarvis._contract import flushwrap
    from clio_jarvis.gray_scott import pkg
    from clio_jarvis.gray_scott.pkg import FLUSH_SECONDS, GrayScott

    seen = {}

    class FakeExec:
        def __init__(self, command, info):
            seen.update(command=command, cwd=info.cwd, nprocs=info.nprocs, ppn=info.ppn)

        def run(self):
            return type("Done", (), {"exit_code": {"localhost": 0}})()

    monkeypatch.setattr(pkg, "Exec", FakeExec)
    for name, value in (("hostfile", None), ("mod_env", {}), ("runtime_line_callback", lambda self: None)):
        monkeypatch.setattr(GrayScott, name, value, raising=False)
    package = GrayScott.__new__(GrayScott)
    _, menu = spec.read_menu(REPO_PACKAGES_DIR / "gray_scott")
    package.config = {**{entry["name"]: entry.get("default") for entry in menu}, "run_dir": str(tmp_path / "run"), "ppn": 4}
    package.start()
    words = shlex.split(seen["command"])
    assert seen["cwd"] == str(tmp_path / "run") and seen["ppn"] == 4
    assert words[:2] == [sys.executable, os.path.abspath(flushwrap.__file__)]
    args = flushwrap.parse(words[2:])
    assert args.output == str(tmp_path / "run" / "gs.bp") and args.every == FLUSH_SECONDS
    assert args.first == ["data.*"] and args.last == ["md.0", "mmd.0", "md.idx"]
    assert args.program == ["adios2_simulations_gray-scott", "settings.json"]
    assert json.loads((tmp_path / "run" / "settings.json").read_text())["output"] == str(tmp_path / "run" / "gs.bp")


def _supervisor_module():
    sys.path.insert(0, str(REPO_PACKAGES_DIR / "pvserver"))
    try:
        import supervisor
    finally:
        sys.path.pop(0)
    return supervisor


def test_supervisor_transport_classifier_is_the_contract_copy():
    from clio_jarvis._contract import transport

    supervisor = _supervisor_module()
    for name in ("UCX_WARN", "LIBFABRIC_WARN", "OPENFABRICS_SKIP", "UNAVAILABLE", "BARE_UNAVAILABLE", "OFFERED"):
        ours, theirs = getattr(supervisor, name), getattr(transport, name)
        assert (ours.pattern, ours.flags) == (theirs.pattern, theirs.flags), name
    assert supervisor.TRANSPORT_SETTINGS == transport.SETTINGS
    assert (supervisor.TRANSPORT_NOTE, supervisor.TRANSPORT_NOTE_EARLY) == (transport.NOTE, transport.NOTE_EARLY)
    lines = [UCX_SKIP, "libfabric:4242:1763766000::verbs:fabric:vrb_get_info():123<warn> no RDMA devices found",
             "[comp-05:4242:0] ucp_ep.c:1500 UCX  ERROR  failed to connect: Connection refused", "Waiting for client..."]
    for line in lines:
        assert supervisor.transport_library(line) == transport.library(line)
    copy, source = supervisor.TransportNotes(), transport.TransportNotes()
    for line in lines:
        assert copy.observe(line) == source.observe(line)
    env = {"UCX_TLS": "tcp", "PATH": "/usr/bin"}
    assert copy.report(env) == source.report(env) and copy.report(env, progressed=False) == source.report(env, progressed=False)


def test_supervisor_keeps_transport_warnings_out_of_the_evidence(tmp_path):
    import io

    supervisor = _supervisor_module()
    watcher = supervisor.Supervisor(supervisor.parse(["--state-dir", str(tmp_path)]))
    watcher.stdout_open = False
    refused = "[comp-05:4242:0] ucp_ep.c:1500 UCX  ERROR  failed to connect: Connection refused"
    warning = UCX_SKIP.replace("network device", "connection refused on network device")
    log = io.StringIO()
    watcher.forward(io.StringIO("\n".join([UCX_SKIP, warning, "Waiting for client..."]) + "\n"), log)
    assert UCX_SKIP in log.getvalue()
    assert list(watcher.recent) == ["Waiting for client..."] and watcher.failure is None
    early = watcher.diagnosis("exited_before_ready")
    assert early["evidence"] == ["Waiting for client..."]
    assert early["transport"]["unavailable"] == ["mlx5_0:1"] and early["transport"]["note"] == supervisor.TRANSPORT_NOTE_EARLY
    watcher.forward(io.StringIO(refused + "\n"), log)
    assert watcher.failure[0] == "mpi_network_unreachable" and watcher.recent[-1] == refused

    clean = supervisor.Supervisor(supervisor.parse(["--state-dir", str(tmp_path)]))
    assert "transport" not in clean.diagnosis("start_timeout")


def test_pvserver_start_error_reports_a_fallback_as_information(tmp_path):
    from clio_jarvis.pvserver.pkg import Pvserver

    supervisor = _supervisor_module()
    notes = supervisor.TransportNotes()
    notes.observe(UCX_SKIP)
    state = tmp_path / "state"
    state.mkdir()
    (state / "pvserver.log").write_text("Waiting for client...\n")
    record = {"reason": "start_failed", "cause": {"code": "start_timeout", "message": "rank 0 did not listen"},
              "transport": notes.report({}, progressed=False)}
    package = Pvserver.__new__(Pvserver)
    package._read_json = lambda name: record
    package._state = lambda name: state / name
    text = package._start_failure()
    assert "network info, not the cause: ucx skipped mlx5_0:1 and used tcp" in text
    assert text.index("network info") < text.index("log tail")


def test_flushwrap_leader_and_order(tmp_path, monkeypatch):
    from clio_jarvis._contract import flushwrap

    assert flushwrap.is_node_leader({})
    assert flushwrap.is_node_leader({"OMPI_COMM_WORLD_LOCAL_RANK": "0"})
    assert not flushwrap.is_node_leader({"MPI_LOCALRANKID": "3"})
    assert not flushwrap.is_node_leader({"SLURM_LOCALID": "1"})
    for name in ("md.idx", "data.1", "md.0", "data.0", "profiling.json"):
        (tmp_path / name).write_bytes(b"x")
    synced = []
    real_open = os.open
    names = {}

    def tracking_open(path, flags, *args):
        fd = real_open(path, flags, *args)
        names[fd] = os.path.basename(path)
        return fd

    monkeypatch.setattr(flushwrap.os, "open", tracking_open)
    monkeypatch.setattr(flushwrap.os, "fsync", lambda fd: synced.append(names[fd]))
    assert flushwrap.flush_once(str(tmp_path), ["data.*"], ["md.0", "mmd.0", "md.idx"]) == 4
    assert synced == ["data.0", "data.1", "md.0", "md.idx"]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="the wrapper forks, as on the compute nodes")
def test_flushwrap_runs_the_program_as_the_rank(tmp_path):
    from clio_jarvis._contract import flushwrap

    output = tmp_path / "data.bp5"
    output.mkdir()
    (output / "md.idx").write_bytes(b"x")
    script = "import os, sys, time; print(os.getpid(), flush=True); time.sleep(0.5); sys.exit(3)"
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, flushwrap.__file__, "--output", str(output), "--every", "0.1", "--last", "md.idx", "--", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # The program's status is the rank's, and its output ends with it: the flusher holds no stream open.
    assert done.returncode == 3
    assert done.stdout.strip().isdigit()
    assert time.monotonic() - started < 10
    for _ in range(50):
        flushers = [p for p in os.listdir("/proc") if p.isdigit() and _cmdline(p).count(str(output))] if os.path.isdir("/proc") else []
        if not flushers:
            break
        time.sleep(0.1)
    assert not flushers, "the flusher outlived the program"


def _cmdline(pid: str) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return ""


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


def test_pvserver_launch_file_is_jarvis_launch_record():
    from clio_jarvis.pvserver.pkg import launch_document

    record = {
        "schema_version": "jarvis.mpi-launch.v1",
        "built_by": "jarvis_cd.shell.mpi_exec.OpenMpiExec",
        "command": "mpiexec -n 2 pvserver --server-port=@PORT@",
        "nprocs": 2,
        "advisories": [{"code": "site_fact_uncertain", "reason_codes": ["ucx_device_unusable"]}],
        "launch_json": "/x/provenance/launch-001/launch.json",
    }

    class Info:
        env = {"OMPI_MCA_btl": "self,tcp"}

    class Launch:
        exec_info = Info()

        def launch_record(self):
            return dict(record)

        def get_cmd(self):
            return "unused"

    document = launch_document(Launch())
    assert document == {**record, "env": {"OMPI_MCA_btl": "self,tcp"}}

    class Old(Launch):
        launch_record = None

    with pytest.raises(RuntimeError, match="launch_record"):
        launch_document(Old())


# -- package helpers --------------------------------------------------------------------


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


@pytest.mark.skipif(not Path("/proc/net/tcp").exists(), reason="needs Linux /proc/net/tcp")
def test_supervisor_restarts_a_crashed_server_within_its_budget(tmp_path):
    """A pvserver that segfaults starts again on the same port; past the budget a crash ends it."""
    fake = tmp_path / "fake-pvserver"
    counter = tmp_path / "launches"
    fake.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import os, pathlib, signal, socket, sys, time
            counter = pathlib.Path({str(counter)!r})
            n = int(counter.read_text()) + 1 if counter.exists() else 1
            counter.write_text(str(n))
            port = int(next(a for a in sys.argv if a.startswith("--server-port=")).split("=")[1])
            s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port)); s.listen(1)
            print("Accepting connection(s): 127.0.0.1:%d" % port, flush=True)
            time.sleep(1)
            if n == 1:
                print("vtkPolyData::ComputeCellsBounds", flush=True)
                os.kill(os.getpid(), signal.SIGSEGV)
            time.sleep(600)
            """
        )
    )
    fake.chmod(0o755)
    state = tmp_path / "state"
    supervisor = subprocess.Popen(
        [sys.executable, str(REPO_PACKAGES_DIR / "pvserver" / "supervisor.py"),
         "--state-dir", str(state), "--executable", str(fake), "--grace", "5", "--crash-restarts", "1"],
        stdout=subprocess.PIPE, text=True,
    )
    lines = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not any(l.startswith("clio-pvserver restarted") for l in lines):
        lines.append(supervisor.stdout.readline().strip())
    assert "clio-pvserver crashed signal_SIGSEGV restarting crashes=1" in lines, lines
    ready = yaml.safe_load((state / "pvserver-ready.json").read_text())
    port = ready["port"]
    assert ready["crashes"] == 1 and ready["restarts"] == 0 and supervisor.poll() is None
    assert ready["last_crash"]["signal"] == "SIGSEGV"
    assert "vtkPolyData::ComputeCellsBounds" in ready["last_crash"]["evidence"]
    # Launch 2 serves; a second crash is past the budget and ends the service.
    os.kill(ready["server_pid"], signal.SIGSEGV)
    assert supervisor.wait(timeout=60) != 0
    record = yaml.safe_load((state / "pvserver-stop.json").read_text())
    assert record["reason"] == "server-exited" and record["port"] == port
    assert [c["signal"] for c in record["crashes"]] == ["SIGSEGV", "SIGSEGV"]
    assert counter.read_text() == "2"


# -- pvserver linger after the solver ---------------------------------------------------


def test_pvserver_linger_is_a_service_setting_with_a_default():
    contract = load_contract(REPO_PACKAGES_DIR / "pvserver")
    names = {entry["name"] for entry in contract.menu}
    base = {k: v for k, v in contract.resolve_config({}, {**CONFIG, "pkg_dir": str(contract.pkg_dir)}).items() if k in names}
    assert base["linger"] == 180
    contract.check_parameters(base)
    assert contract.service(base)["linger"] == 180
    contract.check_parameters({**base, "linger": 0})
    contract.check_parameters({**base, "linger": 10 * 365 * 86400})
    with pytest.raises(ValueError, match="linger"):
        contract.check_parameters({**base, "linger": -1})


def test_scheduler_durations():
    supervisor = _supervisor_module()
    parse = supervisor.parse_scheduler_duration
    assert parse("1-02:03:04") == 86400 + 2 * 3600 + 3 * 60 + 4
    assert parse("02:03:04") == 2 * 3600 + 3 * 60 + 4
    assert parse("3:04") == 3 * 60 + 4
    assert parse("7") == 7 * 60
    assert parse("2-05") == 2 * 86400 + 5 * 3600
    assert parse("  0:59 \n") == 59
    for text in ("UNLIMITED", "NOT_SET", "INVALID", "", "1:2:3:4", "a-1:00"):
        assert parse(text) is None


def test_linger_flags_and_job_time_left():
    supervisor = _supervisor_module()
    args = supervisor.parse(["--state-dir", "x"])
    assert args.linger == 0 and args.walltime is None and not args.job_id and args.job_end is None
    assert supervisor.job_time_left(args, 10) == (None, None)
    with pytest.raises(SystemExit):
        supervisor.parse(["--state-dir", "x", "--linger", "-1"])
    with pytest.raises(SystemExit):
        supervisor.parse(["--state-dir", "x", "--walltime", "0"])
    args = supervisor.parse(["--state-dir", "x", "--linger", "600", "--walltime", "1000"])
    assert supervisor.job_time_left(args, 100) == (900, "walltime")
    args = supervisor.parse(["--state-dir", "x", "--job-end", str(time.time() + 500), "--walltime", "9"])
    left, source = supervisor.job_time_left(args, 0)
    assert source == "scheduler end time" and 490 <= left <= 500


def test_linger_keeps_clear_of_the_job_end(tmp_path):
    supervisor = _supervisor_module()
    args = supervisor.parse(["--state-dir", str(tmp_path), "--linger", "600", "--walltime", "100000", "--grace", "15"])
    unbounded = supervisor.Supervisor(args)
    unbounded.begin_linger(0)
    assert unbounded.linger["seconds"] == 600 and unbounded.linger["capped_by"] is None
    args = supervisor.parse(["--state-dir", str(tmp_path), "--linger", "600", "--walltime", "400", "--grace", "15"])
    capped = supervisor.Supervisor(args)
    end = capped.begin_linger(100)
    room = 300 - 15 - supervisor.LINGER_MARGIN_SECONDS
    assert capped.linger["seconds"] == room and capped.linger["capped_by"] == "walltime"
    assert capped.linger["requested_seconds"] == 600 and capped.linger["job_seconds_left"] == 300
    assert end - time.monotonic() <= room
    capped.end_linger("signal")
    capped.end_linger("elapsed")
    assert capped.linger["ended"] == "signal" and capped.linger["ended_at"]
    args = supervisor.parse(["--state-dir", str(tmp_path), "--linger", "600", "--walltime", "50"])
    assert supervisor.Supervisor(args).begin_linger(40) <= time.monotonic() + 0.5


def test_pvserver_lingers_only_after_steps_returned():
    from jarvis_cd.core.pkg import Application, Service

    from clio_jarvis.pvserver.pkg import Pvserver

    def package(started, linger=600, holding=True):
        instance = Pvserver.__new__(Pvserver)
        instance.config = {"linger": linger}
        instance._clio_holding = holding

        class Pipeline:
            _started_instances = []

        instance.pipeline = Pipeline()
        instance.pipeline._started_instances = [instance if item == "self" else item for item in started]
        return instance

    step = Application.__new__(Application)
    other_service = Service.__new__(Service)
    assert package(["self", step])._lingers()
    assert not package(["self"])._lingers()
    assert not package(["self", other_service])._lingers()
    assert not package(["self", step], linger=0)._lingers()
    assert not package(["self", step], holding=False)._lingers()
    failing = package(["self", step])
    try:
        raise RuntimeError("a step failed")
    except RuntimeError:
        assert not failing._lingers()


def test_pvserver_linger_arguments(monkeypatch):
    from clio_jarvis.pvserver.pkg import Pvserver

    instance = Pvserver.__new__(Pvserver)
    instance.mod_env = {}
    instance.config = {"linger": 0, "walltime": 3600}
    assert instance._linger_arguments() == []
    monkeypatch.delenv("SLURM_JOB_END_TIME", raising=False)
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    instance.config = {"linger": 120, "walltime": 3600}
    assert instance._linger_arguments() == ["--linger", "120", "--walltime", "3600", "--job-id", "12345"]
    monkeypatch.delenv("SLURM_JOB_ID")
    instance.mod_env = {"SLURM_JOB_END_TIME": "1900000000"}
    instance.config = {"linger": 120, "walltime": None}
    assert instance._linger_arguments() == ["--linger", "120", "--job-end", "1900000000"]


@pytest.mark.skipif(not Path("/proc/net/tcp").exists(), reason="needs Linux /proc/net/tcp")
@pytest.mark.parametrize("ending", ["elapsed", "signal"])
def test_supervisor_lingers_on_sigusr1(tmp_path, ending):
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
    linger = 4 if ending == "elapsed" else 600
    supervisor = subprocess.Popen(
        [sys.executable, str(REPO_PACKAGES_DIR / "pvserver" / "supervisor.py"), "--state-dir", str(state),
         "--executable", str(fake), "--grace", "5", "--persist", "--linger", str(linger)],
        stdout=subprocess.PIPE, text=True,
    )
    lines = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (lines and lines[-1].startswith("clio-pvserver ready")):
        lines.append(supervisor.stdout.readline().strip())
    ready = json.loads((state / "pvserver-ready.json").read_text())
    assert ready["linger"] == linger and ready["lingering"] is False
    began = time.monotonic()
    supervisor.send_signal(signal.SIGUSR1)
    while time.monotonic() < deadline and not lines[-1].startswith("clio-pvserver lingering"):
        lines.append(supervisor.stdout.readline().strip())
    assert lines[-1].startswith(f"clio-pvserver lingering seconds={linger} until="), lines
    ready = json.loads((state / "pvserver-ready.json").read_text())
    assert ready["lingering"] is True and ready["linger_seconds"] == linger and ready["linger_until"]
    assert supervisor.poll() is None
    if ending == "signal":
        supervisor.send_signal(signal.SIGTERM)
    assert supervisor.wait(timeout=30) == 0
    took = time.monotonic() - began
    record = json.loads((state / "pvserver-stop.json").read_text())
    assert record["linger"]["seconds"] == linger and record["linger"]["ended"] == ending
    if ending == "elapsed":
        assert record["reason"] == "linger-ended" and took >= linger - 0.5
    else:
        assert record["reason"] == "signal" and took < 20


def _with_install(package: str, tmp_path: Path, monkeypatch, entries: list[dict]) -> Path:
    """A copy of the package whose menu gives these install entries instead of its own."""
    pkg_dir = _copy(package, tmp_path)
    class_name, menu = spec.read_menu(REPO_PACKAGES_DIR / package)
    menu = [entry for entry in menu if entry["name"] not in spec.INSTALL_ENTRIES] + entries
    monkeypatch.setattr(spec, "read_menu", lambda _pkg_dir: (class_name, menu))
    return pkg_dir


def _entries(method: str, query: str, repos: list | None = None, channels: list | None = None) -> list[dict]:
    entries = [
        {"name": "install_method", "msg": "m", "type": "str", "default": method},
        {"name": "install_query", "msg": "q", "type": "str", "default": query},
    ]
    if repos is not None:
        entries.append({"name": "install_repos", "msg": "r", "type": "list", "default": repos})
    if channels is not None:
        entries.append({"name": "install_channels", "msg": "c", "type": "list", "default": channels})
    return entries


def _methods(options: list[dict]) -> list[dict]:
    return [{"name": "install_methods", "msg": "o", "type": "list", "default": options}]


@pytest.mark.parametrize("package", PACKAGES)
def test_the_menu_is_the_install(package):
    contract = load_contract(REPO_PACKAGES_DIR / package)
    defaults = contract.defaults()
    described = contract.describe({}, {**CONFIG, "pkg_dir": str(contract.pkg_dir)})["install"]
    assert described["method"] == defaults["install_method"] == "spack"
    assert described["query"] == defaults["install_query"]
    assert described["repos"] == defaults["install_repos"]
    assert "alternatives" not in described


def test_an_install_part_in_the_contract_is_refused(tmp_path):
    pkg_dir = _copy("gray_scott", tmp_path)
    path = pkg_dir / "contract.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["install"] = {"method": "spack", "query": "gray-scott", "repos": []}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(ContractError, match="pkg.py's menu") as caught:
        load_contract(pkg_dir)
    assert caught.value.part == "install"


def test_install_methods_list_the_alternatives(tmp_path, monkeypatch):
    spack_option = {"method": "spack", "query": "gray-scott@2.10.2 ^adios2+mpi", "repos": ["../_spack"]}
    image = {"method": "container", "query": "ghcr.io/example/gray-scott:2.10.2"}
    contract = load_contract(_with_install("gray_scott", tmp_path, monkeypatch, _methods([spack_option, image])))
    described = contract.describe({}, {**CONFIG, "pkg_dir": str(contract.pkg_dir)})["install"]
    assert described == {**spack_option, "alternatives": [image]}
    assert (contract.pkg_dir / described["repos"][0] / "repo.yaml").is_file()


def test_conda_and_pip_alternatives_for_a_non_mpi_code(tmp_path, monkeypatch):
    options = [
        {"method": "spack", "query": "paraview@5.13.1 +python ~qt", "repos": []},
        {"method": "conda", "query": "paraview=5.13.1", "channels": ["conda-forge"]},
        {"method": "pip", "query": "rich==13.7.1"},
    ]
    contract = load_contract(_with_install("pvserver", tmp_path, monkeypatch, _methods(options)))
    assert contract.install_options() == options


@pytest.mark.parametrize("method", ["pip", "conda"])
def test_package_managers_never_install_an_mpi_code(method, tmp_path, monkeypatch):
    pkg_dir = _with_install("gray_scott", tmp_path, monkeypatch, _entries(method, "gray-scott"))
    with pytest.raises(ContractError, match="site's MPI") as caught:
        load_contract(pkg_dir)
    assert caught.value.part == "install"


@pytest.mark.parametrize("query", ["mpi4py numpy", "conda-forge::openmpi", "mpich>=4"])
def test_package_managers_never_name_an_mpi(query, tmp_path, monkeypatch):
    options = [{"method": "spack", "query": "paraview@5.13.1", "repos": []}, {"method": "conda", "query": query}]
    with pytest.raises(ContractError, match="names the MPI") as caught:
        load_contract(_with_install("pvserver", tmp_path, monkeypatch, _methods(options)))
    assert caught.value.part == "install"


@pytest.mark.parametrize(
    "entries",
    [
        _entries("spack", ""),
        _entries("apt", "paraview"),
        _entries("spack", "paraview@5.13.1 ^openmpi", repos=[]),
        _entries("spack", "paraview@5.13.1", repos=["/abs/repo"]),
        _entries("spack", "paraview@5.13.1", repos=["../gray_scott"]),
        _methods([{"method": "spack", "query": "paraview@5.13.1"}, {"method": "spack", "query": "paraview@5.12"}]),
    ],
)
def test_malformed_install_is_refused(entries, tmp_path, monkeypatch):
    with pytest.raises(ContractError) as caught:
        load_contract(_with_install("pvserver", tmp_path, monkeypatch, entries))
    assert caught.value.part == "install"
