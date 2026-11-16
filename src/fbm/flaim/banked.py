"""Week-to-date ("banked") stats rebuilt from what actually counted: each day's lineup and the ESPN box scores.

FBM's simulator needs makes/attempts for FG% and FT% to add the rest of the week. Flaim normally sends Yahoo's own
display-only FGM/A and FTM/A rows ("246/499"), and then nothing is rebuilt. This is the fallback for when those rows
are missing or empty ("/") although the week is under way: for every ET day of the matchup so far, each player in
an active slot that day contributes his box-score line; bench and IL days don't count (Yahoo's rule). Yahoo's own
figures always win: counting stats come from Yahoo when reported, and rebuilt attempts are rescaled to Yahoo's
percentage. Pure functions; the caller supplies lineups and box-score rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from fbm.league.model import INACTIVE_SLOTS
from fbm.yahoo.league_reads import COUNT_STATS, banked_stats

COUNT_COLS = tuple(COUNT_STATS.values())  # tpm, pts, reb, ast, stl, blk, tov
MADE_ATT = (("fgm", "fga", "5", "FG%"), ("ftm", "fta", "8", "FT%"))  # makes, attempts, Yahoo pct stat id, label
MADE_ATT_IDS = {"fga": "9004003", "fta": "9007006"}  # Yahoo's display-only makes/attempts rows, by attempts column
TOTAL_COLS = (*COUNT_COLS, "fgm", "fga", "ftm", "fta")
COUNT_TOLERANCE = (3.0, 0.08)  # a counting stat "disagrees" beyond max(3, 8% of Yahoo's value)
PCT_TOLERANCE = 0.02  # rebuilt FG%/FT% this far from Yahoo's is worth a warning


def reconstruct(lineups: Mapping[str, Mapping[str, str]], lines: Mapping[str, Sequence[Any]]) -> dict[str, float]:
    """Team totals from box scores. `lineups`: {ET date: {player id: selected slot}}; `lines`: {player id: box-score
    rows with `et_date` and stat columns} (games played only). A line counts when its player sat in an active slot
    (not BN/IL/IL+/NA) on that date. Adds `games` (lines counted)."""
    totals = dict.fromkeys(TOTAL_COLS, 0.0)
    games = 0
    for pid, rows in lines.items():
        for row in rows:
            slot = lineups.get(str(row["et_date"]), {}).get(pid)
            if slot is None or slot.upper() in INACTIVE_SLOTS:
                continue
            for col in TOTAL_COLS:
                totals[col] += float(row[col] or 0)
            games += 1
    return {**totals, "games": float(games)}


def made_attempted(raw: str | None) -> bool:
    """A Yahoo makes/attempts value with numbers on both sides ("246/499", "0/0"). "/" or "-/-" (the row Yahoo
    shows before anything is banked) and a missing row are not."""
    if not raw or "/" not in raw:
        return False
    made, att = (x.strip() for x in raw.split("/", 1))
    try:
        float(made), float(att)
    except ValueError:
        return False
    return True


def reports_made_attempted(reported: Mapping[str, str]) -> bool:
    """Yahoo's own FGM/A and FTM/A are both in the reported week totals: no rebuild needed."""
    return all(made_attempted(reported.get(sid)) for sid in MADE_ATT_IDS.values())


def _pct(raw: str | None) -> float | None:
    try:
        return float(raw) if raw not in (None, "", "-") else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Banked:
    stats: dict[str, float]  # engine columns, like fbm.yahoo.league_reads.banked_stats
    source: str  # yahoo | yahoo + box scores | box scores | yahoo (no makes/attempts)
    warnings: list[str] = field(default_factory=list)


def merge(reported: Mapping[str, str], rebuilt: Mapping[str, float] | None, label: str) -> Banked:
    """Yahoo's reported week totals (stat id -> raw value) completed by the box-score rebuild where Yahoo is silent."""
    yahoo = banked_stats(dict(reported))
    if rebuilt is None:
        has_ma = reports_made_attempted(reported)
        return Banked(yahoo, "yahoo" if has_ma else "yahoo (no makes/attempts)")
    for made, att, _, _ in MADE_ATT:
        if not made_attempted(reported.get(MADE_ATT_IDS[att])):  # "/" parses as 0/0: let the rebuild fill it
            yahoo.pop(made, None)
            yahoo.pop(att, None)
    stats = dict(yahoo)
    warnings: list[str] = []
    used = False
    for col in COUNT_COLS:
        if col not in stats:
            stats[col] = rebuilt[col]
            used = True
        elif abs(stats[col] - rebuilt[col]) > max(COUNT_TOLERANCE[0], COUNT_TOLERANCE[1] * abs(stats[col])):
            warnings.append(
                f"{label} {col.upper()}: Yahoo {stats[col]:g} vs box scores {rebuilt[col]:g} "
                "(games in progress, a missing box score or lineup day, or a stat correction)"
            )
    for made, att, pct_id, cat in MADE_ATT:
        if att in stats:
            continue  # Yahoo's own makes/attempts
        used = True
        pct, r_made, r_att = _pct(reported.get(pct_id)), rebuilt[made], rebuilt[att]
        if pct is None or r_att <= 0:
            stats[made], stats[att] = r_made, r_att
            continue
        stats[made], stats[att] = round(pct * r_att, 2), r_att
        if abs(r_made / r_att - pct) > PCT_TOLERANCE:
            warnings.append(
                f"{label} {cat}: Yahoo {pct:.3f} vs box scores {r_made / r_att:.3f} on {r_att:g} attempts "
                "(attempts kept, makes set to Yahoo's percentage)"
            )
    if not any(col in yahoo for col in COUNT_COLS):
        source = "box scores"
    else:
        source = "yahoo + box scores" if used else "yahoo"
    return Banked(stats, source, warnings)
