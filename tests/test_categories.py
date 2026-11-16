"""League scoring categories: supported mapping unchanged; unsupported ones are listed and surfaced loudly."""

from __future__ import annotations

import json

from fbm.analytics import categories as cats
from fbm.gm import health


def _cat(stat_id, name, **extra):
    return {"stat_id": stat_id, "name": name, "display_name": name, "higher_is_better": True, **extra}


NINE = [
    _cat("5", "FG%"),
    _cat("8", "FT%"),
    _cat("10", "3PTM"),
    _cat("12", "PTS"),
    _cat("15", "REB"),
    _cat("16", "AST"),
    _cat("17", "ST"),
    _cat("18", "BLK"),
    _cat("19", "TO"),
]


def test_standard_nine_cat_is_fully_supported():
    support = cats.category_support({"scoring_categories": NINE})
    assert support.categories == cats.NINE_CAT
    assert support.status == "OK" and support.ignored == () and not support.assumed_nine_cat
    assert "All 9 scoring categories modeled" in support.message
    assert cats.from_league_settings({"scoring_categories": NINE}) == cats.NINE_CAT


def test_subset_league_maps_unchanged():
    eight = [c for c in NINE if c["stat_id"] != "19"]
    support = cats.category_support({"scoring_categories": eight})
    assert [c.key for c in support.categories] == ["fg_pct", "ft_pct", "tpm", "pts", "reb", "ast", "stl", "blk"]
    assert support.status == "OK"


def test_unsupported_scored_category_fails_loudly():
    summary = {"scoring_categories": [*NINE, _cat("27", "DD"), _cat("13", "OREB")]}
    support = cats.category_support(summary)
    assert support.categories == cats.NINE_CAT  # supported mapping unchanged
    assert [c["name"] for c in support.ignored] == ["DD", "OREB"]
    assert support.status == "FAIL"
    assert "Your league scores DD, OREB, which FBM can't model" in support.message
    d = support.as_dict()
    assert d["status"] == "FAIL" and d["ignored"][0] == {"stat_id": "27", "name": "DD", "display_only": False}
    assert cats.from_league_settings(summary) == cats.NINE_CAT


def test_display_only_unknowns_warn_but_makes_attempts_are_silent():
    summary = {
        "scoring_categories": [
            *NINE,
            _cat("9004003", "FGM/A", is_only_display_stat="1"),
            _cat("9007006", "FTM/A", display_only=True),
            _cat("28", "TD", display_only=True),
            _cat("21", "PF", enabled=False),
        ]
    }
    support = cats.category_support(summary)
    assert [c["name"] for c in support.ignored] == ["TD"]
    assert support.status == "WARN" and "display-only" in support.message


def test_nothing_mapped_falls_back_to_nine_cat_and_says_so():
    points = cats.category_support({"scoring_categories": [_cat("27", "DD")]})
    assert points.categories == cats.NINE_CAT and points.assumed_nine_cat and points.status == "FAIL"
    assert "assumed standard 9-cat" in points.message
    empty = cats.category_support({"scoring_categories": []})
    assert empty.categories == cats.NINE_CAT and empty.status == "WARN" and empty.ignored == ()
    missing = cats.category_support(None)
    assert missing.status == "WARN" and "not read yet" in missing.message
    assert cats.from_league_settings({}) == cats.NINE_CAT


def test_integer_stat_ids_still_map():
    support = cats.category_support({"scoring_categories": [{"stat_id": 12, "display_name": "PTS"}]})
    assert [c.key for c in support.categories] == ["pts"]


def test_health_check_categories(tmp_path):
    assert health.check_categories("demo", None) == []
    ok = health.check_categories("yahoo", {"scoring_categories": NINE})
    assert ok[0]["status"] == "OK" and ok[0]["check"] == "League categories" and ok[0]["fix"] == ""
    bad = health.check_categories("yahoo", {"scoring_categories": [*NINE, _cat("20", "A/T")]})
    assert bad[0]["status"] == "FAIL" and "A/T" in bad[0]["detail"] and "fbm settings" in bad[0]["fix"]
    unread = health.check_categories("yahoo", None)
    assert unread[0]["status"] == "WARN" and "fbm snapshot" in unread[0]["fix"]
    path = tmp_path / "summary.json"
    assert health.read_league_summary(path) is None
    path.write_text("not json", encoding="utf-8")
    assert health.read_league_summary(path) is None
    path.write_text(json.dumps({"scoring_categories": NINE}), encoding="utf-8")
    assert health.read_league_summary(path) == {"scoring_categories": NINE}


def test_settings_command_warning_block():
    from fbm.cli import _category_warning

    assert _category_warning({"scoring_categories": NINE}) == ""
    block = _category_warning({"scoring_categories": [*NINE, _cat("27", "DD")]})
    assert "FAIL: league categories FBM can't model" in block and "DD" in block


def test_context_pack_exposes_category_support(monkeypatch, tmp_path):
    from fbm.gm import services as svc
    from fbm.store import db

    conn = db.connect(tmp_path / "fbm.db")
    db.migrate(conn)
    monkeypatch.setattr(svc, "_conn", lambda: conn)
    monkeypatch.setattr(svc.memory, "memory_files", lambda chars: {})
    monkeypatch.setattr(svc.memory, "journal_digest", lambda: [])
    monkeypatch.setattr(svc.memory, "manager_note_names", lambda: [])
    monkeypatch.setattr(svc, "signals_tool", lambda **kw: {"signals": []})
    monkeypatch.setattr(health, "read_league_summary", lambda: {"scoring_categories": [*NINE, _cat("27", "DD")]})
    monkeypatch.setattr(svc, "league_overview", lambda: {"source": "demo"})
    assert "category_support" not in svc.context_pack()
    monkeypatch.setattr(svc, "league_overview", lambda: {"source": "yahoo"})
    support = svc.context_pack()["category_support"]
    conn.close()
    assert support["status"] == "FAIL" and support["ignored"][0]["name"] == "DD"
