"""Pytest bootstrap that makes the Hermes contract importable for tests.

The adapter is a Hermes *plugin*: it imports ``gateway.platforms.base`` and
``gateway.config`` from the Hermes Agent it is installed into.  Tests must run
against that real contract rather than a hand-written stub, otherwise a Hermes
API change would silently pass here and fail on the gateway.

Hermes is not a PyPI package, so this module locates an existing Hermes
checkout and puts it on ``sys.path`` **read-only** — nothing is installed, and
the Hermes runtime is never modified.  Resolution order:

1. ``HERMES_AGENT_PATH``
2. ``<repo>/vendor/hermes-agent`` (the layout documented in the README)
3. ``$HERMES_HOME/hermes-agent``, else ``~/.hermes/hermes-agent``

When the checkout has its own virtualenv built with the running interpreter's
``major.minor``, its ``site-packages`` is appended *last* so Hermes' own
third-party dependencies resolve without polluting this repository's ``.venv``.
Repo-local packages always win because the repo root is prepended first.

When no checkout is found, the Hermes-dependent modules simply cannot be
imported; those tests skip with an explicit reason via the ``hermes_contract``
marker instead of erroring.  Everything that does not touch Hermes (VK client,
formatting, keyboards, callbacks, doctor, bounded state) still runs — that is
the bulk of the suite and it runs unchanged in CI.
"""

from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def _candidate_hermes_roots() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.getenv("HERMES_AGENT_PATH", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(REPO_ROOT / "vendor" / "hermes-agent")
    hermes_home = os.getenv("HERMES_HOME", "").strip()
    home_root = Path(hermes_home).expanduser() if hermes_home else Path.home() / ".hermes"
    candidates.append(home_root / "hermes-agent")
    return candidates


def _looks_like_hermes(root: Path) -> bool:
    return (root / "gateway" / "platforms" / "base.py").is_file()


def _venv_site_packages(root: Path) -> Path | None:
    """Return the checkout venv's site-packages when it matches this interpreter.

    Binary wheels are ABI-specific, so only a venv built with the same
    ``major.minor`` is safe to borrow.  Anything else is ignored.
    """
    tag = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = root / ".venv" / "lib" / tag / "site-packages"
    return site_packages if site_packages.is_dir() else None


def _bootstrap_hermes_path() -> Path | None:
    for root in _candidate_hermes_roots():
        if not _looks_like_hermes(root):
            continue
        if str(root) not in sys.path:
            sys.path.append(str(root))
        site_packages = _venv_site_packages(root)
        # Only borrow the checkout's dependencies; never shadow this repo's own
        # installed packages (pytest, httpx, respx) or the stdlib.
        if site_packages is not None and str(site_packages) not in sys.path:
            sys.path.append(str(site_packages))
        return root
    return None


# The repository root must come first so ``plugins.vk`` resolves here even when
# a Hermes checkout on the path also ships a ``plugins`` package.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Never let a borrowed site-packages shadow this venv's own installs.
_PURELIB = sysconfig.get_paths().get("purelib")
if _PURELIB and _PURELIB not in sys.path:
    sys.path.insert(1, _PURELIB)

HERMES_ROOT = _bootstrap_hermes_path()


def hermes_available() -> bool:
    """Whether the Hermes contract can be imported in this environment."""
    if HERMES_ROOT is None:
        return False
    try:
        import gateway.platforms.base  # noqa: F401
    except Exception:
        return False
    return True


HERMES_SKIP_REASON = (
    "Hermes Agent checkout not importable. Set HERMES_AGENT_PATH, clone into "
    "vendor/hermes-agent, or install Hermes into this environment to run the "
    "adapter contract tests."
)


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "hermes_contract: test requires an importable Hermes Agent checkout.",
    )


def pytest_collection_modifyitems(config, items) -> None:
    if hermes_available():
        return
    import pytest

    skip = pytest.mark.skip(reason=HERMES_SKIP_REASON)
    for item in items:
        if "hermes_contract" in item.keywords:
            item.add_marker(skip)
