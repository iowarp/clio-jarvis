"""ADIOS2 Gray-Scott as a plain JARVIS application.

The run directory gets ``settings.json`` and ``adios2.xml``. The solver runs
under MPI there and writes ``gs.bp`` with a ``vtk.xml`` attribute, so ParaView's
ADIOS2VTXReader opens it as image data and sees each new step as it lands. The
software comes from the ``clio.gray-scott`` Spack recipe in the repository's
``_spack/`` directory, whose ``+clio`` variant adds that attribute and the
simulation time; ``jarvis ppl install`` builds it from the menu's install
entries. contract.yaml declares what relay checks before it submits.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo

#: Seconds between syncs of gs.bp on each node with solver ranks.
FLUSH_SECONDS = 1.0

CONFIG_DIR = Path(__file__).resolve().parent / "config"


class GrayScott(Application):
    """Gray-Scott reaction-diffusion with a growing BP5 output."""

    def _configure_menu(self) -> list[dict[str, Any]]:
        return [
            {"name": "install_method", "msg": "Installer for Gray-Scott", "type": str, "default": "spack"},
            {
                "name": "install_query",
                "msg": "Spack spec; MPI stays the mpi virtual, so the site's MPI is reused",
                "type": str,
                "default": "gray-scott@2.10.2 +clio ^adios2+mpi",
            },
            {
                "name": "install_repos",
                "msg": "Spack repositories with the clio.gray-scott recipe, relative to the package directory",
                "type": list,
                "default": ["../_spack"],
            },
            {"name": "nprocs", "msg": "MPI processes; they split the grid into a 3D block grid (see the contract's sizing)", "type": int, "default": 4},
            {
                "name": "ppn",
                "msg": "MPI processes per node; empty means cores_per_node, else JARVIS's MPI launch decides",
                "type": int,
                "default": None,
                "required": False,
            },
            {
                "name": "cores_per_node",
                "msg": "Physical cores per node of the target partition (relay's describe reports them); fills an empty ppn",
                "type": int,
                "default": None,
                "required": False,
            },
            {"name": "L", "msg": "Grid cells per side (the grid is L x L x L)", "type": int, "default": 128},
            {"name": "steps", "msg": "Simulation steps", "type": int, "default": 15000},
            {"name": "plotgap", "msg": "Simulation steps between two output steps", "type": int, "default": 150},
            {"name": "F", "msg": "Feed rate", "type": float, "default": 0.01},
            {"name": "k", "msg": "Kill rate", "type": float, "default": 0.05},
            {"name": "Du", "msg": "Diffusion rate of U", "type": float, "default": 0.2},
            {"name": "Dv", "msg": "Diffusion rate of V", "type": float, "default": 0.1},
            {"name": "dt", "msg": "Time step", "type": float, "default": 2.0},
            {"name": "noise", "msg": "Amplitude of the initial noise", "type": float, "default": 1e-7},
            {"name": "run_dir", "msg": "Run directory; empty means {shared_dir}/run", "type": str, "default": ""},
            {
                "name": "checkpoint_freq",
                "msg": "Simulation steps between checkpoints written to ckpt.bp; 0 means none",
                "type": int,
                "default": 0,
            },
            {"name": "walltime", "msg": "Wall-time limit in seconds; absent means no limit", "type": int, "default": None, "required": False},
            {"name": "queue", "msg": "Batch queue; empty means the site default", "type": str, "default": ""},
        ]

    def check_config(self, config: dict[str, Any]) -> None:
        """Refuse a run the solver would reject, before any job starts."""
        nprocs, size, steps, plotgap = (int(config.get(name) or 0) for name in ("nprocs", "L", "steps", "plotgap"))
        if nprocs < 1:
            raise ValueError("gray_scott: nprocs must be at least 1")
        if size < 8:
            raise ValueError("gray_scott: L must be at least 8")
        if steps < 1:
            raise ValueError("gray_scott: steps must be at least 1")
        if not 1 <= plotgap <= steps:
            raise ValueError("gray_scott: plotgap must be between 1 and steps")

    @property
    def _settings(self) -> dict[str, Any]:
        return self.config

    def run_dir(self) -> Path:
        return Path(self._settings.get("run_dir") or Path(str(self.shared_dir)) / "run")

    def ppn(self) -> int | None:
        for name in ("ppn", "cores_per_node"):
            value = self._settings.get(name)
            if value not in (None, ""):
                return int(value)
        return None

    def _write_inputs(self, run_dir: Path) -> None:
        config = self._settings
        run_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CONFIG_DIR / "adios2.xml", run_dir / "adios2.xml")
        settings = {
            "L": config["L"],
            "Du": config["Du"],
            "Dv": config["Dv"],
            "F": config["F"],
            "k": config["k"],
            "dt": config["dt"],
            "plotgap": config["plotgap"],
            "steps": config["steps"],
            "noise": config["noise"],
            "output": str(run_dir / "gs.bp"),
            "checkpoint": bool(config.get("checkpoint_freq")),
            "checkpoint_freq": int(config.get("checkpoint_freq") or 0) or 100000000,
            "checkpoint_output": str(run_dir / "ckpt.bp"),
            "restart": False,
            "restart_input": str(run_dir / "ckpt.bp"),
            "adios_config": str(run_dir / "adios2.xml"),
            "adios_span": False,
            "adios_memory_selection": False,
            "mesh_type": "image",
        }
        (run_dir / "settings.json").write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    def start(self) -> None:
        from clio_jarvis._contract import flushwrap
        from clio_jarvis._contract.files import revealing

        run_dir = self.run_dir()
        # The progress and artifact providers read the run directory from the config.
        self._settings["run_dir"] = str(run_dir)
        output = run_dir / "gs.bp"
        # A rerun starts a fresh series; a viewer must not see the last run's steps.
        shutil.rmtree(output, ignore_errors=True)
        shutil.rmtree(run_dir / "ckpt.bp", ignore_errors=True)
        self._write_inputs(run_dir)
        # ADIOS2 does not sync: without a flusher where the ranks write, a viewer
        # on another node sees gs.bp jump by the page cache's writeback interval.
        command = flushwrap.bp5_command(output, ["adios2_simulations_gray-scott", "settings.json"], FLUSH_SECONDS)
        with revealing(output):
            result = Exec(
                command,
                MpiExecInfo(
                    nprocs=int(self._settings["nprocs"]),
                    ppn=self.ppn(),
                    hostfile=self.hostfile,
                    env=self.mod_env,
                    cwd=str(run_dir),
                    line_callback=self.runtime_line_callback(),
                ),
            ).run()
        failures = {host: code for host, code in dict(result.exit_code).items() if code != 0}
        if failures:
            raise RuntimeError(f"gray_scott failed: {failures}")

    def stop(self) -> None:
        """Gray-Scott runs to completion."""

    def clean(self) -> None:
        shutil.rmtree(self.run_dir() / "gs.bp", ignore_errors=True)
