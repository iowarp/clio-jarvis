"""ParaView's pvserver as a clio-jarvis service (RL40).

``start`` launches ``supervisor.py``, which runs pvserver bound to 127.0.0.1.
It returns once the port is listening, and the progress provider reports the
ready port. ``stop`` sends SIGTERM; the supervisor stops pvserver and writes
``pvserver-stop.json`` in the state directory. A client reaches the server
through an SSH tunnel to that port.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any

from jarvis_cd.shell import Exec, LocalExecInfo

from clio_jarvis._contract.base import ClioService

SUPERVISOR = Path(__file__).resolve().parent / "supervisor.py"
READY_FILE = "pvserver-ready.json"
STOP_FILE = "pvserver-stop.json"
PID_FILE = "pvserver-supervisor.pid"
LOG_FILE = "pvserver.log"
HOST = "localhost"


class Pvserver(ClioService):
    """A ParaView server for remote rendering of clio outputs."""

    clio_program = "pvserver"
    clio_probe_arguments = ("--version",)
    clio_capabilities = ("paraview_server",)

    _clio_service_exec: Exec | None = None

    def _configure_menu(self) -> list[dict[str, Any]]:
        return [
            {"name": "nprocs", "msg": "pvserver processes", "type": int, "default": 1},
            {"name": "ppn", "msg": "pvserver processes per node", "type": int, "default": 1},
            {"name": "port", "msg": "TCP port on 127.0.0.1; 0 picks a free port", "type": int, "default": 0},
            {
                "name": "connect_id",
                "msg": "Connect ID a client must present; 0 means none",
                "type": int,
                "default": 0,
            },
            {
                "name": "persist",
                "msg": "Start a new pvserver on the same port when a client disconnects (pvserver serves one session)",
                "type": bool,
                "default": True,
            },
            {
                "name": "hold",
                "msg": "Keep start running until the server stops, so a hand-run 'jarvis ppl run' serves until it is signalled",
                "type": bool,
                "default": False,
            },
            {
                "name": "env_script",
                "msg": "Shell script sourced before pvserver starts, such as ParaView's environment; empty means none",
                "type": str,
                "default": "",
            },
            {
                "name": "executable",
                "msg": "pvserver program, by name on PATH or by absolute path",
                "type": str,
                "default": "pvserver",
            },
            {
                "name": "max_lifetime",
                "msg": "Seconds before the server stops itself; absent means it runs until stopped",
                "type": int,
                "default": None,
                "required": False,
            },
            {"name": "start_timeout", "msg": "Seconds to wait for the port to open", "type": int, "default": 120},
            {
                "name": "state_dir",
                "msg": "Directory for the ready and stop records and the log; empty means {shared_dir}/pvserver",
                "type": str,
                "default": "",
            },
            {
                "name": "walltime",
                "msg": "Wall-time limit in seconds; absent means no limit",
                "type": int,
                "default": None,
                "required": False,
            },
            {"name": "queue", "msg": "Batch queue; empty means the site default", "type": str, "default": ""},
        ]

    def _clio_prepare(self, config: dict[str, Any]) -> None:
        Path(config["state_dir"]).mkdir(parents=True, exist_ok=True)

    def _clio_runtime_status(self):
        if self.config.get("env_script"):
            # pvserver needs its environment script to start; finding the script is the check.
            from jarvis_cd.deployment import RuntimeStatus

            if not os.path.isfile(self.config["env_script"]):
                return RuntimeStatus("unavailable", "software_not_found")
            return RuntimeStatus("ready", "software_found")
        return super()._clio_runtime_status()

    # -- files ---------------------------------------------------------------

    def _state(self, name: str) -> Path:
        self._clio_ensure_configured(prepare=False)
        return Path(self.config["state_dir"]) / name

    def _read_json(self, name: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._state(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _supervisor_pid(self) -> int | None:
        try:
            return int(self._state(PID_FILE).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    def _log_tail(self, lines: int = 20) -> str:
        try:
            return "\n".join(self._state(LOG_FILE).read_text(encoding="utf-8").splitlines()[-lines:])
        except OSError:
            return ""

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        self._clio_ensure_configured(prepare=True)
        config = self.config
        state_dir = Path(config["state_dir"])
        state_dir.mkdir(parents=True, exist_ok=True)
        for name in (READY_FILE, STOP_FILE):
            self._state(name).unlink(missing_ok=True)
        argv = [
            sys.executable,
            str(SUPERVISOR),
            "--state-dir", str(state_dir),
            "--executable", self.clio_executable(),
            "--env-script", str(config.get("env_script") or ""),
            "--port", str(int(config["port"])),
            "--nprocs", str(int(config["nprocs"])),
            "--connect-id", str(int(config["connect_id"])),
            "--start-timeout", str(int(config["start_timeout"])),
            "--grace", str(self._grace()),
        ]
        if config.get("max_lifetime") not in (None, ""):
            argv += ["--max-lifetime", str(int(config["max_lifetime"]))]
        if config.get("persist"):
            argv.append("--persist")
        self._clio_service_exec = Exec(
            shlex.join(argv),
            LocalExecInfo(
                env=self.mod_env,
                cwd=str(state_dir),
                exec_async=True,
                hide_output=False,
                line_callback=self.runtime_line_callback(),
            ),
        )
        self._clio_service_exec.run()

        deadline = time.monotonic() + int(config["start_timeout"]) + 15
        while True:
            if self._read_json(READY_FILE) is not None:
                if config.get("hold"):
                    self._hold()
                return
            process = self._clio_service_exec.processes.get(HOST)
            if process is None or process.poll() is not None or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        self._terminate(signal.SIGTERM)
        self._finish()
        raise RuntimeError(f"pvserver did not start; log tail:\n{self._log_tail()}")

    def _hold(self) -> None:
        """Serve until the supervisor ends; a signal to this process stops the server.

        ``jarvis ppl run`` calls ``start`` and then ``stop``. Holding here keeps
        the service up for as long as the run lasts, which is how a scheduler
        job or relay's launcher serves it. SIGTERM, SIGINT or SIGHUP to JARVIS
        is passed to the supervisor, which stops pvserver and writes the stop
        record; ``stop`` then finds the supervisor already finished.
        """
        service = self._clio_service_exec
        process = service.processes.get(HOST) if service is not None else None
        if process is None:
            return

        def forward(number, _frame):
            self._terminate(signal.SIGTERM)

        previous = {}
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                previous[number] = signal.signal(number, forward)
            except ValueError:  # not the main thread: the caller stops the service itself
                break
        try:
            while process.poll() is None:
                time.sleep(0.5)
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)

    def _grace(self) -> int:
        return int(self.clio_contract().data["service"]["stop"].get("grace_seconds", 30))

    def _terminate(self, number: int) -> None:
        pid = self._supervisor_pid()
        if pid is None:
            return
        try:
            os.kill(pid, number)
        except ProcessLookupError:
            pass

    def _finish(self) -> int | None:
        """Wait for the supervisor this instance started; this finalizes its progress."""
        service = self._clio_service_exec
        if service is None:
            return None
        process = service.processes.get(HOST)
        deadline = time.monotonic() + self._grace() + 10
        while process is not None and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        if process is not None and process.poll() is None:
            service.kill(HOST)
        code = service.wait(HOST)
        self._clio_service_exec = None
        return code

    def stop(self) -> None:
        self._terminate(signal.SIGTERM)
        if self._clio_service_exec is not None:
            code = self._finish()
            if code not in (0, None):
                raise RuntimeError(f"pvserver supervisor exited with status {code}")
            return
        # Another process started the server: wait for its stop record.
        deadline = time.monotonic() + self._grace() + 10
        while self._supervisor_pid() is not None and time.monotonic() < deadline:
            time.sleep(0.2)

    def kill(self) -> None:
        self._terminate(signal.SIGKILL)
        ready = self._read_json(READY_FILE) or {}
        if ready.get("server_pid"):
            try:
                os.killpg(int(ready["server_pid"]), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self._state(PID_FILE).unlink(missing_ok=True)

    def status(self) -> str:
        pid = self._supervisor_pid()
        if pid is None:
            return "stopped"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "stopped"
        ready = self._read_json(READY_FILE)
        return f"running on port {ready['port']}" if ready else "starting"

    def clean(self) -> None:
        if self._supervisor_pid() is None:
            shutil.rmtree(self.config["state_dir"], ignore_errors=True)
