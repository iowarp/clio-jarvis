"""ADIOS2 Gray-Scott as a clio-jarvis package (RL40).

The run directory gets ``settings.json`` and ``adios2.xml``. The solver runs
under MPI there and writes ``gs.bp`` with a ``vtk.xml`` attribute, so ParaView's
ADIOS2VTXReader opens it as image data and sees each new step as it lands.
"""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from typing import Any

from clio_jarvis._contract.base import ClioApplication

CONFIG_DIR = Path(__file__).resolve().parent / "config"


class GrayScott(ClioApplication):
    """Gray-Scott reaction-diffusion with a growing BP5 output."""

    clio_program = "adios2_simulations_gray-scott"
    clio_probe_arguments = None
    clio_capabilities = ("mpi_execution", "adios2_bp5", "gray_scott")

    def _configure_menu(self) -> list[dict[str, Any]]:
        return [
            {"name": "nprocs", "msg": "MPI processes", "type": int, "default": 4},
            {"name": "ppn", "msg": "MPI processes per node", "type": int, "default": 4},
            {"name": "L", "msg": "Grid cells per side (the grid is L x L x L)", "type": int, "default": 128},
            {"name": "steps", "msg": "Simulation steps", "type": int, "default": 15000},
            {"name": "plotgap", "msg": "Simulation steps between two output steps", "type": int, "default": 150},
            {"name": "F", "msg": "Feed rate", "type": float, "default": 0.01},
            {"name": "k", "msg": "Kill rate", "type": float, "default": 0.05},
            {"name": "Du", "msg": "Diffusion rate of U", "type": float, "default": 0.2},
            {"name": "Dv", "msg": "Diffusion rate of V", "type": float, "default": 0.1},
            {"name": "dt", "msg": "Time step", "type": float, "default": 2.0},
            {"name": "noise", "msg": "Amplitude of the initial noise", "type": float, "default": 1e-7},
            {
                "name": "run_dir",
                "msg": "Run directory; empty means {shared_dir}/run",
                "type": str,
                "default": "",
            },
            {
                "name": "executable",
                "msg": "Gray-Scott program, by name on PATH or by absolute path",
                "type": str,
                "default": "adios2_simulations_gray-scott",
            },
            {"name": "walltime", "msg": "Wall-time limit in seconds; absent means no limit", "type": int, "default": None, "required": False},
            {"name": "queue", "msg": "Batch queue; empty means the site default", "type": str, "default": ""},
        ]

    def _clio_prepare(self, config: dict[str, Any]) -> None:
        run_dir = Path(config["run_dir"])
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
            "checkpoint": False,
            "checkpoint_freq": 100000000,
            "checkpoint_output": str(run_dir / "ckpt.bp"),
            "restart": False,
            "restart_input": str(run_dir / "ckpt.bp"),
            "adios_config": str(run_dir / "adios2.xml"),
            "adios_span": False,
            "adios_memory_selection": False,
            "mesh_type": "image",
        }
        (run_dir / "settings.json").write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    def _output(self) -> Path:
        return Path(self.config["run_dir"]) / "gs.bp"

    def start(self) -> None:
        self._clio_ensure_configured(prepare=True)
        run_dir = Path(self.config["run_dir"])
        # A rerun starts a fresh series; a viewer must not see the last run's steps.
        shutil.rmtree(self._output(), ignore_errors=True)
        command = f"{shlex.quote(self.clio_executable())} settings.json"
        self._clio_mpi(command, cwd=str(run_dir))

    def stop(self) -> None:
        """Gray-Scott runs to completion."""

    def clean(self) -> None:
        self._clio_ensure_configured(prepare=False)
        shutil.rmtree(self._output(), ignore_errors=True)
