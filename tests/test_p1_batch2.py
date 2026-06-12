"""P1 第二批测试：细纲契约（#7）/ 文风锚点（#9）/ 爽点循环完成率（#10）。

spec: docs/superpowers/specs/2026-06-13-p1-batch2-design.md
"""
from __future__ import annotations

import json
import sqlite3

import pytest


@pytest.fixture
def conn():
    from novelforge.db.connection import init_db_from_conn
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db_from_conn(c)
    yield c
    c.close()


# ── #7 钩子枚举与归一 ─────────────────────────────────────────────────────────

class TestHookNormalize:
    def test_exact_enum_key_passthrough(self):
        from novelforge.craft.hooks import normalize_hook_type
        assert normalize_hook_type("reversal", "ending") == "reversal"
        assert normalize_hook_type("suspense", "opening") == "suspense"

    def test_chinese_keyword_maps_to_enum(self):
        from novelforge.craft.hooks import normalize_hook_type
        assert normalize_hook_type("反转式", "ending") == "reversal"
        assert normalize_hook_type("命悬一线", "ending") == "cliffhanger"
        assert normalize_hook_type("悬念开局", "opening") == "suspense"

    def test_unrecognizable_returns_other(self):
        from novelforge.craft.hooks import normalize_hook_type
        assert normalize_hook_type("写得很好看", "ending") == "other"
        assert normalize_hook_type("", "ending") == "other"
        assert normalize_hook_type(None, "opening") == "other"

    def test_wrong_kind_enum_not_passthrough(self):
        from novelforge.craft.hooks import normalize_hook_type
        # cliffhanger 是章尾式，不在章首 7 式里
        assert normalize_hook_type("cliffhanger", "opening") == "other"

    def test_hook_label_renders_chinese(self):
        from novelforge.craft.hooks import hook_label
        assert hook_label("reversal") == "反转"
        assert hook_label("other") == "other"
