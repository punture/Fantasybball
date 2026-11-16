"""Common-random-number bank of simulated single-game stat lines.

Each (player, key) gets its own deterministic RNG stream, so two candidate rosters that share players reuse the exact
same simulated games. A player's weekly form, return-day (injury) draws and rounding draws are per-player streams too.
Comparisons between candidates (streaming moves, trades) then reflect the roster change, not Monte Carlo noise.
"""

from __future__ import annotations

import hashlib
from collections.abc import Hashable, Iterable, Mapping

import numpy as np

from fbm.analytics.simulation import COL, STATS, PlayerModel, form_multiplier, round_counts, rounding_draws


class DrawBank:
    def __init__(self, models: Mapping[str, PlayerModel], n_sims: int = 2000, seed: int = 17):
        self.models = models
        self.n = n_sims
        self.seed = seed
        self._games: dict[tuple[str, Hashable], np.ndarray] = {}
        self._weeks: dict[tuple[str, int], np.ndarray] = {}
        self._forms: dict[str, np.ndarray] = {}
        self._backs: dict[str, np.ndarray] = {}
        self._rounding: dict[str, np.ndarray] = {}

    def _rng(self, pid: str, key: Hashable) -> np.random.Generator:
        digest = hashlib.blake2b(f"{pid}|{key}".encode(), digest_size=8).hexdigest()
        return np.random.default_rng([self.seed, int(digest, 16)])

    def _form(self, pid: str, model: PlayerModel) -> np.ndarray | None:
        """One weekly role/form multiplier per simulation, shared by all of the player's games (same stream for
        every candidate roster)."""
        if model.form_sd <= 0:
            return None
        arr = self._forms.get(pid)
        if arr is None:
            arr = form_multiplier(self._rng(pid, "__form__"), self.n, model.form_sd).astype(np.float32)
            self._forms[pid] = arr
        return arr

    def _back(self, pid: str) -> np.ndarray:
        """Return-day draws: one uniform per simulation shared by all of the player's games (absences are streaky)."""
        arr = self._backs.get(pid)
        if arr is None:
            arr = self._backs[pid] = self._rng(pid, "__return__").random(self.n)
        return arr

    def _round(self, pid: str) -> np.ndarray:
        """Rounding draws for the player's weekly line (same for every candidate roster)."""
        arr = self._rounding.get(pid)
        if arr is None:
            arr = self._rounding[pid] = rounding_draws(self._rng(pid, "__round__"), self.n, np.float32)
        return arr

    def game(self, pid: str, key: Hashable) -> np.ndarray:
        """Simulated stat lines for one game of `pid` (shape n x len(STATS)); zeros if the player sits.

        `key` is normally the game's ET date (enables the return-day process and back-to-back rest risk)."""
        cache_key = (pid, key)
        arr = self._games.get(cache_key)
        if arr is None:
            model = self.models.get(pid)
            if model is None or model.game_play_prob(key) <= 0:
                arr = np.zeros((self.n, len(STATS)), np.float32)
            else:
                rng = self._rng(pid, key)
                arr = model.samples[rng.integers(0, len(model.samples), self.n)].astype(np.float32)
                keep = model.play_mask(key, rng, self.n, self._back(pid))
                if keep is not None:
                    arr *= keep[:, None]
                form = self._form(pid, model)
                if form is not None:
                    arr *= form[:, None]
            self._games[cache_key] = arr
        return arr

    def total(
        self, player_games: Iterable[tuple[str, Hashable]], banked: Mapping[str, float] | None = None
    ) -> np.ndarray:
        """Team totals: each player's games summed and rounded to whole numbers with his own rounding draws, plus
        banked stats."""
        weeks: dict[str, np.ndarray] = {}
        for pid, key in player_games:
            g = self.game(pid, key)
            weeks[pid] = weeks[pid] + g if pid in weeks else g
        out = np.zeros((self.n, len(STATS)))
        for pid, lines in weeks.items():
            out += round_counts(lines, self._round(pid))
        for stat, value in (banked or {}).items():
            if stat in COL:
                out[:, COL[stat]] += value
        return out

    def generic_week(self, pid: str, games: int = 4) -> np.ndarray:
        """Sum of `games` independent games — a schedule-neutral week for season-level comparisons (not rounded:
        callers scale it by a start share)."""
        key = (pid, games)
        arr = self._weeks.get(key)
        if arr is None:
            arr = np.zeros((self.n, len(STATS)), np.float32)
            for i in range(games):
                arr += self.game(pid, ("generic", i))
            self._weeks[key] = arr
        return arr
