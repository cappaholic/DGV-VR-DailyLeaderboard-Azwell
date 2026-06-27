#!/usr/bin/env python3
"""
DGV VR Rating Calculator
Runs every Monday via GitHub Actions.
Reads public/history.json and public/flagged.json,
computes DGV VR Ratings for all non-flagged players,
writes public/ratings.json.
"""

import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path

# ── Constants (must match index.html) ────────────────────────────────────────
RATING_PTS                 = 4.5   # fixed pts/stroke
PROPAGATOR_MIN_DAYS        = 5     # min appearances to be a propagator
MIN_PROPAGATORS            = 3     # min propagators needed to trust difficulty offset
RATING_MIN_ROUNDS          = 2     # min rounds to display a player rating
RATING_PROVISIONAL_ROUNDS  = 8     # rounds before provisional badge drops

# ── Paths ─────────────────────────────────────────────────────────────────────
PUBLIC_DIR   = Path(__file__).parent / "public"
HISTORY_FILE = PUBLIC_DIR / "history.json"
FLAGGED_FILE = PUBLIC_DIR / "flagged.json"
RATINGS_FILE = PUBLIC_DIR / "ratings.json"


def load_flagged(path: Path) -> set:
    """Return a lowercase set of flagged player names."""
    try:
        data = json.loads(path.read_text())
        return {n.lower() for n in (data if isinstance(data, list) else [])}
    except Exception:
        return set()


def is_flagged(name: str, flagged: set) -> bool:
    return name.lower() in flagged


def build_propagators(history: list, flagged: set) -> dict:
    """Players with PROPAGATOR_MIN_DAYS+ appearances → their career avg vsPar."""
    totals = {}
    counts = {}
    for day in history:
        for p in day["players"]:
            name = p["name"]
            if is_flagged(name, flagged):
                continue
            totals[name] = totals.get(name, 0) + p["vsPar"]
            counts[name] = counts.get(name, 0) + 1
    return {
        name: totals[name] / counts[name]
        for name, count in counts.items()
        if count >= PROPAGATOR_MIN_DAYS
    }


def build_daily_difficulty(history: list, propagators: dict, flagged: set) -> dict:
    """Per-day difficulty offset derived from propagator performance."""
    difficulty = {}
    for day in history:
        props = [
            p for p in day["players"]
            if not is_flagged(p["name"], flagged) and p["name"] in propagators
        ]
        if len(props) < MIN_PROPAGATORS:
            difficulty[day["date"]] = 0.0
            continue
        offsets = [p["vsPar"] - propagators[p["name"]] for p in props]
        difficulty[day["date"]] = sum(offsets) / len(offsets)
    return difficulty


def compute_round_rating(vs_par: float, difficulty_offset: float) -> float:
    return 1000 - (vs_par - difficulty_offset) * RATING_PTS


def compute_rolling_rating(round_ratings: list) -> float | None:
    """Weighted rolling average with recent 25% double-weighted and outlier exclusion."""
    n = len(round_ratings)
    if n < RATING_MIN_ROUNDS:
        return None

    # Recent 25% double-weighted if 9+ rounds
    weights = [1.0] * n
    if n >= 9:
        cutoff = n - max(1, round(n * 0.25))
        for i in range(cutoff, n):
            weights[i] = 2.0

    sum_w  = sum(weights)
    w_avg  = sum(r * w for r, w in zip(round_ratings, weights)) / sum_w

    # Outlier exclusion: drop rounds >100pts below avg OR >2.5 std devs below avg
    if n >= 7:
        variance  = sum((r - w_avg) ** 2 for r in round_ratings) / n
        std       = math.sqrt(variance)
        threshold = min(100.0, 2.5 * std)
        filtered  = [(r, w) for r, w in zip(round_ratings, weights) if r >= w_avg - threshold]
        if len(filtered) >= RATING_MIN_ROUNDS:
            fw = sum(w for _, w in filtered)
            return sum(r * w for r, w in filtered) / fw

    return w_avg


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] DGV VR Rating Calculator starting...")

    # Load data
    history = json.loads(HISTORY_FILE.read_text())
    flagged = load_flagged(FLAGGED_FILE)
    print(f"  History days: {len(history)}")
    print(f"  Flagged players: {len(flagged)}")

    # Build propagators and daily difficulty
    propagators = build_propagators(history, flagged)
    difficulty  = build_daily_difficulty(history, propagators, flagged)
    print(f"  Propagators: {len(propagators)}")

    # Collect per-player round ratings
    player_rounds: dict[str, list] = {}
    for day in history:
        diff = difficulty.get(day["date"], 0.0)
        for p in day["players"]:
            name = p["name"]
            if is_flagged(name, flagged):
                continue
            rr = compute_round_rating(p["vsPar"], diff)
            if name not in player_rounds:
                player_rounds[name] = []
            player_rounds[name].append({
                "date":        day["date"],
                "vsPar":       p["vsPar"],
                "par":         day["par"],
                "roundRating": round(rr),
                "difficulty":  round(diff, 4),
            })

    # Compute player ratings
    players_out = {}
    for name, rounds in player_rounds.items():
        rr_vals = [r["roundRating"] for r in rounds]
        rating  = compute_rolling_rating(rr_vals)
        if rating is None:
            continue

        included = [r for r in rr_vals if r >= min(rr_vals)]  # all after outlier logic
        players_out[name] = {
            "rating":           round(rating),
            "provisional":      len(rounds) < RATING_PROVISIONAL_ROUNDS,
            "rounds_counted":   len(rounds),
            "best_round":       max(rr_vals),
            "worst_round":      min(rr_vals),
            "last_played":      rounds[-1]["date"],
        }

    # Sort by rating descending for readability
    players_sorted = dict(
        sorted(players_out.items(), key=lambda x: x[1]["rating"], reverse=True)
    )

    output = {
        "generated":   datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_ts": datetime.now(timezone.utc).isoformat(),
        "propagators": len(propagators),
        "players":     players_sorted,
    }

    RATINGS_FILE.write_text(json.dumps(output, separators=(",", ":")))
    print(f"  Ratings written: {len(players_sorted)} players → {RATINGS_FILE}")
    print(f"  Top 5:")
    for i, (name, d) in enumerate(list(players_sorted.items())[:5]):
        prov = " (provisional)" if d["provisional"] else ""
        print(f"    {i+1}. {name}: {d['rating']}{prov}")
    print("Done.")


if __name__ == "__main__":
    main()
