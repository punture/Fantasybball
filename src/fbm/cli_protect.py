"""`fbm protect`: the late-week protection advisor. Registered by importing this module from fbm.cli."""

from __future__ import annotations

import typer
from rich.markup import escape
from rich.table import Table

from fbm.cli import _svc, app, console


@app.command()
def protect(
    day: str = typer.Option(None, help="ET date (YYYY-MM-DD) the benching can start (default: today)."),
    opponent: str = typer.Option(None, help="Opponent team key or name (default: this week's opponent)."),
    top: int = typer.Option(3, help="How many recommended options to show."),
) -> None:
    """Late-week protection: should anyone sit to protect close FG%, FT% or TO? (FBM changes nothing.)"""
    from fbm.gm import services as svc

    r = _svc(svc.protect_plan, day, opponent, top)
    if "error_code" in r:  # HT_UNHEALTHY: no suggestions while Hashtag data is unhealthy (no fallback)
        console.print(f"[red]{escape(str(r.get('message', r['error_code'])))}[/]")
        for key in ("reasons", "fix"):
            items = r.get(key) or []
            for line in [items] if isinstance(items, str) else items:
                console.print(f"- {escape(str(line))}")
        return
    base = r["baseline"]
    console.print(
        f"[bold]{r['window_et']}[/] vs {escape(r['opponent'])} · benching from {r['actionable_from_et']} · "
        f"expected cats {base['expected_cats']:.2f} · P(win week) {base['p_win_week']:.0%} · "
        f"objective {r['objective']}"
    )
    close = r["contested_categories"]
    console.print(
        f"Contested now: FG%/FT%/TO {', '.join(close['ratio_to']) or 'none'} · "
        f"counting {', '.join(close['counting']) or 'none'}"
    )
    if r["actions"]:
        table = Table(title="Protection options")
        for col in ("Bench", "+Exp cats", "+P(win wk)", "Games lost", "Why"):
            table.add_column(col)
        for a in r["actions"]:
            table.add_row(
                escape(", ".join(a["players"])) if a["kind"] != "sit_all" else "everyone (protect mode)",
                f"{a['gain_expected_cats']:+.2f}",
                f"{a['gain_p_win_week']:+.0%}",
                str(a["games_lost"]),
                escape(a["reason"]),
            )
        console.print(table)
    console.print(f"[bold]{escape(r['recommendation'])}[/]")
    if r["actions"]:
        console.print("Steps (you make them in Yahoo):")
        for step in r["actions"][0]["steps"]:
            console.print(f"  - {escape(step)}")
    if r["volume"]:
        console.print(escape(r["volume"]))
    if r["locked_game_started"]:
        console.print(f"[dim]Locked (game started): {escape(', '.join(r['locked_game_started']))}[/]")
    for note in r["notes"]:
        console.print(f"[dim]{escape(note)}[/]")
