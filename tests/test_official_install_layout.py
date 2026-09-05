from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from hermes_cli.plugins import PluginManager

# Plugin discovery is a Hermes CLI contract.
pytestmark = pytest.mark.hermes_contract


def test_repo_root_is_discovered_as_official_hermes_plugin(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "hermes-vk-adapter").symlink_to(root, target_is_directory=True)

    manifests = PluginManager()._scan_directory(plugins_dir, source="user")
    vk_manifests = [manifest for manifest in manifests if manifest.name == "vk"]

    assert len(vk_manifests) == 1
    assert vk_manifests[0].kind == "platform"
    assert Path(vk_manifests[0].path or "").resolve() == root


def test_repo_root_register_shim_loads_vk_adapter() -> None:
    root = Path(__file__).resolve().parents[1]
    init_file = root / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.test_vk_root",
        init_file,
        submodule_search_locations=[str(root)],
    )

    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert isinstance(module, ModuleType)
    assert callable(module.register)


# ── manifest consistency (Task 11) ────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(relative: str) -> dict:
    import yaml

    return yaml.safe_load((REPO_ROOT / relative).read_text(encoding="utf-8"))


def _env_names(manifest: dict, key: str) -> list[str]:
    return [entry["name"] for entry in (manifest.get(key) or [])]


def test_root_and_nested_manifests_agree_on_identity():
    root = _load("plugin.yaml")
    nested = _load("plugins/vk/plugin.yaml")

    for field in ("name", "label", "kind", "version"):
        assert root[field] == nested[field], field


def test_root_and_nested_manifests_declare_the_same_environment():
    """A drifted nested manifest silently changes what the installer asks for."""
    root = _load("plugin.yaml")
    nested = _load("plugins/vk/plugin.yaml")

    assert _env_names(root, "requires_env") == _env_names(nested, "requires_env")
    assert sorted(_env_names(root, "optional_env")) == sorted(_env_names(nested, "optional_env"))


def test_both_manifests_and_the_package_declare_the_same_version():
    """All three version declarations must be bumped together.

    A release that touches only some of them ships a plugin whose advertised
    version depends on which file the installer happened to read.  Versions are
    compared as strings because YAML would silently read a two-part `0.2` as a
    float.
    """
    import tomllib

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    versions = {
        "plugin.yaml": str(_load("plugin.yaml")["version"]),
        "plugins/vk/plugin.yaml": str(_load("plugins/vk/plugin.yaml")["version"]),
        "pyproject.toml": str(pyproject["project"]["version"]),
    }

    assert len(set(versions.values())) == 1, f"version drift: {versions}"


def test_every_documented_env_var_is_declared_in_the_manifest():
    """Anything the code reads must be discoverable by an operator."""
    import re

    manifest = _load("plugin.yaml")
    declared = set(_env_names(manifest, "requires_env") + _env_names(manifest, "optional_env"))

    reader = re.compile(r'(?:getenv|environ\.get|source\.get)\(\s*"(VK_[A-Z_]+)"')
    used = set()
    for path in (REPO_ROOT / "plugins").rglob("*.py"):
        used.update(reader.findall(path.read_text(encoding="utf-8")))

    assert used - declared == set(), f"undeclared env vars: {sorted(used - declared)}"


def test_env_example_covers_every_declared_variable():
    manifest = _load("plugin.yaml")
    declared = set(_env_names(manifest, "requires_env") + _env_names(manifest, "optional_env"))
    example = (REPO_ROOT / "config/.env.example").read_text(encoding="utf-8")

    missing = {name for name in declared if name not in example}
    assert missing == set(), f"missing from config/.env.example: {sorted(missing)}"


def test_the_repository_ships_a_ci_workflow_that_runs_the_gates():
    workflow = (REPO_ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")

    for gate in ("pytest", "ruff", "pip check"):
        assert gate in workflow, gate


def test_the_repository_does_not_claim_a_meaningful_wheel():
    """Hermes installs this plugin as a directory, not from a wheel.

    `[tool.setuptools] packages = []` builds a dist-info-only artifact, so a
    CI "packaging smoke" that then imports from the checkout is false green.
    Either package the modules or do not claim to test packaging.
    """
    import tomllib

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packaged = pyproject.get("tool", {}).get("setuptools", {}).get("packages")
    workflow = (REPO_ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")

    if not packaged:
        assert "build --wheel" not in workflow, (
            "CI builds a wheel that contains no plugin modules"
        )
        assert "Isolated import smoke" in workflow
