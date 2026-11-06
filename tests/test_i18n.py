# tests/test_i18n.py
# Every text key the interface uses exists in English AND Arabic, and the two
# languages define exactly the same keys with the same {placeholders}.

import re
import string
from pathlib import Path

from app.config import CATEGORY_TREE
from ui.i18n import CATEGORY_NAMES_AR, TEXT

ROOT = Path(__file__).resolve().parent.parent
SOURCES = list((ROOT / "ui").rglob("*.py")) + [ROOT / "streamlit_app.py"]


KEY = r'"([a-z_]+\.[A-Za-z0-9_]+)"'


def _used_keys():
    """Keys passed to t(), and keys passed to page_header() / empty()."""
    keys = set()
    for path in SOURCES:
        source = path.read_text()
        keys |= set(re.findall(r"\bt\(\s*" + KEY, source))
        for args in re.findall(r"\b(?:page_header|empty)\(([^)]*)\)", source):
            keys |= set(re.findall(KEY, args))
    return keys


def _placeholders(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_both_languages_define_the_same_keys():
    assert set(TEXT["en"]) == set(TEXT["ar"])


def test_every_used_key_exists():
    missing = sorted(k for k in _used_keys() if k not in TEXT["en"])
    assert missing == []


def test_dynamic_key_families_are_complete():
    for prefix in ("nav.", "trend.", "freq.", "action."):
        assert any(k.startswith(prefix) for k in TEXT["en"])
    for page in ("dashboard", "transactions", "money", "analysis", "budget", "goals",
                 "plan", "essentials", "assistant", "tips", "setup"):
        assert f"nav.{page}" in TEXT["en"]
    for direction in ("increasing", "decreasing", "stable", "unknown"):
        assert f"trend.{direction}" in TEXT["en"]


def test_placeholders_match_between_languages():
    for key, english in TEXT["en"].items():
        assert _placeholders(english) == _placeholders(TEXT["ar"][key]), key


def test_every_category_has_an_arabic_name():
    names = set(CATEGORY_TREE) | {s for subs in CATEGORY_TREE.values() for s in subs}
    assert sorted(names - set(CATEGORY_NAMES_AR)) == []


def test_every_model_action_is_translated():
    from app.ml.feature_engineering import LABEL_COLUMNS

    for action in LABEL_COLUMNS:
        assert f"action.{action}" in TEXT["en"]


def test_every_tool_and_state_has_a_label():
    from app.tools.financial_tools import TOOL_FUNCTIONS

    for name in TOOL_FUNCTIONS:
        assert f"tool.{name}" in TEXT["en"], name
    for state in ("none", "entered", "recent", "pattern"):
        assert f"state.short.{state}" in TEXT["en"]
        assert f"state.{state}" in TEXT["en"]
    for label in ("Likely", "Borderline", "Unlikely"):
        assert f"afford.label.{label}" in TEXT["en"]
    for n in (1, 4, 12, 365):
        assert f"tips.times_{n}" in TEXT["en"]
