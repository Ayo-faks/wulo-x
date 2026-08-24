from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.version import Version

PACKAGE_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
LOCKED_PACKAGE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^ ;\\]+)")

SECURE_BACKEND_FLOORS = {
    "aiohttp": "3.14.1",
    "click": "8.3.3",
    "cryptography": "48.0.1",
    "fastapi": "0.141.1",
    "idna": "3.15",
    "pydantic-settings": "2.14.2",
    "pyjwt": "2.13.0",
    "python-multipart": "0.0.31",
    "starlette": "1.3.1",
}

PYTHON_313_BASE = (
    "python:3.13.14-slim-trixie@"
    "sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91"
)


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_names(path: Path) -> set[str]:
    names = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = PACKAGE_NAME.match(line)
        assert match, f"Unparseable requirement in {path}: {line}"
        names.add(_normalized(match.group(1)))
    return names


def _project_dependency_names(path: Path) -> set[str]:
    project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
    return {
        _normalized(PACKAGE_NAME.match(requirement).group(1))
        for requirement in project["dependencies"]
    }


def _assert_hash_lock(path: Path, expected_names: set[str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    declarations = [index for index, line in enumerate(lines) if LOCKED_PACKAGE.match(line)]
    assert declarations, f"No pinned packages in {path}"

    locked_names = {_normalized(LOCKED_PACKAGE.match(lines[index]).group(1)) for index in declarations}
    assert expected_names <= locked_names

    for offset, start in enumerate(declarations):
        end = declarations[offset + 1] if offset + 1 < len(declarations) else len(lines)
        assert any("--hash=sha256:" in line for line in lines[start:end]), lines[start]


def test_python_remote_builds_consume_current_hash_locks() -> None:
    services = (
        (
            Path("src/recall-agent/Dockerfile"),
            Path("src/recall-agent/requirements.txt"),
            Path("src/recall-agent/requirements.lock"),
        ),
        (
            Path("src/inbound-assistant/Dockerfile"),
            Path("src/inbound-assistant/requirements.txt"),
            Path("src/inbound-assistant/requirements.lock"),
        ),
        (
            Path("apps/cardapi/Dockerfile.mcp"),
            Path("apps/cardapi/mcp_app/requirements.txt"),
            Path("apps/cardapi/mcp_app/requirements.lock"),
        ),
    )

    for dockerfile, requirements, lock in services:
        source = dockerfile.read_text(encoding="utf-8")
        assert "pip install --no-cache-dir --require-hashes -r requirements.lock" in source
        assert "pip install --no-cache-dir -r requirements.txt" not in source
        _assert_hash_lock(lock, _requirement_names(requirements))

    backend_dockerfile = Path("apps/artagent/backend/Dockerfile").read_text(encoding="utf-8")
    assert "COPY apps/artagent/backend/requirements.lock ./requirements.lock" in backend_dockerfile
    assert "pip install --no-cache-dir --require-hashes -r requirements.lock" in backend_dockerfile
    assert "pip install --no-cache-dir --upgrade pip" not in backend_dockerfile
    assert "pip uninstall --yes setuptools" in backend_dockerfile
    assert backend_dockerfile.count(f"FROM {PYTHON_313_BASE}") == 2
    assert "COPY --from=builder /opt/venv /opt/venv" in backend_dockerfile
    runtime_stage = backend_dockerfile.rsplit("FROM python:3.13.14-slim-trixie", 1)[1]
    assert "apt-get" not in runtime_stage
    assert "build-essential" not in runtime_stage
    assert "gcc" not in runtime_stage
    assert "pip install --no-cache-dir ." not in backend_dockerfile
    _assert_hash_lock(
        Path("apps/artagent/backend/requirements.lock"),
        _project_dependency_names(Path("pyproject.toml")),
    )


def test_backend_runtime_lock_excludes_governance_dependencies() -> None:
    lock = Path("apps/artagent/backend/requirements.lock").read_text(encoding="utf-8")
    locked_names = {
        _normalized(match.group(1))
        for line in lock.splitlines()
        if (match := LOCKED_PACKAGE.match(line))
    }

    assert locked_names.isdisjoint(
        {"agentops-accelerator", "azure-ai-evaluation", "nltk"}
    )


def test_backend_runtime_lock_meets_security_floors() -> None:
    lock = Path("apps/artagent/backend/requirements.lock").read_text(encoding="utf-8")
    locked_versions = {
        _normalized(match.group(1)): Version(match.group(2))
        for line in lock.splitlines()
        if (match := LOCKED_PACKAGE.match(line))
    }

    for package, floor in SECURE_BACKEND_FLOORS.items():
        assert package in locked_versions, package
        assert locked_versions[package] >= Version(floor), package


def test_python_image_lock_freshness_gate_is_wired() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    checker = Path("devops/scripts/validate-python-image-locks.sh")

    assert "validate_python_image_locks:" in makefile
    assert checker.exists()
    source = checker.read_text(encoding="utf-8")
    assert "REQUIRED_UV_VERSION=0.7.19" in source
    assert source.count("--no-config") == 3
    for lock in (
        "src/recall-agent/requirements.lock",
        "src/inbound-assistant/requirements.lock",
        "apps/cardapi/mcp_app/requirements.lock",
        "apps/artagent/backend/requirements.lock",
    ):
        assert lock in source