"""The RL40 contract of one clio-jarvis package: load it, check it, and resolve it.

A package directory holds ``pkg.py`` (a jarvis-cd package), ``contract.yaml``
(this contract), ``progress.py`` and ``artifacts.py`` (jarvis-cd providers that
delegate to :mod:`clio_jarvis._contract.runtime`), and a self-test. Nothing here
changes jarvis-cd; it only reads the package.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import string
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

CONTRACT_SCHEMA = "clio.package-contract.v1"
VIEW_SCHEMA = "clio.view-descriptor.v1"
RESOLVED_SCHEMA = "clio.package-resolved.v1"
STOP_RECORD_SCHEMA = "clio.service-stop.v1"

#: Every part RL40 requires, in the order the validator reports them.
PARTS: tuple[str, ...] = (
    "install",
    "parameters",
    "resources",
    "sizing",
    "outputs",
    "progress",
    "service",
    "provenance",
    "selftest",
)

CONTRACT_DIR = Path(__file__).resolve().parent
REPO_PACKAGES_DIR = CONTRACT_DIR.parent
REPO_ROOT = REPO_PACKAGES_DIR.parent
SCHEMAS_DIR = REPO_ROOT / "schemas"

#: Outputs a viewer follows; every other role is reported once, when the run ends.
VIEW_ROLE = "snapshots"
#: Spack MPI providers. A query names the mpi virtual only, so the site's MPI is reused.
_MPI_PROVIDERS = re.compile(
    r"\^\s*(?:\[[^\]]*\]\s*)?"
    r"(openmpi|mpich|mvapich2?|mvapich-plus|intel-mpi|intel-oneapi-mpi|cray-mpich|"
    r"spectrum-mpi|hpcx-mpi|msmpi|mpt|fujitsu-mpi|nvhpc|mpitrampoline|mpiwrapper)\b"
)


#: Set inside a batch allocation by the schedulers JARVIS submits to.
_JOB_VARIABLES = ("SLURM_JOB_ID", "PBS_JOBID", "LSB_JOBID", "FLUX_JOB_ID", "COBALT_JOBID")


def in_job(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return any(values.get(name) for name in _JOB_VARIABLES)


def is_view(output: Mapping[str, Any]) -> bool:
    return output.get("role", VIEW_ROLE) == VIEW_ROLE


def _in_case(when: Mapping[str, Any] | None, config: Mapping[str, Any]) -> bool:
    """Whether every parameter ``when`` names matches its schema; an absent one matches.

    ``const``, ``enum``, ``minimum`` and ``maximum`` are read here, so a job
    without jsonschema can evaluate them; other keywords need jsonschema.
    """
    if not when:
        return True
    present = {name: value for name, value in config.items() if name in when and value is not None}
    simple = {"const", "enum", "minimum", "maximum"}
    if all(set(rule) <= simple for rule in when.values()):
        for name, value in present.items():
            rule = when[name]
            if "const" in rule and value != rule["const"]:
                return False
            if "enum" in rule and value not in rule["enum"]:
                return False
            if "minimum" in rule and not value >= rule["minimum"]:
                return False
            if "maximum" in rule and not value <= rule["maximum"]:
                return False
        return True
    import jsonschema

    return jsonschema.Draft202012Validator({"properties": dict(when)}).is_valid(present)

_MENU_TYPES = {
    "int": "integer",
    "float": "number",
    "str": "string",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
}
_HASH_EXCLUDE_DIRS = {"__pycache__", ".git"}
_HASH_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


class ContractError(ValueError):
    """A package is refused. ``part`` names the RL40 part at fault."""

    def __init__(self, package: str, part: str, reason: str) -> None:
        self.package = package
        self.part = part
        self.reason = reason
        super().__init__(f"package {package!r} refused: contract part {part!r} {reason}")


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    """Return one JSON Schema from the repository's ``schemas/`` directory."""
    return json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))


def _validator(name: str):
    import jsonschema

    schema = load_schema(name)
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def validate_view_descriptor(descriptor: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` when a descriptor does not match view-descriptor.v1."""
    errors = sorted(_validator("view-descriptor.v1.json").iter_errors(dict(descriptor)), key=str)
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "(root)"
        raise ValueError(f"view descriptor invalid at {where}: {first.message}")


# ---------------------------------------------------------------------------
# Values and templates
# ---------------------------------------------------------------------------


class _Strict(dict):
    def __missing__(self, key: str) -> Any:
        raise KeyError(key)


def template_fields(template: str) -> set[str]:
    """Return the ``{name}`` fields a path template uses."""
    return {
        name.split(".")[0].split("[")[0]
        for _, name, _, _ in string.Formatter().parse(template)
        if name
    }


def render(template: str, config: Mapping[str, Any]) -> str:
    """Fill a path template from the configuration; a missing field is an error."""
    return template.format_map(_Strict(config))


def value_params(value: Any) -> set[str]:
    """Return the parameter names a contract value refers to."""
    if isinstance(value, Mapping):
        if "param" in value:
            return {value["param"]}
        (operands,) = value.values()
        names: set[str] = set()
        for item in operands:
            names |= {item} if isinstance(item, str) else value_params(item)
        return names
    return set()


def _operand(item: Any, config: Mapping[str, Any]) -> Any:
    if isinstance(item, str):
        return config.get(item)
    return evaluate(item, config)


def evaluate(value: Any, config: Mapping[str, Any]) -> Any:
    """Evaluate a literal, ``{param: name}`` or integer arithmetic value.

    An operand is a number, a parameter name or another arithmetic value.
    """
    if not isinstance(value, Mapping):
        return value
    if "param" in value:
        return config.get(value["param"])
    (operator, operands) = next(iter(value.items()))
    a, b = (_operand(item, config) for item in operands)
    if a is None or b is None:
        return None
    if operator == "mul":
        return a * b
    if operator == "add":
        return a + b
    a, b = int(a), int(b)
    if operator == "div":
        return a // b
    if operator == "ceil_div":
        return -(-a // b)
    raise ValueError(f"unknown operator {operator!r}")


#: Keys JARVIS knows at configure time but does not hand every provider.
PLACEMENT_KEYS = {"shared_dir", "private_dir", "pkg_dir"}


def fill(contract_fill: Mapping[str, str], config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the parameters a ``fill`` rule sets because they were left empty."""
    filled: dict[str, Any] = {}
    for name, template in contract_fill.items():
        if config.get(name) in (None, ""):
            filled[name] = render(template, {**config, **filled})
    return filled


# ---------------------------------------------------------------------------
# Package files
# ---------------------------------------------------------------------------


def package_files(pkg_dir: Path) -> list[Path]:
    """Files that define a package's behaviour: its directory, plus the repository's
    shared ``_*`` files and directories (``_contract``, ``_spack``)."""
    repo = pkg_dir.parent
    roots = [pkg_dir, *(child for child in sorted(repo.iterdir()) if child.name.startswith("_"))]
    files: list[Path] = []
    for root in roots:
        for path in [root] if root.is_file() else sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _HASH_EXCLUDE_DIRS for part in path.relative_to(repo).parts):
                continue
            if path.suffix in _HASH_EXCLUDE_SUFFIXES:
                continue
            files.append(path)
    return files


def package_sha256(pkg_dir: Path) -> str:
    """The review digest of a package as it sits in a JARVIS repository.

    One line per file of :func:`package_files`, ``<sha256> <x|-> <path>``
    (``x`` when the owner may execute it, the path relative to the
    repository with ``/``), the lines sorted bytewise and each ended by a
    newline. Relay computes the same value on the site from the package in
    the site's JARVIS root and pins a reviewed package by it (RL24, RL42).
    """
    repo = pkg_dir.parent
    lines = [
        f"{file_sha256(path)} {'x' if path.stat().st_mode & 0o100 else '-'} {path.relative_to(repo).as_posix()}\n".encode()
        for path in package_files(pkg_dir)
    ]
    return hashlib.sha256(b"".join(sorted(lines))).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# The menu, read without running jarvis-cd
# ---------------------------------------------------------------------------


def read_menu(pkg_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    """Return the package class name and its ``_configure_menu`` entries.

    pkg.py is parsed, not imported, so the validator runs without a JARVIS
    installation and never executes package code. ``type`` is kept by name.
    """
    package = pkg_dir.name
    source = (pkg_dir / "pkg.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_name = "".join(word.capitalize() for word in re.split(r"[_-]", package))
    cls = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name),
        None,
    )
    if cls is None:
        raise ContractError(package, "parameters", f"has no class {class_name} in pkg.py")
    bases = {getattr(base, "id", getattr(base, "attr", "")) for base in cls.bases}
    if not bases & {"Application", "Service"}:
        raise ContractError(
            package, "parameters", f"class {class_name} must subclass JARVIS's Application or Service"
        )
    method = next(
        (node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_configure_menu"),
        None,
    )
    if method is None:
        raise ContractError(package, "parameters", "has no _configure_menu in pkg.py")
    returns = [node for node in ast.walk(method) if isinstance(node, ast.Return)]
    if len(returns) != 1 or not isinstance(returns[0].value, ast.List):
        raise ContractError(package, "parameters", "_configure_menu must return one literal list")
    entries: list[dict[str, Any]] = []
    for element in returns[0].value.elts:
        if not isinstance(element, ast.Dict):
            raise ContractError(package, "parameters", "menu entries must be literal dicts")
        entry: dict[str, Any] = {}
        for key_node, value_node in zip(element.keys, element.values):
            if key_node is None:
                raise ContractError(package, "parameters", "menu entries must be literal dicts")
            key = ast.literal_eval(key_node)
            if key == "type":
                entry[key] = getattr(value_node, "id", None)
            else:
                try:
                    entry[key] = ast.literal_eval(value_node)
                except ValueError as exc:
                    raise ContractError(
                        package, "parameters", f"menu entry field {key!r} must be a literal"
                    ) from exc
        entries.append(entry)
    return class_name, entries


def parameters_schema(menu: Iterable[Mapping[str, Any]], refinement: Mapping[str, Any]) -> dict[str, Any]:
    """JSON Schema for the parameters: the menu, refined by the contract."""
    properties: dict[str, Any] = {}
    for entry in menu:
        prop: dict[str, Any] = {"description": entry["msg"]}
        json_type = _MENU_TYPES.get(entry.get("type") or "")
        if json_type:
            prop["type"] = json_type
        if entry.get("default") is not None:
            prop["default"] = entry["default"]
        if entry.get("choices"):
            prop["enum"] = list(entry["choices"])
        if entry["name"] in INSTALL_ENTRIES:
            # The reviewed install part: a run may not change it.
            prop["const"] = entry.get("default")
        properties[entry["name"]] = prop
    for name, extra in (refinement.get("properties") or {}).items():
        properties[name] = {**properties[name], **extra}
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        # JARVIS adds its own keys (deploy_mode, interceptors, ...) to every package.
        "additionalProperties": True,
    }
    if refinement.get("required"):
        schema["required"] = list(refinement["required"])
    # A case's rules apply only when the configuration is in that case, so a
    # parameter one case uses is not checked, or required, in another.
    rules = []
    for case in refinement.get("cases") or []:
        then: dict[str, Any] = {}
        if case.get("required"):
            then["required"] = list(case["required"])
        if case.get("properties"):
            then["properties"] = {name: dict(extra) for name, extra in case["properties"].items()}
        if then:
            rules.append({"if": {"properties": {name: dict(match) for name, match in case["when"].items()}}, "then": then})
    if rules:
        schema["allOf"] = rules
    return schema


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


@dataclass
class Contract:
    pkg_dir: Path
    data: dict[str, Any]
    class_name: str
    menu: list[dict[str, Any]]
    sha256: str = field(default="")
    #: False when loaded in a job without the schema checks (see load_contract).
    checked: bool = field(default=True)

    @property
    def package(self) -> str:
        return self.data["package"]

    @property
    def pkg_type(self) -> str:
        # The repository's own name, so a copy of _contract in another repository names its packages.
        return f"{self.pkg_dir.parent.name}.{self.package}"

    @property
    def version(self) -> str:
        return self.data["version"]

    @property
    def kind(self) -> str:
        return self.data["kind"]

    def defaults(self) -> dict[str, Any]:
        return {entry["name"]: entry.get("default") for entry in self.menu}

    def install_options(self) -> list[dict[str, Any]]:
        """The install part's first entry, then its alternatives."""
        return install_options(self.data["install"])

    def _described_install(self) -> dict[str, Any]:
        """The install part as relay reads it: the first option, then its alternatives."""
        first, *rest = (
            {key: list(value) if isinstance(value, list) else value for key, value in option.items()}
            for option in self.install_options()
        )
        return {**first, "alternatives": rest} if rest else first

    def resolve_config(
        self, config: Mapping[str, Any], placement: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Defaults, then the given values, then the ``fill`` rule for empty ones.

        ``placement`` carries ``shared_dir`` and friends; only fill templates
        may use it, and it is not part of the result. The sizing part's
        ``cores`` parameter (the target partition's physical cores per node)
        fills an empty processes-per-node parameter. Nothing here reads the
        configuring host, which may be a login node.
        """
        merged = {**self.defaults(), **dict(config)}
        rules = self.data["parameters"].get("fill") or {}
        merged.update(fill(rules, {**dict(placement or {}), **merged}))
        ppn = self.data["sizing"]["processes_per_node"]
        cores = merged.get(ppn["cores"])
        if merged.get(ppn["param"]) in (None, "") and cores not in (None, ""):
            merged[ppn["param"]] = int(cores)
        return merged

    def parameters_schema(self) -> dict[str, Any]:
        return parameters_schema(self.menu, self.data["parameters"])

    def check_parameters(self, config: Mapping[str, Any]) -> None:
        """Refuse a configuration the parameter schema rejects.

        A parameter whose value is None is absent: an optional limit such as
        ``walltime`` or ``max_lifetime`` with no value means no limit.
        """
        import jsonschema

        validator = jsonschema.Draft202012Validator(self.parameters_schema())
        present = {name: value for name, value in config.items() if value is not None}
        errors = sorted(validator.iter_errors(present), key=str)
        if errors:
            first = errors[0]
            where = "/".join(str(p) for p in first.absolute_path) or "(root)"
            raise ValueError(f"{self.pkg_type} parameter {where}: {first.message}")

    def resources(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """PSI/J resources and attributes for this configuration (RL15)."""
        resolved = {
            name: evaluate(value, config)
            for name, value in self.data["resources"].items()
        }
        resolved = {name: value for name, value in resolved.items() if value not in (None, "")}
        if "node_count" not in resolved and resolved.get("processes_per_node"):
            resolved["node_count"] = math.ceil(
                int(resolved["process_count"]) / int(resolved["processes_per_node"])
            )
        return resolved

    def view_descriptor(
        self, output: Mapping[str, Any], config: Mapping[str, Any], *, growing: bool
    ) -> dict[str, Any]:
        """The view descriptor of one snapshots output for this configuration."""
        if not is_view(output):
            raise ValueError(f"output {output['name']!r} is {output['role']}, not a view")
        descriptor: dict[str, Any] = {
            "schema": VIEW_SCHEMA,
            "name": output["name"],
            "path": render(output["path"], config),
            "path_kind": output["path_kind"],
            "format": output["format"],
            "reader": output["reader"],
            "reader_properties": dict(output.get("reader_properties") or {}),
            "growing": growing,
        }
        follow = dict(output.get("follow") or {})
        expected = evaluate(output.get("expected_steps"), config)
        if expected is not None:
            follow["expected_steps"] = int(expected)
        if follow:
            descriptor["follow"] = follow
        time_spec = dict(output.get("time") or {})
        if "step_interval" in time_spec:
            time_spec["step_interval"] = evaluate(time_spec["step_interval"], config)
        if time_spec:
            descriptor["time"] = time_spec
        if output.get("display"):
            descriptor["display"] = dict(output["display"])
        geometry = [
            {
                "path": render(entry["path"], config),
                **{key: entry[key] for key in ("label", "visible") if key in entry},
            }
            for entry in output.get("geometry") or []
            if _in_case(entry.get("when"), config)
        ]
        if geometry:
            descriptor["geometry"] = geometry
        descriptor["producer"] = {"package": self.pkg_type, "version": self.version}
        if self.sha256:
            descriptor["producer"]["sha256"] = self.sha256
        if self.checked:
            validate_view_descriptor(descriptor)
        return descriptor

    def output_record(self, output: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        """A statistics or restart output: its role, rendered path and format."""
        record = {
            "name": output["name"],
            "role": output.get("role", VIEW_ROLE),
            "path": render(output["path"], config),
            "path_kind": output["path_kind"],
            "format": output["format"],
        }
        if output.get("description"):
            record["description"] = output["description"]
        return record

    def output(self, name: str) -> dict[str, Any]:
        for output in self.data["outputs"]:
            if output["name"] == name:
                return output
        raise KeyError(name)

    def describe(
        self, config: Mapping[str, Any] | None = None, placement: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """The document relay reads before a run: schema, resources, outputs, ports."""
        try:
            merged = self.resolve_config(config or {}, placement)
        except KeyError:
            merged = {**self.defaults(), **(config or {})}
        document: dict[str, Any] = {
            "schema": "clio.package-description.v1",
            "package": self.pkg_type,
            "version": self.version,
            "sha256": self.sha256,
            "kind": self.kind,
            "title": self.data.get("title") or self.pkg_type,
            "summary": self.data["summary"],
            "install": self._described_install(),
            "parameters": self.parameters_schema(),
            "review": list(self.data["parameters"].get("review") or []),
            "sizing": dict(self.data["sizing"]),
            "progress": {
                key: value
                for key, value in self.data["progress"].items()
                if key in {"unit", "output"}
            },
            "provenance": {
                "inputs": list(self.data["provenance"]["inputs"]),
                "upstream": list(self.data["provenance"].get("upstream") or []),
            },
        }
        try:
            document["resources"] = self.resources(merged)
        except (TypeError, ValueError):
            document["resources"] = None
        outputs = []
        for output in self.data["outputs"]:
            try:
                if is_view(output):
                    outputs.append(self.view_descriptor(output, merged, growing=True))
                else:
                    outputs.append(self.output_record(output, merged))
            except (KeyError, ValueError):
                outputs.append({"name": output["name"], "path": output["path"], "unresolved": True})
        document["outputs"] = outputs
        if "service" in self.data:
            document["service"] = self.service(merged)
        return document

    def service(self, config: Mapping[str, Any]) -> dict[str, Any]:
        spec = self.data["service"]
        ports = []
        for port in spec["ports"]:
            ports.append(
                {
                    "name": port["name"],
                    "protocol": port["protocol"],
                    "port": evaluate(port["port"], config),
                    "bind": evaluate(port.get("bind", "127.0.0.1"), config),
                    "health": dict(port["health"]),
                    "protection": {
                        key: evaluate(value, config) if key == "connect_id" else value
                        for key, value in port["protection"].items()
                    },
                }
            )
        stop = dict(spec["stop"])
        try:
            stop["writes"] = render(stop["writes"], config)
        except KeyError:
            pass
        return {
            "ports": ports,
            "stop": stop,
            "max_lifetime": evaluate(spec.get("max_lifetime"), config),
            "linger": evaluate(spec.get("linger"), config),
        }


def load_contract(pkg_dir: str | Path, *, check: bool = True) -> Contract:
    """Load and check one package. Raises :class:`ContractError` naming the missing part.

    With ``check`` false the schema and semantic checks are skipped, so the
    progress and artifact providers run in a job whose JARVIS has no
    jsonschema. Relay checks the package before it submits, and the digest
    pins what it checked.
    """
    pkg_dir = Path(pkg_dir).resolve()
    package = pkg_dir.name
    contract_path = pkg_dir / "contract.yaml"
    if not (pkg_dir / "pkg.py").is_file():
        raise ContractError(package, "parameters", "has no pkg.py")
    if not contract_path.is_file():
        raise ContractError(package, "parameters", "has no contract.yaml")
    data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ContractError(package, "parameters", "contract.yaml is not a mapping")
    if "install" in data:
        raise ContractError(
            package, "install", "belongs in pkg.py's menu (install_method, install_query, install_repos), not contract.yaml"
        )
    class_name, menu = read_menu(pkg_dir)
    data["install"] = install_from_menu(package, menu)

    kind = data.get("kind")
    if not check:
        return Contract(
            pkg_dir=pkg_dir, data=data, class_name=class_name, menu=menu, sha256=package_sha256(pkg_dir), checked=False
        )
    for part in PARTS:
        if part == "service" and kind != "service":
            continue
        if part not in data or data[part] is None:
            raise ContractError(package, part, "is missing")

    errors = sorted(_validator("package-contract.v1.json").iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        part = str(first.absolute_path[0]) if first.absolute_path else "parameters"
        if part not in PARTS:
            part = next((p for p in PARTS if p in first.message), part)
        where = "/".join(str(p) for p in first.absolute_path) or "(root)"
        raise ContractError(package, part, f"is malformed at {where}: {first.message}")
    if data["package"] != package:
        raise ContractError(package, "parameters", f"names package {data['package']!r}, not {package!r}")

    contract = Contract(pkg_dir=pkg_dir, data=data, class_name=class_name, menu=menu)
    _check_semantics(contract)
    contract.sha256 = package_sha256(pkg_dir)
    return contract


#: Menu entries that hold the install part; jarvis-cd's installer reads them.
INSTALL_ENTRIES = ("install_method", "install_query", "install_repos", "install_channels", "install_methods")


def install_from_menu(package: str, menu: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The install part, read from the menu defaults pkg.py gives jarvis-cd.

    One option comes from install_method, install_query, install_repos and
    install_channels; alternatives come from install_methods, first entry
    first. Spack repos stay relative to the package directory, as jarvis-cd
    resolves them.
    """
    values = {entry.get("name"): entry.get("default") for entry in menu}
    listed = values.get("install_methods") or []
    if listed:
        raw = [dict(option) for option in listed]
    else:
        raw = [
            {
                "method": values.get("install_method"),
                "query": values.get("install_query"),
                "repos": values.get("install_repos") or [],
                "channels": values.get("install_channels") or [],
            }
        ]
    options = []
    for option in raw:
        method, query = option.get("method"), option.get("query")
        if not method or not query:
            raise ContractError(
                package, "install", "pkg.py's menu needs install_method and install_query defaults (or install_methods)"
            )
        entry: dict[str, Any] = {"method": method, "query": query}
        if method == "spack":
            entry["repos"] = list(option.get("repos") or [])
        if method == "conda" and option.get("channels"):
            entry["channels"] = list(option["channels"])
        options.append(entry)
    first, *rest = options
    return {**first, "alternatives": rest} if rest else first


def install_options(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """An install block's first entry, then its alternatives, each without alternatives."""
    first = {key: value for key, value in spec.items() if key != "alternatives"}
    return [first, *(dict(option) for option in spec.get("alternatives") or [])]


#: Names a pip or conda query must not install: an MPI (conda's mpi4py pulls one in).
_PACKAGE_MANAGER_MPI = re.compile(
    r"^\s*(?:[\w.-]+::)?(openmpi|mpich|mvapich2?|intel-mpi|impi[-_]rt|intel-oneapi-mpi|"
    r"cray-mpich|hpcx-mpi|msmpi|mpi4py|mpi)\b",
    re.IGNORECASE,
)

#: MPI decompositions: such a code is compiled against the site's MPI, so Spack installs it.
_MPI_DECOMPOSITIONS = {"pencil2d", "block3d"}


def _check_install(contract: Contract) -> None:
    package = contract.package
    methods = [option["method"] for option in contract.install_options()]
    duplicated = sorted({method for method in methods if methods.count(method) > 1})
    if duplicated:
        raise ContractError(package, "install", f"lists the method {duplicated[0]!r} twice")
    for option in contract.install_options():
        if option["method"] == "spack":
            _check_spack(package, option, contract.pkg_dir)
        elif option["method"] in ("pip", "conda"):
            _check_package_manager(contract, option)


def _check_package_manager(contract: Contract, option: Mapping[str, Any]) -> None:
    package, method = contract.package, option["method"]
    decomposition = contract.data["sizing"]["decomposition"]
    if decomposition in _MPI_DECOMPOSITIONS:
        raise ContractError(
            package, "install",
            f"{method} cannot install a {decomposition} MPI code; use spack so the site's MPI is reused",
        )
    for token in option["query"].split():
        match = _PACKAGE_MANAGER_MPI.match(token)
        if match:
            raise ContractError(
                package, "install",
                f"{method} query names the MPI {match.group(1)!r}; MPI comes from the site's Spack",
            )


def _check_spack(package: str, spec: Mapping[str, Any], pkg_dir: Path) -> None:
    match = _MPI_PROVIDERS.search(spec["query"])
    if match:
        raise ContractError(
            package, "install", f"query pins the MPI {match.group(1)!r}; depend on the mpi virtual only"
        )
    repo_dir = pkg_dir.parent.resolve()
    paths = []
    for repo in spec["repos"]:
        path = (pkg_dir / repo).resolve()
        inside = path.is_relative_to(repo_dir) and path != repo_dir
        # Only top-level _ directories are in the review digest.
        if Path(repo).is_absolute() or not inside or not path.relative_to(repo_dir).parts[0].startswith("_"):
            raise ContractError(
                package, "install", f"repo {repo!r} must be a relative path to a top-level _ directory of this repository"
            )
        if not (path / "repo.yaml").is_file() or not (path / "packages").is_dir():
            raise ContractError(package, "install", f"repo {repo!r} is not a Spack repository")
        paths.append(path)
    root = re.match(r"\s*([A-Za-z0-9_.-]+)", spec["query"])
    name = root.group(1).split(".")[-1] if root else ""
    in_repos = any((path / "packages" / name / "package.py").is_file() for path in paths)
    if spec["repos"] and not in_repos and name not in _BUILTIN_ROOTS:
        raise ContractError(package, "install", f"no recipe for {name!r} in {spec['repos']}")


#: Root specs Spack's builtin repository provides, so they need no clio recipe.
_BUILTIN_ROOTS = {"paraview"}


def _check_semantics(contract: Contract) -> None:
    package = contract.package
    data = contract.data
    names = {entry.get("name") for entry in contract.menu}
    for entry in contract.menu:
        if not entry.get("name") or not entry.get("msg") or not entry.get("type"):
            raise ContractError(package, "parameters", f"menu entry {entry!r} needs name, msg and type")
    known = names | {"shared_dir", "private_dir", "pkg_dir"}

    def need(part: str, referenced: Iterable[str], where: str) -> None:
        missing = sorted(set(referenced) - known)
        if missing:
            raise ContractError(package, part, f"{where} refers to unknown parameters {missing}")

    refinement = data["parameters"]
    need("parameters", (refinement.get("properties") or {}).keys(), "properties")
    need("parameters", refinement.get("required") or [], "required")
    need("parameters", refinement.get("review") or [], "review")
    for case in refinement.get("cases") or []:
        where = f"case {case['name']!r}"
        need("parameters", case["when"].keys(), f"{where} when")
        need("parameters", case.get("required") or [], f"{where} required")
        need("parameters", (case.get("properties") or {}).keys(), f"{where} properties")
    for name, template in (refinement.get("fill") or {}).items():
        need("parameters", {name} | template_fields(template), f"fill {name!r}")
    known = names
    # Providers see the configuration, not JARVIS placement, so everything past
    # the fill rule names parameters only.

    for name, value in data["resources"].items():
        need("resources", value_params(value), name)

    _check_install(contract)
    sizing = data["sizing"]
    need("sizing", {sizing["processes_per_node"]["param"], sizing["processes_per_node"]["cores"]}, "processes_per_node")
    need("sizing", value_params(sizing["nodes"]), "nodes")

    view_names = set()
    for output in data["outputs"]:
        if is_view(output):
            view_names.add(output["name"])
        need("outputs", template_fields(output["path"]), f"output {output['name']!r} path")
        need("outputs", value_params(output.get("expected_steps")), f"output {output['name']!r} expected_steps")
        for entry in output.get("geometry") or []:
            where = f"output {output['name']!r} geometry {entry['path']!r}"
            need("outputs", template_fields(entry["path"]), where)
            need("outputs", (entry.get("when") or {}).keys(), f"{where} when")

    progress = data["progress"]
    for key in ("step_pattern", "done_pattern", "ready_pattern"):
        if key in progress:
            try:
                compiled = re.compile(progress[key])
            except re.error as exc:
                raise ContractError(package, "progress", f"{key} does not compile: {exc}") from exc
            if key == "step_pattern" and "step" not in compiled.groupindex:
                raise ContractError(package, "progress", "step_pattern needs a named group 'step'")
    if "output" in progress and progress["output"] not in view_names:
        raise ContractError(package, "progress", f"points at {progress['output']!r}, which is not a snapshots output")
    need("progress", value_params(progress.get("total")), "total")
    for provider in ("progress.py", "artifacts.py"):
        path = contract.pkg_dir / provider
        if not path.is_file() or "adapter_from_package" not in path.read_text(encoding="utf-8"):
            raise ContractError(package, "progress", f"needs {provider} exporting adapter_from_package")

    if "service" in data:
        for port in data["service"]["ports"]:
            need("service", value_params(port["port"]) | value_params(port.get("bind")), f"port {port['name']!r}")
        need("service", template_fields(data["service"]["stop"]["writes"]), "stop.writes")

    for template in data["provenance"]["inputs"]:
        need("provenance", template_fields(template), f"input {template!r}")

    selftest = data["selftest"]
    unknown = sorted(set(selftest["params"]) - names)
    if unknown:
        raise ContractError(package, "selftest", f"params refers to unknown parameters {unknown}")
    script = selftest.get("script")
    if script and not (contract.pkg_dir / script).is_file():
        raise ContractError(package, "selftest", f"script {script!r} does not exist")
    config = contract.resolve_config(
        {**selftest["params"]},
        placement={"shared_dir": "/selftest/shared", "private_dir": "/selftest/private", "pkg_dir": str(contract.pkg_dir)},
    )
    try:
        contract.check_parameters({k: v for k, v in config.items() if k in names})
    except ValueError as exc:
        raise ContractError(package, "selftest", f"params are invalid: {exc}") from exc
    for output in data["outputs"]:
        try:
            if is_view(output):
                contract.view_descriptor(output, config, growing=True)
            else:
                contract.output_record(output, config)
        except (KeyError, ValueError) as exc:
            raise ContractError(
                package, "outputs", f"output {output['name']!r} gives no valid view descriptor: {exc}"
            ) from exc
    resources = contract.resources(config)
    if resources.get("node_count", 1) != 1:
        raise ContractError(package, "selftest", "must run on one node")
    duration = resources.get("duration")
    if duration is not None and (not isinstance(duration, int) or duration <= 0):
        raise ContractError(package, "resources", "duration, when given, must resolve to positive seconds")


def find_packages(repo_dir: str | Path = REPO_PACKAGES_DIR) -> list[Path]:
    """Package directories in the repository (every child with a pkg.py)."""
    root = Path(repo_dir)
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "pkg.py").is_file())
