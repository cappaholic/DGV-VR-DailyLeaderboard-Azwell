#!/usr/bin/env python3
"""
DGV VR Rating Calculator
Runs every Monday via GitHub Actions.
Reads public/history.json and public/flagged.json,
computes DGV VR Ratings for all non-flagged players,
writes public/ratings.json.

Rolling window methodology (mirrors PDGA):
  - Primary window: 90 days back from player's most recent round
  - Fallback window: 180 days if fewer than 8 rounds in primary window
  - All calculations restricted to DATA_CUTOFF_DATE or later
  - Propagator averages and daily difficulty use only post-cutoff data
  - Only the per-player rating window is further restricted to 90/180 days
"""

import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Constants (must match index.html) ────────────────────────────────────────
RATING_PTS                 = 4.5   # fixed pts/stroke
PROPAGATOR_MIN_DAYS        = 8     # min rounds to be a propagator (PDGA standard)
MIN_PROPAGATORS            = 3     # min propagators needed to trust difficulty offset
RATING_MIN_ROUNDS          = 1     # min rounds to display a rating (PDGA gives rating after 1 round)
RATING_PROVISIONAL_ROUNDS  = 8     # rounds before provisional badge drops + Top 50 eligibility

# Data cutoff — only rounds on/after this date count toward any calculation
DATA_CUTOFF_DATE           = '2026-07-03'

# Rolling window (PDGA equivalent: 12 months → 90 days for daily DGV VR play)
RATING_WINDOW_DAYS         = 90    # primary window
RATING_WINDOW_FALLBACK     = 180   # fallback if < 8 rounds in primary window
RATING_WINDOW_MIN_ROUNDS   = 8     # threshold that triggers fallback

# ── Paths ─────────────────────────────────────────────────────────────────────
PUBLIC_DIR   = Path(__file__).parent / "public"
HISTORY_FILE = PUBLIC_DIR / "history.json"
FLAGGED_FILE = PUBLIC_DIR / "flagged.json"
RATINGS_FILE = PUBLIC_DIR / "ratings.json"


def load_flagged(path: Path) -> set:
    """Return a lowercase set of flagged player names."""
    try:
        data = json.loads(path.read_text())
        return {n.lower() for n in (data.get('players', []) if isinstance(data, dict) else data)}
    except Exception:
        return set()


def is_flagged(name: str, flagged: set) -> bool:
    return name.lower() in flagged


def build_propagators(history: list, flagged: set) -> dict:
    """Players with PROPAGATOR_MIN_DAYS+ appearances → their career avg vsPar.
    Uses only post-cutoff history for accuracy."""
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


def apply_window(rounds: list, window_days: int, fallback_days: int) -> tuple[list, bool]:
    """Filter rounds to the rolling window anchored to the player's most recent round."""
    if not rounds:
        return rounds, False

    last_date = datetime.strptime(rounds[-1]["date"], "%Y-%m-%d").date()
    primary_cutoff  = (last_date - timedelta(days=window_days)).isoformat()
    fallback_cutoff = (last_date - timedelta(days=fallback_days)).isoformat()

    primary = [r for r in rounds if r["date"] >= primary_cutoff]
    if len(primary) >= RATING_WINDOW_MIN_ROUNDS:
        return primary, False

    fallback = [r for r in rounds if r["date"] >= fallback_cutoff]
    if len(fallback) >= RATING_MIN_ROUNDS:
        return fallback, True

    return rounds, False


def compute_rolling_rating(round_ratings: list) -> float | None:
    """Weighted rolling average with recent 25% double-weighted and outlier exclusion."""
    n = len(round_ratings)
    if n < RATING_MIN_ROUNDS:
        return None

    weights = [1.0] * n
    if n >= 9:
        cutoff = n - max(1, round(n * 0.25))
        for i in range(cutoff, n):
            weights[i] = 2.0

    sum_w = sum(weights)
    w_avg = sum(r * w for r, w in zip(round_ratings, weights)) / sum_w

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

    history_all = json.loads(HISTORY_FILE.read_text())
    flagged     = load_flagged(FLAGGED_FILE)
    print(f"  History days (total):    {len(history_all)}")
    print(f"  Flagged players:         {len(flagged)}")

    # Apply data cutoff — only post-cutoff entries count toward any calculation
    history = [d for d in history_all if d["date"] >= DATA_CUTOFF_DATE]
    print(f"  History days (post-cutoff {DATA_CUTOFF_DATE}): {len(history)}")

    if not history:
        output = {
            "generated":    datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "generated_ts": datetime.now(timezone.utc).isoformat(),
            "cutoff_date":  DATA_CUTOFF_DATE,
            "window_days":  RATING_WINDOW_DAYS,
            "propagators":  0,
            "players":      {},
        }
        RATINGS_FILE.write_text(json.dumps(output, separators=(",", ":")))
        print("  No post-cutoff data yet — empty ratings.json written.")
        return

    propagators = build_propagators(history, flagged)
    difficulty  = build_daily_difficulty(history, propagators, flagged)
    print(f"  Propagators:             {len(propagators)}")

    # Collect per-player round ratings (post-cutoff only)
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

    # Compute player ratings using rolling window
    players_out = {}
    fallback_count = 0

    for name, all_rounds in player_rounds.items():
        windowed, used_fallback = apply_window(
            all_rounds, RATING_WINDOW_DAYS, RATING_WINDOW_FALLBACK
        )
        if used_fallback:
            fallback_count += 1

        rr_vals = [r["roundRating"] for r in windowed]
        rating  = compute_rolling_rating(rr_vals)
        if rating is None:
            continue

        players_out[name] = {
            "rating":           round(rating),
            "provisional":      len(windowed) < RATING_PROVISIONAL_ROUNDS,
            "rounds_counted":   len(windowed),
            "total_rounds":     len(all_rounds),
            "used_fallback":    used_fallback,
            "best_round":       max(rr_vals),
            "worst_round":      min(rr_vals),
            "last_played":      all_rounds[-1]["date"],
        }

    players_sorted = dict(
        sorted(players_out.items(), key=lambda x: x[1]["rating"], reverse=True)
    )

    output = {
        "generated":    datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_ts": datetime.now(timezone.utc).isoformat(),
        "cutoff_date":  DATA_CUTOFF_DATE,
        "window_days":  RATING_WINDOW_DAYS,
        "propagators":  len(propagators),
        "players":      players_sorted,
    }

    RATINGS_FILE.write_text(json.dumps(output, separators=(",", ":")))
    print(f"  Ratings written:         {len(players_sorted)} players → {RATINGS_FILE}")
    print(f"  Using fallback window ({RATING_WINDOW_FALLBACK}d): {fallback_count} players")
    print(f"  Top 5:")
    for i, (name, d) in enumerate(list(players_sorted.items())[:5]):
        prov = " (provisional)" if d["provisional"] else ""
        print(f"    {i+1}. {name}: {d['rating']}{prov}")
    print("Done.")


if __name__ == "__main__":
    main()

"""
DGV VR Rating Calculator
Runs every Monday via GitHub Actions.
Reads public/history.json and public/flagged.json,
computes DGV VR Ratings for all non-flagged players,
writes public/ratings.json.

Rolling window methodology (mirrors PDGA):
  - Primary window: 90 days back from player's most recent round
  - Fallback window: 180 days if fewer than 8 rounds in primary window
  - Propagator averages use ALL history (full career) for accuracy
  - Daily difficulty offsets use ALL history (full career) for accuracy
  - Only the per-player rating window is restricted to 90/180 days
"""

import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Constants (must match index.html) ────────────────────────────────────────
RATING_PTS                 = 4.5   # fixed pts/stroke
PROPAGATOR_MIN_DAYS        = 8     # min rounds to be a propagator (PDGA standard)
MIN_PROPAGATORS            = 3     # min propagators needed to trust difficulty offset
RATING_MIN_ROUNDS          = 1     # min rounds to display a rating (PDGA gives rating after 1 round)
RATING_PROVISIONAL_ROUNDS  = 8     # rounds before provisional badge drops + Top 50 eligibility

# Rolling window (PDGA equivalent: 12 months → 90 days for daily DGV VR play)
RATING_WINDOW_DAYS         = 90    # primary window
RATING_WINDOW_FALLBACK     = 180   # fallback if < 8 rounds in primary window
RATING_WINDOW_MIN_ROUNDS   = 8     # threshold that triggers fallback

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
    """Players with PROPAGATOR_MIN_DAYS+ appearances → their career avg vsPar.
    Uses full history (not windowed) for maximum accuracy."""
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
    """Per-day difficulty offset derived from propagator performance.
    Uses full history (not windowed) so all daily offsets are available."""
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


def apply_window(rounds: list, window_days: int, fallback_days: int) -> tuple[list, bool]:
    """
    Filter rounds to the rolling window anchored to the player's most recent round.
    Returns (windowed_rounds, used_fallback).

    Mirrors PDGA logic:
      - Start with primary window (90 days back from last round)
      - If fewer than RATING_WINDOW_MIN_ROUNDS exist, extend to fallback (180 days)
      - If still fewer than RATING_MIN_ROUNDS, use all available rounds
    """
    if not rounds:
        return rounds, False

    last_date = datetime.strptime(rounds[-1]["date"], "%Y-%m-%d").date()
    primary_cutoff  = (last_date - timedelta(days=window_days)).isoformat()
    fallback_cutoff = (last_date - timedelta(days=fallback_days)).isoformat()

    primary = [r for r in rounds if r["date"] >= primary_cutoff]

    if len(primary) >= RATING_WINDOW_MIN_ROUNDS:
        return primary, False

    fallback = [r for r in rounds if r["date"] >= fallback_cutoff]
    if len(fallback) >= RATING_MIN_ROUNDS:
        return fallback, True

    # Not enough data in either window — use everything available
    return rounds, False


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

    sum_w = sum(weights)
    w_avg = sum(r * w for r, w in zip(round_ratings, weights)) / sum_w

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
    print(f"  History days:    {len(history)}")
    print(f"  Flagged players: {len(flagged)}")

    # Build propagators and daily difficulty using FULL history
    propagators = build_propagators(history, flagged)
    difficulty  = build_daily_difficulty(history, propagators, flagged)
    print(f"  Propagators:     {len(propagators)}")

    # Collect ALL per-player round ratings (full history, pre-windowing)
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

    # Compute player ratings using rolling window
    players_out = {}
    fallback_count = 0

    for name, all_rounds in player_rounds.items():
        # Apply rolling window (90 days primary, 180 days fallback)
        windowed, used_fallback = apply_window(
            all_rounds, RATING_WINDOW_DAYS, RATING_WINDOW_FALLBACK
        )
        if used_fallback:
            fallback_count += 1

        rr_vals = [r["roundRating"] for r in windowed]
        rating  = compute_rolling_rating(rr_vals)
        if rating is None:
            continue

        players_out[name] = {
            "rating":          round(rating),
            "provisional":     len(windowed) < RATING_PROVISIONAL_ROUNDS,
            "rounds_counted":  len(windowed),
            "rounds_in_window": len(windowed),
            "total_rounds":    len(all_rounds),
            "used_fallback":   used_fallback,
            "best_round":      max(rr_vals),
            "worst_round":     min(rr_vals),
            "last_played":     all_rounds[-1]["date"],
        }

    # Sort by rating descending for readability
    players_sorted = dict(
        sorted(players_out.items(), key=lambda x: x[1]["rating"], reverse=True)
    )

    output = {
        "generated":    datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_ts": datetime.now(timezone.utc).isoformat(),
        "window_days":  RATING_WINDOW_DAYS,
        "propagators":  len(propagators),
        "players":      players_sorted,
    }

    RATINGS_FILE.write_text(json.dumps(output, separators=(",", ":")))
    print(f"  Ratings written: {len(players_sorted)} players → {RATINGS_FILE}")
    print(f"  Using fallback window ({RATING_WINDOW_FALLBACK}d): {fallback_count} players")
    print(f"  Top 5:")
    for i, (name, d) in enumerate(list(players_sorted.items())[:5]):
        prov = " (provisional)" if d["provisional"] else ""
        fb   = " [fallback window]" if d["used_fallback"] else ""
        print(f"    {i+1}. {name}: {d['rating']}{prov}{fb}")
    print("Done.")


if __name__ == "__main__":
    main()
