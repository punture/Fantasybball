"""Scoring categories. Defaults to standard 9-cat; can be derived from Yahoo league settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Category:
    key: str  # internal key, e.g. "pts", "fg_pct"
    label: str  # display label, e.g. "PTS", "FG%"
    kind: str  # "count" | "pct"
    stat: str  # per-game stat column for counting cats
    made: str = ""  # pct cats: makes column
    att: str = ""  # pct cats: attempts column
    higher_is_better: bool = True
    yahoo_stat_id: str = ""


FG_PCT = Category("fg_pct", "FG%", "pct", "", "fgm", "fga", True, "5")
FT_PCT = Category("ft_pct", "FT%", "pct", "", "ftm", "fta", True, "8")
TPM = Category("tpm", "3PM", "count", "tpm", yahoo_stat_id="10")
PTS = Category("pts", "PTS", "count", "pts", yahoo_stat_id="12")
REB = Category("reb", "REB", "count", "reb", yahoo_stat_id="15")
AST = Category("ast", "AST", "count", "ast", yahoo_stat_id="16")
STL = Category("stl", "STL", "count", "stl", yahoo_stat_id="17")
BLK = Category("blk", "BLK", "count", "blk", yahoo_stat_id="18")
TOV = Category("tov", "TO", "count", "tov", higher_is_better=False, yahoo_stat_id="19")

NINE_CAT: tuple[Category, ...] = (FG_PCT, FT_PCT, TPM, PTS, REB, AST, STL, BLK, TOV)
_BY_YAHOO_ID = {c.yahoo_stat_id: c for c in NINE_CAT}
_ALIASES = {c.label.lower(): c for c in NINE_CAT} | {c.key: c for c in NINE_CAT} | {"3ptm": TPM, "to": TOV, "st": STL}

BASE_STATS = ("minutes", "pts", "reb", "ast", "stl", "blk", "tov", "tpm", "tpa", "fgm", "fga", "ftm", "fta")


# Display-only makes/attempts stats Yahoo shows next to FG%/FT% (never scored; FBM models the percentages).
_COMPONENT_STAT_IDS = frozenset({"9004003", "9007006"})
SUPPORTED_LABELS = ", ".join(c.label for c in NINE_CAT)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes")


def _them(n: int) -> str:
    return "it" if n == 1 else "them"


@dataclass(frozen=True)
class CategorySupport:
    """How the league's Yahoo scoring categories map onto the categories FBM can model."""

    categories: tuple[Category, ...]  # what FBM models (standard 9-cat when nothing maps)
    ignored: tuple[dict[str, Any], ...] = ()  # league categories FBM can't model: {stat_id, name, display_only}
    assumed_nine_cat: bool = False  # nothing mapped: FBM assumed standard 9-cat
    settings_read: bool = True  # False = no league settings available yet

    @property
    def scored_ignored(self) -> tuple[dict[str, Any], ...]:
        return tuple(c for c in self.ignored if not c["display_only"])

    @property
    def status(self) -> str:
        """FAIL: the league scores a category FBM can't model. WARN: display-only extras or an assumed 9-cat."""
        if self.scored_ignored:
            return "FAIL"
        return "WARN" if self.ignored or self.assumed_nine_cat else "OK"

    @property
    def message(self) -> str:
        parts = []
        scored = [c["name"] for c in self.scored_ignored]
        if scored:
            it = _them(len(scored))
            parts.append(
                f"Your league scores {', '.join(scored)}, which FBM can't model: FBM ignores {it}, so its odds, "
                f"rankings and advice don't account for {it}. Weigh {it} yourself."
            )
        shown = [c["name"] for c in self.ignored if c["display_only"]]
        if shown:
            parts.append(f"Ignored display-only stats (not scored): {', '.join(shown)}.")
        if not self.settings_read:
            parts.append("League settings not read yet: FBM assumes standard 9-cat until they are.")
        elif self.assumed_nine_cat:
            parts.append(f"No league category matched FBM's: FBM assumed standard 9-cat ({SUPPORTED_LABELS}).")
        if parts:
            return " ".join(parts)
        return f"All {len(self.categories)} scoring categories modeled: {', '.join(c.label for c in self.categories)}."

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "modeled": [c.label for c in self.categories],
            "ignored": [dict(c) for c in self.ignored],
            "assumed_9cat": self.assumed_nine_cat,
            "message": self.message,
        }


def category_support(summary: dict[str, Any] | None) -> CategorySupport:
    """Map Yahoo scoring categories (as in fbm.yahoo.settings.summarize) onto supported Category definitions and list
    the ones FBM can't model. Falls back to standard 9-cat when none map."""
    if summary is None:
        return CategorySupport(NINE_CAT, assumed_nine_cat=True, settings_read=False)
    cats: list[Category] = []
    ignored: list[dict[str, Any]] = []
    raw = summary.get("scoring_categories") if isinstance(summary, dict) else None
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        stat_id = str(c.get("stat_id", ""))
        if stat_id in _BY_YAHOO_ID:
            cats.append(_BY_YAHOO_ID[stat_id])
            continue
        if "enabled" in c and not _truthy(c["enabled"]):
            continue
        name = str(c.get("display_name") or c.get("name") or f"stat {stat_id}")
        display_only = _truthy(c.get("display_only")) or _truthy(c.get("is_only_display_stat"))
        if display_only and (stat_id in _COMPONENT_STAT_IDS or name.upper().endswith("M/A")):
            continue
        ignored.append({"stat_id": stat_id, "name": name, "display_only": display_only})
    return CategorySupport(tuple(cats) or NINE_CAT, tuple(ignored), assumed_nine_cat=not cats)


def from_league_settings(summary: dict[str, Any]) -> tuple[Category, ...]:
    """Map Yahoo scoring categories onto supported Category definitions. Unknown stats are ignored (listed by
    `category_support`, which `fbm settings`, `fbm health` and `context_pack` surface); none mapped = 9-cat."""
    return category_support(summary).categories


def parse_category_list(text: str | None) -> tuple[Category, ...]:
    """Parse e.g. 'FT%,TO' into categories (used for punts)."""
    if not text:
        return ()
    out = []
    for token in (t.strip().lower() for t in text.split(",")):
        if not token:
            continue
        if token not in _ALIASES:
            raise ValueError(f"Unknown category '{token}'. Use one of: {', '.join(c.label for c in NINE_CAT)}")
        out.append(_ALIASES[token])
    return tuple(out)
