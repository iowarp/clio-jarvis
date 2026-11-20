#!/usr/bin/env python3
"""Run one pvserver for clio-jarvis and report its life in the state directory.

The supervisor picks the port (0 asks for a free one) and starts pvserver bound
to 127.0.0.1. It waits until the port is listening, then prints
``clio-pvserver ready port=N`` and writes ``pvserver-ready.json``. It never
connects to check the port, because pvserver serves the first client that
connects.

pvserver serves one client session and exits when that client disconnects
(this package does not use ``--multi-clients``). With ``--persist`` the
supervisor then starts a new pvserver on the same port and prints
``clio-pvserver restarted port=N``, so the service outlives its clients.

On SIGTERM or SIGINT, at the lifetime limit, or when pvserver exits without
being restarted, it stops the server and writes ``pvserver-stop.json``
(clio.service-stop.v1).

With more than one rank it starts pvserver through the mpiexec of the MPI
JARVIS chose (JARVIS_MPI_PREFIX, JARVIS_MPI_TYPE), over ``--hosts`` with this
node first, so rank 0 listens where the supervisor checks.

Only the standard library is used, so any Python 3.9+ can run it.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

READY_FILE = "pvserver-ready.json"
STOP_FILE = "pvserver-stop.json"
PID_FILE = "pvserver-supervisor.pid"
LOG_FILE = "pvserver.log"
READY_SCHEMA = "clio.service-ready.v1"
STOP_SCHEMA = "clio.service-stop.v1"
BIND = "127.0.0.1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, document: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((BIND, 0))
        return sock.getsockname()[1]


def listening(port: int) -> bool | None:
    """True when a socket listens on ``port``. None when /proc/net/tcp is not there."""
    seen_table = False
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as handle:
                lines = handle.read().splitlines()[1:]
        except OSError:
            continue
        seen_table = True
        for line in lines:
            parts = line.split()
            if len(parts) > 3 and parts[3] == "0A" and int(parts[1].rsplit(":", 1)[1], 16) == port:
                return True
    return False if seen_table else None


def server_command(args: argparse.Namespace, port: int) -> list[str]:
    command = [
        args.executable,
        "--force-offscreen-rendering",
        f"--bind-address={BIND}",
        f"--server-port={port}",
    ]
    if args.connect_id:
        command.append(f"--connect-id={args.connect_id}")
    if args.nprocs > 1:
        command = mpi_launcher(args) + command
    return command


def ordered_hosts(hosts: list[str]) -> list[str]:
    """``hosts`` with this node first, so rank 0 listens where the supervisor checks."""
    names = {socket.gethostname(), socket.gethostname().split(".")[0], socket.getfqdn(), "localhost"}
    for index, host in enumerate(hosts):
        if host in names or host.split(".")[0] in names:
            return [host] + hosts[:index] + hosts[index + 1:]
    raise ValueError(f"this node ({socket.gethostname()}) is not among the hosts {hosts}")


def mpi_launcher(args: argparse.Namespace) -> list[str]:
    """mpiexec of the MPI ParaView was built against, over ``--hosts`` or this node.

    JARVIS's Spack installer exports the chosen MPI as JARVIS_MPI_PREFIX and
    JARVIS_MPI_TYPE; without them mpiexec is taken from PATH.
    """
    nprocs = str(args.nprocs)
    prefix = os.environ.get("JARVIS_MPI_PREFIX", "")
    launcher = os.path.join(prefix, "bin", "mpiexec") if prefix else ""
    if not os.path.isfile(launcher):
        launcher = "mpiexec"
    kind = os.environ.get("JARVIS_MPI_TYPE", "").strip().lower()
    hosts = args.hosts.split(",") if args.hosts else []
    ppn = args.ppn or -(-args.nprocs // max(len(hosts), 1))
    if kind == "openmpi":
        if not hosts:
            return [launcher, "--host", f"localhost:{nprocs}", "-n", nprocs]
        command = [launcher, "--host", ",".join(f"{host}:{ppn}" for host in hosts), "-x", "PATH", "-x", "LD_LIBRARY_PATH"]
        if prefix:
            command += ["--prefix", prefix]
        return command + ["-n", nprocs]
    if kind in ("mpich", "intel_mpi", "cray_mpich") or not kind:
        if not hosts:
            return [launcher, "-launcher", "fork", "-n", nprocs] if kind else [launcher, "-n", nprocs]
        return [launcher, "-hosts", ",".join(hosts), "-ppn", str(ppn), "-n", nprocs]
    return [launcher, "-n", nprocs]


class Supervisor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.state_dir = Path(args.state_dir)
        self.stop_reason: str | None = None
        self.stop_signal: str | None = None
        self.stdout_open = True
        self.accepting = threading.Event()
        self.client_seen = threading.Event()
        self.wake = threading.Event()

    def say(self, text: str) -> None:
        if not self.stdout_open:
            return
        try:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            # The launcher went away; keep serving and keep logging to the file.
            self.stdout_open = False

    def on_signal(self, number: int, _frame) -> None:
        if self.stop_reason is None:
            self.stop_reason = "signal"
            self.stop_signal = signal.Signals(number).name
        self.wake.set()

    def forward(self, pipe, log) -> None:
        for line in iter(pipe.readline, ""):
            log.write(line)
            log.flush()
            if "Accepting connection" in line:
                self.accepting.set()
            if "Client connected" in line:
                self.client_seen.set()
            self.say(line.rstrip("\n"))
        pipe.close()

    def launch(self, port: int, log) -> tuple[subprocess.Popen, threading.Thread]:
        command = server_command(self.args, port)
        log.write(f"# {now()} {' '.join(command)}\n")
        log.flush()
        self.accepting.clear()
        self.client_seen.clear()
        server = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        reader = threading.Thread(target=self.forward, args=(server.stdout, log), daemon=True)
        reader.start()
        return server, reader

    def wait_listening(self, server: subprocess.Popen, port: int) -> bool:
        deadline = time.monotonic() + self.args.start_timeout
        while server.poll() is None and self.stop_reason is None and time.monotonic() < deadline:
            state = listening(port)
            if state or (state is None and self.accepting.is_set()):
                return True
            self.wake.wait(0.2)
        return False

    def run(self) -> int:
        args = self.args
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for name in (READY_FILE, STOP_FILE):
            (self.state_dir / name).unlink(missing_ok=True)
        (self.state_dir / PID_FILE).write_text(f"{os.getpid()}\n", encoding="ascii")
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, self.on_signal)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        port = args.port or free_port()
        started = now()
        log = open(self.state_dir / LOG_FILE, "a", encoding="utf-8")
        server, reader = self.launch(port, log)

        ready_at = None
        restarts = 0
        if self.wait_listening(server, port):
            ready_at = now()
        elif self.stop_reason is None:
            self.stop_reason = "failed-to-start"

        if ready_at is not None:
            ready = {
                "schema": READY_SCHEMA,
                "host": socket.gethostname(),
                "bind": BIND,
                "port": port,
                "url": f"cs://{BIND}:{port}",
                "connect_id": args.connect_id or None,
                "persist": bool(args.persist),
                "nprocs": args.nprocs,
                "supervisor_pid": os.getpid(),
                "server_pid": server.pid,
                "started_at": started,
                "ready_at": ready_at,
                "max_lifetime": args.max_lifetime,
                "restarts": 0,
            }
            write_json(self.state_dir / READY_FILE, ready)
            self.say(f"clio-pvserver ready port={port} url=cs://{BIND}:{port}")
            end = time.monotonic() + args.max_lifetime if args.max_lifetime is not None else None
            while self.stop_reason is None:
                if end is not None and time.monotonic() > end:
                    self.stop_reason = "max-lifetime"
                    break
                code = server.poll()
                if code is None:
                    self.wake.wait(0.5)
                    continue
                reader.join(timeout=5)
                # A clean exit after a client session is pvserver's normal end of
                # session; anything else is a real failure.
                if not (args.persist and code == 0 and self.client_seen.is_set()):
                    self.stop_reason = "server-exited"
                    break
                restarts += 1
                server, reader = self.launch(port, log)
                if not self.wait_listening(server, port):
                    self.stop_reason = self.stop_reason or "failed-to-restart"
                    break
                ready.update(server_pid=server.pid, restarts=restarts, restarted_at=now())
                write_json(self.state_dir / READY_FILE, ready)
                self.say(f"clio-pvserver restarted port={port} restarts={restarts}")

        server_code = self.stop_server(server)
        reader.join(timeout=5)
        stopped = now()
        record = {
            "schema": STOP_SCHEMA,
            "reason": self.stop_reason,
            "signal": self.stop_signal,
            "host": socket.gethostname(),
            "port": port,
            "started_at": started,
            "ready_at": ready_at,
            "stopped_at": stopped,
            "restarts": restarts,
            "server_exit_code": server_code,
            "log": str(self.state_dir / LOG_FILE),
        }
        write_json(self.state_dir / STOP_FILE, record)
        log.write(f"# {stopped} stopped: {self.stop_reason}\n")
        log.close()
        (self.state_dir / PID_FILE).unlink(missing_ok=True)
        self.say(f"clio-pvserver stopped reason={self.stop_reason}")
        if self.stop_reason in ("signal", "max-lifetime"):
            return 0
        if self.stop_reason == "server-exited":
            return server_code if server_code else 0
        return 1

    def stop_server(self, server: subprocess.Popen) -> int | None:
        if server.poll() is None:
            try:
                os.killpg(server.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                server.wait(timeout=self.args.grace)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(server.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                server.wait()
        return server.returncode


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, not {value}")
    return value


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--executable", default="pvserver")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--nprocs", type=int, default=1)
    parser.add_argument("--ppn", type=int, default=0, help="ranks per host; 0 spreads nprocs evenly")
    parser.add_argument("--hosts", default="", help="comma-separated hosts for the ranks; empty means this node")
    parser.add_argument("--connect-id", type=int, default=0)
    parser.add_argument("--persist", action="store_true", help="start a new pvserver on the same port after each client session")
    parser.add_argument(
        "--max-lifetime",
        type=_positive_int,
        default=None,
        help="seconds before the server stops itself; omitted means it runs until stopped",
    )
    parser.add_argument("--start-timeout", type=int, default=120)
    parser.add_argument("--grace", type=int, default=20)
    args = parser.parse_args(argv)
    if args.hosts:
        try:
            args.hosts = ",".join(ordered_hosts([host for host in args.hosts.split(",") if host]))
        except ValueError as error:
            parser.error(str(error))
    return args


if __name__ == "__main__":
    sys.exit(Supervisor(parse(sys.argv[1:])).run())
