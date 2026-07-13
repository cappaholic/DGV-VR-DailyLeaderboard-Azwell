#!/usr/bin/env python3
"""
DGV VR Rating Calculator — True PDGA SSA Method
Runs every Sunday night via GitHub Actions (Monday 00:30 UTC).
Reads public/history.json and public/flagged.json,
computes DGV VR Ratings for all non-flagged players,
writes public/ratings.json.

Rating formula (true PDGA method, no par anchor):
  SSA = average raw score of propagators who played that day
  Round Rating = 1000 + (SSA - playerScore) x RATING_PTS
  Player Rating = weighted rolling average of round ratings

Rolling window (mirrors PDGA):
  Primary:  90 days from player's most recent round
  Fallback: 180 days if fewer than 8 rounds in primary window
"""

import json, math, statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Constants (must match index.html exactly) ─────────────────────────────────
RATING_PTS                 = 8.0    # pts/stroke — calibrated against the all-time top 175
                                     # PDGA "Event Ratings" (avg round rating per tournament).
                                     # Lands DGV VR's most dominant players (~1064-1066) at the
                                     # median of that list (1066.5); best individual rounds stay
                                     # under the single best event rating ever recorded (1088).
RATING_PROPAGATOR_MIN_DAYS = 8      # min rounds to be a propagator (PDGA standard)
RATING_MIN_PROPAGATORS     = 3      # min propagators needed to compute SSA
RATING_MIN_ROUNDS          = 1      # min rounds to display a rating
RATING_PROVISIONAL_ROUNDS  = 8      # min rounds_counted for Top-N leaderboard eligibility
                                     # (no longer controls the "provisional" tag itself —
                                     # see load_previously_seen / is_provisional below)

DATA_CUTOFF_DATE           = '2026-07-03'  # only post-cutoff data used in calculations

RATING_WINDOW_DAYS         = 90     # primary rolling window
RATING_WINDOW_FALLBACK     = 180    # fallback if < 8 rounds in primary window
RATING_WINDOW_MIN_ROUNDS   = 8      # threshold that triggers fallback

# ── Paths ─────────────────────────────────────────────────────────────────────
PUBLIC_DIR   = Path(__file__).parent / "public"
HISTORY_FILE = PUBLIC_DIR / "history.json"
FLAGGED_FILE = PUBLIC_DIR / "flagged.json"
RATINGS_FILE = PUBLIC_DIR / "ratings.json"


def load_flagged(path: Path) -> set:
    try:
        data = json.loads(path.read_text())
        names = data.get('players', []) if isinstance(data, dict) else data
        return {n.lower() for n in names}
    except Exception:
        return set()


def is_flagged(name: str, flagged: set) -> bool:
    return name.lower() in flagged


def build_propagators(full_history: list, flagged: set) -> dict:
    """
    Career average RAW SCORE per propagator, using FULL history (pre+post cutoff).
    This maximises the propagator pool. Par is never used.
    """
    totals, counts = {}, {}
    for day in full_history:
        for p in day['players']:
            name = p['name']
            if is_flagged(name, flagged):
                continue
            totals[name] = totals.get(name, 0) + p['score']
            counts[name] = counts.get(name, 0) + 1
    return {
        name: totals[name] / counts[name]
        for name, count in counts.items()
        if count >= RATING_PROPAGATOR_MIN_DAYS
    }


def build_daily_ssa(history: list, propagators: dict, flagged: set) -> dict:
    """
    SSA for each day = average raw score of propagators who played that day.
    Uses post-cutoff history only for actual scores.
    Fallback: trimmed mean of best 60% of scores when propagators are insufficient.
    Par is never used.
    """
    ssa = {}
    for day in history:
        props = [p for p in day['players']
                 if not is_flagged(p['name'], flagged) and p['name'] in propagators]
        if len(props) < RATING_MIN_PROPAGATORS:
            # Fallback: trimmed mean — best 60% of scores, robust against outliers
            scores = sorted(p['score'] for p in day['players'])
            trim_n = max(1, int(len(scores) * 0.60))
            trimmed = scores[:trim_n]
            ssa[day['date']] = sum(trimmed) / len(trimmed) if trimmed else None
        else:
            ssa[day['date']] = sum(p['score'] for p in props) / len(props)
    return ssa


def compute_round_rating(player_score: float, ssa: float) -> float:
    return 1000 + (ssa - player_score) * RATING_PTS


def apply_window(rounds: list) -> tuple:
    """Filter to rolling window anchored to player's most recent round."""
    if not rounds:
        return rounds, False
    last_date = datetime.strptime(rounds[-1]['date'], '%Y-%m-%d').date()
    primary_cutoff  = (last_date - timedelta(days=RATING_WINDOW_DAYS)).isoformat()
    fallback_cutoff = (last_date - timedelta(days=RATING_WINDOW_FALLBACK)).isoformat()

    primary = [r for r in rounds if r['date'] >= primary_cutoff]
    if len(primary) >= RATING_WINDOW_MIN_ROUNDS:
        return primary, False

    fallback = [r for r in rounds if r['date'] >= fallback_cutoff]
    if len(fallback) >= RATING_MIN_ROUNDS:
        return fallback, True

    return rounds, False


def compute_rolling_rating(round_ratings: list) -> float | None:
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


def load_previously_seen(path: Path) -> set:
    """
    Players who appeared in ANY previous ratings.json run, regardless of
    round count at the time. Mirrors real PDGA behavior: a new member gets
    a Preliminary Rating the moment they play, which becomes Official the
    moment the next scheduled ratings update includes them — permanently,
    with no round-count minimum to "graduate." The 8-round threshold still
    gates Top-N leaderboard eligibility (via rounds_counted), but no longer
    controls the "provisional" display tag itself.
    """
    try:
        data = json.loads(path.read_text())
        return set(data.get('players', {}).keys())
    except Exception:
        return set()


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] DGV VR Rating Calculator starting...")

    history_all      = json.loads(HISTORY_FILE.read_text())
    flagged          = load_flagged(FLAGGED_FILE)
    previously_seen  = load_previously_seen(RATINGS_FILE)
    print(f"  History days (total):           {len(history_all)}")
    print(f"  Flagged players:                {len(flagged)}")
    print(f"  Previously calculated players:  {len(previously_seen)}")

    # Apply data cutoff
    history = [d for d in history_all if d['date'] >= DATA_CUTOFF_DATE]
    print(f"  History days (post-cutoff):     {len(history)}")

    if not history:
        output = {
            "generated":    datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "generated_ts": datetime.now(timezone.utc).isoformat(),
            "cutoff_date":  DATA_CUTOFF_DATE,
            "method":       "SSA-anchored, 8.0pts/stroke — no par anchor (true PDGA method)",
            "propagators":  0,
            "players":      {},
        }
        RATINGS_FILE.write_text(json.dumps(output, separators=(",", ":")))
        print("  No post-cutoff data yet — empty ratings.json written.")
        return

    # Propagators from FULL history, daily SSA from post-cutoff only
    propagators = build_propagators(history_all, flagged)
    daily_ssa   = build_daily_ssa(history, propagators, flagged)
    print(f"  Propagators (8+ rounds):        {len(propagators)}")
    print(f"  Days with SSA computed:         {sum(1 for v in daily_ssa.values() if v is not None)}")

    # Collect per-player round ratings (post-cutoff only)
    player_rounds: dict[str, list] = {}
    for day in history:
        ssa = daily_ssa.get(day['date'])
        if ssa is None:
            continue
        for p in day['players']:
            name = p['name']
            if is_flagged(name, flagged):
                continue
            rr = compute_round_rating(p['score'], ssa)
            if name not in player_rounds:
                player_rounds[name] = []
            player_rounds[name].append({
                "date":        day['date'],
                "score":       p['score'],
                "vsPar":       p['vsPar'],
                "ssa":         round(ssa, 3),
                "roundRating": round(rr),
            })

    # Compute player ratings using rolling window
    players_out = {}
    fallback_count = 0

    for name, all_rounds in player_rounds.items():
        windowed, used_fallback = apply_window(all_rounds)
        if used_fallback:
            fallback_count += 1

        rr_vals = [r['roundRating'] for r in windowed]
        rating  = compute_rolling_rating(rr_vals)
        if rating is None:
            continue

        # Provisional now means "this is the player's first-ever appearance
        # in an official ratings.json" — matching real PDGA behavior where a
        # new member's rating becomes Official the moment the next update
        # includes them, with no round-count minimum required to graduate.
        # The 8-round threshold (rounds_counted) still separately gates
        # Top-N leaderboard eligibility — see index.html for that check.
        is_provisional = name not in previously_seen

        players_out[name] = {
            "rating":         round(rating),
            "provisional":    is_provisional,
            "rounds_counted": len(windowed),
            "total_rounds":   len(all_rounds),
            "used_fallback":  used_fallback,
            "best_round":     max(rr_vals),
            "worst_round":    min(rr_vals),
            "last_played":    all_rounds[-1]['date'],
        }

    players_sorted = dict(
        sorted(players_out.items(), key=lambda x: x[1]['rating'], reverse=True)
    )

    output = {
        "generated":    datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_ts": datetime.now(timezone.utc).isoformat(),
        "cutoff_date":  DATA_CUTOFF_DATE,
        "method":       "SSA-anchored, 8.0pts/stroke — no par anchor (true PDGA method)",
        "window_days":  RATING_WINDOW_DAYS,
        "propagators":  len(propagators),
        "players":      players_sorted,
    }

    RATINGS_FILE.write_text(json.dumps(output, separators=(",", ":")))
    print(f"  Ratings written:                {len(players_sorted)} players → {RATINGS_FILE}")
    print(f"  Using fallback window ({RATING_WINDOW_FALLBACK}d):  {fallback_count} players")
    print(f"  Top 5:")
    for i, (name, d) in enumerate(list(players_sorted.items())[:5]):
        prov = " (provisional)" if d["provisional"] else ""
        print(f"    {i+1}. {name}: {d['rating']}{prov}")
    print("Done.")


if __name__ == "__main__":
    main()
