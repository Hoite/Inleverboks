"""Every UI string must have a Dutch translation (spec §0.5: i18n-ready)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from inleverboks.i18n import gettext

ROOT = Path(__file__).resolve().parents[2]


def _load_i18n_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("i18n_script", ROOT / "scripts" / "i18n.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["i18n_script"] = module
    spec.loader.exec_module(module)
    return module


def test_all_messages_are_translated() -> None:
    missing: list[str] = _load_i18n_script().missing_translations()
    assert missing == [], f"Run `uv run python scripts/i18n.py` and translate: {missing}"


def test_gettext_returns_dutch() -> None:
    assert gettext("Log in") == "Inloggen"
    assert gettext("An untranslated sentence") == "An untranslated sentence"
