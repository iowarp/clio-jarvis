"""pvserver self-test: a ParaView client connects, renders remotely, and the server stops cleanly.

The generic runner already checked the ready event and the stop record of the
JARVIS run. This check starts the same supervisor again, connects pvpython
through 127.0.0.1, renders a sphere on the server, then sends SIGTERM.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

CLIENT = """
import sys
from paraview.simple import Connect, Disconnect, GetActiveViewOrCreate, Render, SaveScreenshot, Show, Sphere
port, png = int(sys.argv[1]), sys.argv[2]
connection = Connect("127.0.0.1", port)
assert connection is not None, "no connection"
view = GetActiveViewOrCreate("RenderView")
view.ViewSize = [400, 300]
Show(Sphere(ThetaResolution=32, PhiResolution=32), view)
Render(view)
SaveScreenshot(png, view)
print("PVCONNECT ok", connection.GetURI())
Disconnect()
"""


def _wait_restart(state: Path, supervisor: subprocess.Popen, restarts: int = 1, timeout: float = 60) -> bool:
    """Wait until the ready record counts ``restarts`` restarts."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and supervisor.poll() is None:
        try:
            ready = json.loads((state / "pvserver-ready.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ready = {}
        if int(ready.get("restarts") or 0) >= restarts:
            return True
        time.sleep(0.2)
    return False


def check(context: dict) -> None:
    check = context["check"]
    config = context["config"]
    pv_env = context["pv_env"] or config.get("env_script") or ""
    if not pv_env:
        check("pvpython connects and renders", True, "skipped: no ParaView environment script")
        return
    work = Path(context["work"])
    state = work / "pvserver-live"
    supervisor = subprocess.Popen(
        [
            sys.executable, str(HERE / "supervisor.py"),
            "--state-dir", str(state),
            "--executable", str(config.get("executable") or "pvserver"),
            "--env-script", str(config.get("env_script") or pv_env),
            "--nprocs", str(config.get("nprocs") or 1),
            "--persist",
            "--max-lifetime", "300",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    ready = None
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and supervisor.poll() is None:
        path = state / "pvserver-ready.json"
        if path.is_file():
            ready = json.loads(path.read_text(encoding="utf-8"))
            break
        time.sleep(0.2)
    if not check("supervisor reports ready", ready is not None, ready and ready.get("url")):
        supervisor.kill()
        return
    try:
        client = work / "pvconnect.py"
        client.write_text(CLIENT, encoding="utf-8")
        for session in (1, 2):
            if session == 2:
                restarted = _wait_restart(state, supervisor)
                if not check("a new pvserver listens on the same port after the client leaves", restarted, f"port {ready['port']}"):
                    break
            png = work / f"pvserver-sphere-{session}.png"
            result = subprocess.run(
                ["bash", "-c", 'source "$1" && shift && exec "$@"', "clio-pv", pv_env,
                 "pvpython", "--force-offscreen-rendering", str(client), str(ready["port"]), str(png)],
                capture_output=True, text=True, timeout=180,
            )
            (work / f"pvconnect-{session}.log").write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
            ok = result.returncode == 0 and "PVCONNECT ok" in result.stdout
            check(f"client {session}: pvpython connects and renders on the server", ok and png.is_file(), f"rc {result.returncode}, {png}")
        restarted = _wait_restart(state, supervisor, restarts=2)
        check("service still running after both clients left", restarted and supervisor.poll() is None, None)
    finally:
        supervisor.send_signal(signal.SIGTERM)
        try:
            code = supervisor.wait(timeout=60)
        except subprocess.TimeoutExpired:
            supervisor.kill()
            code = supervisor.wait()
    record_path = state / "pvserver-stop.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
    check("SIGTERM stops it with exit 0 and a stop record", code == 0 and record.get("reason") == "signal", f"rc {code}, {record.get('reason')}")
