"""jarvis-cd base classes for clio-jarvis packages.

A package subclasses :class:`ClioApplication` or :class:`ClioService`, defines
``_configure_menu`` and ``start``, and may override ``_clio_prepare`` to write
its input files. The mixin does the RL40 parts that are the same everywhere:
resolve and check the parameters against ``contract.yaml``, fill the empty
ones, write ``clio-contract.json`` into the package's shared directory, and
describe the runtime to jarvis-cd's deployment contract.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

from jarvis_cd.core.pkg import Application, Service
from jarvis_cd.deployment import (
    ConfigurationCondition,
    ExecutionProfile,
    PackageDeploymentContract,
    ReadinessContract,
    RuntimeRequirement,
    RuntimeStatus,
    probe_program,
)
from jarvis_cd.shell import Exec, MpiExecInfo

from .spec import RESOLVED_SCHEMA, Contract, load_contract

#: Parameters the fill rule set on the last configure, so a re-configure refills them.
FILLED_KEY = "_clio_filled"
RECORD_NAME = "clio-contract.json"

# OpenMPI's mpiexec gets every variable as -x KEY="VALUE"; these would break that line.
_UNSAFE_ENV_VALUE = re.compile(r'["$`\\\n]')


def mpi_safe_env(env: Mapping[str, Any]) -> dict[str, str]:
    """The environment without exported shell functions or values mpiexec cannot quote."""
    return {
        key: str(value)
        for key, value in env.items()
        if isinstance(value, (str, int, float))
        and not key.startswith("BASH_FUNC_")
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        and not _UNSAFE_ENV_VALUE.search(str(value))
    }


class _ClioPackage:
    """Shared RL40 behaviour. Mixed in ahead of a jarvis-cd base class."""

    #: Program the deployment contract probes when no ``executable`` parameter is set.
    clio_program: str = ""
    #: None: check the program is on PATH without running it.
    clio_probe_arguments: tuple[str, ...] | None = ("--version",)
    clio_probe_return_codes: tuple[int, ...] = (0,)
    clio_capabilities: tuple[str, ...] = ()

    _clio_contract_cache: Contract | None = None

    # -- contract -----------------------------------------------------------

    def clio_contract(self) -> Contract:
        if self._clio_contract_cache is None:
            self._clio_contract_cache = load_contract(Path(self.pkg_dir))
        return self._clio_contract_cache

    def _clio_placement(self) -> dict[str, str]:
        return {
            "shared_dir": str(self.shared_dir),
            "private_dir": str(self.private_dir),
            "pkg_dir": str(self.pkg_dir),
        }

    def _configure(self, **kwargs: Any) -> None:
        super()._configure(**kwargs)  # type: ignore[misc]
        contract = self.clio_contract()
        names = {entry["name"] for entry in contract.menu}

        # A value the fill rule chose last time is not a user choice: refill it.
        previous = self.config.get(FILLED_KEY) or {}
        for name, value in previous.items():
            if name not in kwargs or kwargs[name] == value:
                self.config[name] = ""

        given = {name: self.config.get(name) for name in names if name in self.config}
        resolved = contract.resolve_config(given, self._clio_placement())
        contract.check_parameters(resolved)
        self.config.update(resolved)
        self.config[FILLED_KEY] = {
            name: resolved[name]
            for name in names
            if given.get(name) in (None, "") and resolved.get(name) not in (None, "")
        }
        self._clio_prepare(resolved)
        self._clio_write_record(contract, resolved)

    def _clio_prepare(self, config: dict[str, Any]) -> None:
        """Write the package's input files. Runs before the record is written."""

    def clio_config(self) -> dict[str, Any]:
        names = {entry["name"] for entry in self.clio_contract().menu}
        return {name: self.config.get(name) for name in names}

    def _clio_write_record(self, contract: Contract, config: Mapping[str, Any]) -> Path:
        document = contract.describe(config, self._clio_placement())
        document["schema"] = RESOLVED_SCHEMA
        document["config"] = dict(config)
        document["inputs"] = contract.input_digests(config)
        document["pkg_id"] = getattr(self, "pkg_id", None)
        path = Path(str(self.shared_dir)) / RECORD_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path

    # -- launching ------------------------------------------------------------

    def clio_executable(self) -> str:
        return str(self.config.get("executable") or self.clio_program)

    def _clio_mpi(self, command: str, cwd: str) -> None:
        """Run ``command`` under MPI with stdout on the progress and artifact providers."""
        result = Exec(
            command,
            MpiExecInfo(
                nprocs=int(self.config["nprocs"]),
                ppn=int(self.config["ppn"]) if self.config.get("ppn") else None,
                hostfile=self.hostfile,
                env=mpi_safe_env(self.mod_env),
                cwd=cwd,
                line_callback=self.runtime_line_callback(),
            ),
        ).run()
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(f"{self.clio_contract().pkg_type} failed: {failures}")

    # -- jarvis-cd deployment contract --------------------------------------

    def _clio_runtime_status(self) -> RuntimeStatus:
        environment = self._deployment_environment()
        program = self.clio_executable()
        if os.path.isabs(program):
            # probe_program takes PATH names only; put the configured directory first.
            directory, program = os.path.split(program)
            environment["PATH"] = os.pathsep.join(
                part for part in (directory, environment.get("PATH", "")) if part
            )
        if shutil.which(program, path=environment.get("PATH")) is None:
            return RuntimeStatus("unavailable", "software_not_found")
        if self.clio_probe_arguments is None:
            # An MPI solver started outside mpiexec can hang in MPI_Init; finding it is enough.
            return RuntimeStatus("ready", "software_found")
        return probe_program(
            program,
            environment=environment,
            arguments=self.clio_probe_arguments,
            accepted_return_codes=self.clio_probe_return_codes,
            timeout_seconds=10,
        ).status

    def _deployment_contract(self) -> PackageDeploymentContract:
        contract = self.clio_contract()
        status = self._clio_runtime_status()
        requirement = RuntimeRequirement(
            requirement_id=contract.package,
            description=f"{self.clio_executable()} available through PATH",
            required_capabilities=self.clio_capabilities,
            available_capabilities=self.clio_capabilities if status.usable else (),
            status=status,
        )
        if contract.kind == "service":
            profile = ExecutionProfile(
                name="service",
                execution_kind="service",
                when=(ConfigurationCondition("nprocs", "greater_than", 0),),
                runtime_requirements=(contract.package,),
                readiness=ReadinessContract(mechanism="progress_event", condition="ready"),
                description=contract.data["summary"],
            )
        else:
            profile = ExecutionProfile(
                name="batch",
                execution_kind="batch",
                when=(ConfigurationCondition("nprocs", "greater_than", 0),),
                runtime_requirements=(contract.package,),
                readiness=ReadinessContract(mechanism="process_exit", condition="successful_exit"),
                description=contract.data["summary"],
            )
        return PackageDeploymentContract(
            package=contract.pkg_type,
            execution_profiles=(profile,),
            runtime_requirements=(requirement,),
        )


class ClioApplication(_ClioPackage, Application):
    """A clio-jarvis batch application."""


class ClioService(_ClioPackage, Service):
    """A clio-jarvis long-running service."""
