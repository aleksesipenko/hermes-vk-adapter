"""`vkbottle` must not be a dependency, an import, or an installation step.

The adapter talks to VK over one raw ``httpx`` transport.  A second, optional
SDK path meant two upload implementations, two keyboard builders, and a
dependency whose pins (aiohttp, pydantic, msgspec, vkbottle-types) have to be
resolvable inside the Hermes runtime for a feature the raw client already
covers.  These tests keep that removal from silently regressing.
"""

from __future__ import annotations

import ast
import importlib
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = (REPO_ROOT / "plugins", REPO_ROOT / "tests")
FORBIDDEN = "vkbottle"


def _python_sources() -> list[Path]:
    sources = [REPO_ROOT / "__init__.py", REPO_ROOT / "conftest.py"]
    for directory in SOURCE_DIRS:
        sources.extend(sorted(directory.rglob("*.py")))
    return [path for path in sources if path.is_file() and Path(__file__) != path]


def _imported_module_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_python_source_imports_vkbottle() -> None:
    offenders = {
        str(path.relative_to(REPO_ROOT))
        for path in _python_sources()
        if FORBIDDEN in _imported_module_roots(path)
    }

    assert offenders == set()


def test_no_python_source_mentions_vkbottle() -> None:
    """Catches leftover attributes (``self.vkbottle_api``) and comments too."""
    offenders = {
        str(path.relative_to(REPO_ROOT))
        for path in _python_sources()
        if FORBIDDEN in path.read_text(encoding="utf-8")
    }

    assert offenders == set()


def test_httpx_is_the_only_runtime_dependency() -> None:
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = manifest["project"]["dependencies"]

    assert len(dependencies) == 1
    assert dependencies[0].startswith("httpx")


@pytest.mark.parametrize(
    "relative_path",
    [
        "scripts/install.sh",
        "README.md",
        "after-install.md",
        "plugin.yaml",
        "plugins/vk/plugin.yaml",
    ],
)
def test_operator_facing_files_never_ask_for_vkbottle(relative_path: str) -> None:
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

    assert FORBIDDEN not in text


class _BlockVkbottle:
    """Meta-path finder that makes ``vkbottle`` unimportable."""

    def find_module(self, fullname: str, path=None):  # pragma: no cover - legacy API
        return None

    def find_spec(self, fullname: str, path=None, target=None):
        if fullname == FORBIDDEN or fullname.startswith(f"{FORBIDDEN}."):
            raise ImportError(f"{fullname} is unavailable in this environment")
        return None


@pytest.fixture()
def without_vkbottle():
    """Reload the VK plugin modules with ``vkbottle`` unavailable."""
    blocker = _BlockVkbottle()
    purged = {name: module for name, module in sys.modules.items() if name.startswith(FORBIDDEN)}
    plugin_modules = [name for name in sys.modules if name.startswith("plugins.vk")]
    for name in plugin_modules:
        sys.modules.pop(name, None)
    for name in purged:
        sys.modules.pop(name, None)
    sys.meta_path.insert(0, blocker)
    try:
        yield importlib.import_module("plugins.vk.adapter")
    finally:
        sys.meta_path.remove(blocker)
        for name in [n for n in sys.modules if n.startswith("plugins.vk")]:
            sys.modules.pop(name, None)
        sys.modules.update(purged)
        importlib.import_module("plugins.vk.adapter")


@pytest.mark.hermes_contract
def test_plugin_imports_and_registers_without_vkbottle(without_vkbottle) -> None:
    adapter_module = without_vkbottle

    class Ctx:
        kwargs: dict | None = None

        def register_platform(self, **kwargs):
            self.kwargs = kwargs

        def register_cli_command(self, **kwargs):
            pass

    ctx = Ctx()
    adapter_module.register(ctx)

    assert ctx.kwargs is not None
    assert ctx.kwargs["name"] == "vk"
    assert FORBIDDEN not in (ctx.kwargs.get("install_hint") or "")


@pytest.mark.hermes_contract
def test_check_requirements_passes_without_vkbottle(without_vkbottle, monkeypatch) -> None:
    monkeypatch.setenv("VK_GROUP_TOKEN", "test-token-not-a-real-secret")
    monkeypatch.setenv("VK_GROUP_ID", "123456789")

    assert without_vkbottle.check_requirements() is True


@pytest.mark.hermes_contract
@pytest.mark.asyncio
async def test_document_upload_works_without_a_vkbottle_attribute(
    without_vkbottle,
    tmp_path: Path,
) -> None:
    """No ``vkbottle_api`` attribute exists at all — the raw path must be used."""
    adapter_module = without_vkbottle

    class FakeClient:
        def __init__(self) -> None:
            self.uploads: list[dict] = []
            self.messages: list[dict] = []

        async def upload_document_raw(self, *, peer_id: int, path: str, title: str | None = None):
            self.uploads.append({"peer_id": peer_id, "path": path, "title": title})
            return "doc-1_99"

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)
            return 123

    file_path = tmp_path / "report.txt"
    file_path.write_text("hello", encoding="utf-8")

    adapter = object.__new__(adapter_module.VKAdapter)
    adapter.client = FakeClient()
    assert not hasattr(adapter, "vkbottle_api")

    result = await adapter_module.VKAdapter.send_document(
        adapter,
        chat_id="987654321",
        file_path=str(file_path),
        caption="Report",
        file_name="visible-report.txt",
    )

    assert result.success
    assert adapter.client.uploads == [
        {"peer_id": 987654321, "path": str(file_path), "title": "visible-report.txt"}
    ]


@pytest.mark.hermes_contract
@pytest.mark.asyncio
async def test_photo_upload_works_without_a_vkbottle_attribute(
    without_vkbottle,
    tmp_path: Path,
) -> None:
    adapter_module = without_vkbottle

    class FakeClient:
        def __init__(self) -> None:
            self.photo_uploads: list[dict] = []
            self.messages: list[dict] = []

        async def upload_photo_message_raw(self, *, peer_id: int, path: str):
            self.photo_uploads.append({"peer_id": peer_id, "path": path})
            return "photo-1_2_key"

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)
            return 321

    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"png")

    adapter = object.__new__(adapter_module.VKAdapter)
    adapter.client = FakeClient()
    assert not hasattr(adapter, "vkbottle_api")

    result = await adapter_module.VKAdapter.send_image_file(
        adapter,
        chat_id="987654321",
        image_path=str(image_path),
        caption="Preview",
    )

    assert result.success
    assert adapter.client.photo_uploads == [{"peer_id": 987654321, "path": str(image_path)}]


def test_keyboards_render_raw_json_without_vkbottle(without_vkbottle) -> None:
    import json

    keyboards = importlib.import_module("plugins.vk.keyboards")
    payload = json.loads(keyboards.VKKeyboardFactory().command_keyboard())

    assert payload["inline"] is False
    assert payload["buttons"]
