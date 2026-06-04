from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from hermes_cli.plugins import PluginManager


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
