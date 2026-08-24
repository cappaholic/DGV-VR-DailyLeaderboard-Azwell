#!/usr/bin/env python3
"""
DGV VR Rating Calculator — True PDGA SSA Method (v2)
Runs every Sunday night via GitHub Actions (Monday 00:30 UTC).
Reads public/history.json and public/flagged.json,
computes DGV VR Ratings for all non-flagged players,
writes public/ratings.json.

Rating formula (true PDGA method, no par anchor):
  SSA = average raw score of propagators who played that day
  Round Rating = 1000 + (SSA - playerScore) x pts_per_stroke(SSA)
  Player Rating = weighted rolling average of round ratings

Rolling window (mirrors PDGA, scaled to our much shorter season):
  Primary:  30 days from player's most recent round
  Fallback: 60 days if fewer than 8 rounds in primary window

Propagators (mirrors real PDGA definition):
  8+ total rounds AND an established rating of 750+.
  Since "established rating" requires ratings to already exist, this is
  done as a two-pass calculation — identical in spirit to how PDGA uses
  a player's rating from BEFORE the round in question, never a rating
  computed circularly from the same data being rated:
    Pass 1: provisional ratings for every player with 8+ rounds, no
            rating floor yet, no per-propagator exclusion yet.
    Pass 2: propagators = 8+ rounds AND Pass-1 rating >= 750. Daily SSA
            is recomputed using this refined pool, this time applying
            the 60-point-below-own-rating exclusion rule (using each
            propagator's Pass-1 rating as their "established" rating).
            Final player ratings are computed from this Pass-2 SSA.

Points-per-stroke ("compression", mirrors real PDGA):
  PDGA does not publish an exact formula for this — only approximate
  reference points on 18-hole courses (~SSA 44/50.5/68 -> ~13/10/6
  pts/throw). Since our format is 9 holes with a much lower total par,
  those reference points are scaled proportionally to our own average
  post-cutoff total par before being used as interpolation anchors.
  This is a best-effort approximation of an unpublished system, not a
  guaranteed match to PDGA's actual internal numbers.
"""

import json, math, statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Constants (must match index.html exactly) ─────────────────────────────────
RATING_PROPAGATOR_MIN_DAYS = 8      # min rounds to be a propagator (PDGA standard)
RATING_PROPAGATOR_MIN_RATING = 750  # min established rating to be a propagator
                                     # (real PDGA uses 700; raised slightly per
                                     # Azwell's request since 700 currently
                                     # includes nearly the entire player pool)
PROPAGATOR_EXCLUSION_MARGIN = 60    # a propagator's round is excluded from that
                                     # day's SSA if it's more than this many
                                     # rating points below their own rating
                                     # (matches PDGA's documented rule exactly)

RATING_MIN_PROPAGATORS     = 2      # min propagators needed to compute SSA
                                     # (matches PDGA's documented minimum)
RATING_MIN_ROUNDS          = 3      # min rounds before a rating is calculated at all
                                     # (kept as-is; PDGA's real minimum is 1 round,
                                     # but this project intentionally keeps a
                                     # higher bar given our much shorter season)
RATING_PROVISIONAL_ROUNDS  = 7      # rounds_counted needed at a Sunday calculation to
                                     # permanently drop the "provisional" tag (also gates
                                     # Top-N leaderboard eligibility via the same flag)

DATA_CUTOFF_DATE           = '2026-07-03'  # only post-cutoff data used in calculations

RATING_WINDOW_DAYS         = 30     # primary rolling window (scaled down from PDGA's
                                     # 12-month window to fit our much shorter season —
                                     # a touring pro plays ~20-30 PDGA events/year, so
                                     # 30 days of DAILY play is a comparable rounds count)
RATING_WINDOW_FALLBACK     = 60     # fallback if < 8 rounds in primary window
RATING_WINDOW_MIN_ROUNDS   = 8      # threshold that triggers fallback

# ── Points-per-stroke compression curve (v3 — linear, data-derived) ──────────
# Derived from two REAL PDGA regression fits (not approximate blog numbers):
#   Echo Valley Open (B-Tier, moderate course): 7.33 pts/stroke, R²=0.9996
#   Big Easy Open (known hard course):          4.00 pts/stroke, R²=1.0
# These converge with the original 606-round baseline (7.42 pts/stroke) at
# the "moderate" end, giving strong confidence in ~7.4 as the typical value.
#
# The real measured relationship between difficulty (18-hole SSA) and
# pts/stroke is -0.2218 pts per SSA-stroke. Since our 9-hole format uses a
# much lower total par, this rate is scaled up by 1/PAR_SCALE_FACTOR before
# being applied to our own SSA scale (a DGV stroke represents a larger share
# of the whole round than an 18-hole stroke does).
#
# The curve is centered on DGV VR's own observed mean SSA — not a PDGA
# absolute SSA value, since DGV's actual scoring runs well below par
# relative to how PDGA's SSA relates to par, so absolute PDGA SSA values
# don't translate directly onto our scale. Clamped to [4.0, 13.0]: the
# floor is a confirmed real value (Big Easy Open); the ceiling is a
# conservative, still-unconfirmed safety cap that this gentler real slope
# essentially never reaches across the actually-observed SSA range.
#
# NOTE (future refinement, not yet implemented): hole composition (count of
# par-4/5 vs par-3 holes) shows a real but modest correlation (r≈0.3, ~9%
# of variance) with score spread, independent of SSA. Worth revisiting as
# a secondary input once more historical data accumulates (currently only
# 23 days) — not built in now to avoid overfitting a 2-variable model to
# a small sample.
PDGA_REFERENCE_PAR     = 54.0
OUR_AVERAGE_PAR        = 31.09   # average total par across post-cutoff history.json
PAR_SCALE_FACTOR       = OUR_AVERAGE_PAR / PDGA_REFERENCE_PAR

DGV_MEAN_SSA           = 26.28   # DGV VR's own observed mean daily SSA (measured)
COMPRESSION_CENTER_PTS = 7.4     # pts/stroke at DGV_MEAN_SSA (avg of Echo Valley's
                                  # 7.33 and the original 606-round baseline's 7.42)
COMPRESSION_SLOPE      = -0.2218 / PAR_SCALE_FACTOR  # real measured rate, rescaled
COMPRESSION_FLOOR      = 4.0     # confirmed real value (Big Easy Open)
COMPRESSION_CEILING    = 13.0    # conservative estimate, rarely reached


def pts_per_stroke(ssa: float) -> float:
    """
    Linear compression model derived from real PDGA regression data (Echo
    Valley Open + Big Easy Open), centered on DGV VR's own observed mean
    SSA rather than PDGA's absolute SSA scale.
    """
    raw = COMPRESSION_CENTER_PTS + COMPRESSION_SLOPE * (ssa - DGV_MEAN_SSA)
    return max(COMPRESSION_FLOOR, min(COMPRESSION_CEILING, raw))


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


def build_propagator_pool(full_history: list, flagged: set) -> dict:
    """
    Candidate propagators = 8+ total rounds (full history, pre+post cutoff),
    same as before. The rating-floor filter is applied separately in Pass 2,
    since it requires ratings that don't exist yet on the first pass.
    Career average RAW SCORE per candidate, for reference/logging only —
    actual SSA still comes from real per-day scores, never this average.
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


def build_daily_ssa_simple(history: list, propagators: dict, flagged: set) -> dict:
    """
    Pass 1 SSA: average raw score of candidate propagators who played that
    day. No per-propagator exclusion yet (no established ratings exist to
    compare against on this pass). Compression is still applied since it
    only depends on that day's own SSA, not on any player's rating.
    """
    ssa = {}
    for day in history:
        props = [p for p in day['players']
                 if not is_flagged(p['name'], flagged) and p['name'] in propagators]
        if len(props) < RATING_MIN_PROPAGATORS:
            scores = sorted(p['score'] for p in day['players'])
            trim_n = max(1, int(len(scores) * 0.60))
            trimmed = scores[:trim_n]
            ssa[day['date']] = sum(trimmed) / len(trimmed) if trimmed else None
        else:
            ssa[day['date']] = sum(p['score'] for p in props) / len(props)
    return ssa


def build_daily_ssa_final(history: list, propagators: set, prelim_ratings: dict,
                           flagged: set) -> dict:
    """
    Pass 2 SSA: uses the refined propagator pool (8+ rounds AND Pass-1
    rating >= RATING_PROPAGATOR_MIN_RATING). For each day:
      1. Compute a preliminary SSA from all qualifying propagators who
         played that day.
      2. Using that preliminary SSA's own compression rate, compute each
         propagator's implied round rating for their score that day.
      3. Exclude any propagator whose implied round rating is more than
         PROPAGATOR_EXCLUSION_MARGIN points below their OWN Pass-1 rating
         (matches PDGA's documented "60 points below their rating" rule).
      4. Recompute the final SSA from the remaining, non-excluded scores.
    Falls back to the trimmed-mean method if too few propagators remain
    at any stage (same fallback used elsewhere in this file).
    """
    ssa = {}
    for day in history:
        day_props = [p for p in day['players']
                     if not is_flagged(p['name'], flagged) and p['name'] in propagators]

        if len(day_props) < RATING_MIN_PROPAGATORS:
            scores = sorted(p['score'] for p in day['players'])
            trim_n = max(1, int(len(scores) * 0.60))
            trimmed = scores[:trim_n]
            ssa[day['date']] = sum(trimmed) / len(trimmed) if trimmed else None
            continue

        prelim_ssa = sum(p['score'] for p in day_props) / len(day_props)
        prelim_pts = pts_per_stroke(prelim_ssa)

        kept = []
        for p in day_props:
            implied_rr = 1000 + (prelim_ssa - p['score']) * prelim_pts
            own_rating = prelim_ratings.get(p['name'])
            if own_rating is not None and implied_rr < (own_rating - PROPAGATOR_EXCLUSION_MARGIN):
                continue  # excluded — round was a big outlier vs their own rating
            kept.append(p)

        if len(kept) < RATING_MIN_PROPAGATORS:
            # Exclusion left too few propagators — fall back to trimmed mean
            scores = sorted(p['score'] for p in day['players'])
            trim_n = max(1, int(len(scores) * 0.60))
            trimmed = scores[:trim_n]
            ssa[day['date']] = sum(trimmed) / len(trimmed) if trimmed else None
        else:
            ssa[day['date']] = sum(p['score'] for p in kept) / len(kept)

    return ssa


def compute_round_rating(player_score: float, ssa: float) -> float:
    return 1000 + (ssa - player_score) * pts_per_stroke(ssa)


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


def compute_all_ratings(history: list, daily_ssa: dict, flagged: set) -> dict:
    """
    Shared final step for both passes: given a day->SSA map, compute every
    player's round ratings, apply the rolling window, and return final
    player ratings. Used for both the Pass-1 (preliminary) and Pass-2
    (final) calculations so the two passes stay logically identical.
    """
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
            player_rounds.setdefault(name, []).append({
                "date":        day['date'],
                "score":       p['score'],
                "vsPar":       p['vsPar'],
                "ssa":         round(ssa, 3),
                "roundRating": round(rr),
            })

    ratings = {}
    for name, all_rounds in player_rounds.items():
        windowed, used_fallback = apply_window(all_rounds)
        rr_vals = [r['roundRating'] for r in windowed]
        rating  = compute_rolling_rating(rr_vals)
        if rating is None:
            continue
        ratings[name] = {
            "rating":         round(rating),
            "rounds_counted": len(windowed),
            "total_rounds":   len(all_rounds),
            "used_fallback":  used_fallback,
            "best_round":     max(rr_vals),
            "worst_round":    min(rr_vals),
            "last_played":    all_rounds[-1]['date'],
        }
    return ratings


def load_previously_graduated(path: Path) -> set:
    """
    Players who were already non-provisional in the last ratings.json run.
    Once a Sunday calculation finds a player with 7+ rounds_counted, they
    stay non-provisional permanently — even if a later week's rolling
    window happens to contain fewer than 7 rounds (e.g. an inactive
    stretch). This reads the ratings.json this run is about to overwrite,
    so "graduated" status persists across every future weekly calculation.
    """
    try:
        data = json.loads(path.read_text())
        players = data.get('players', {})
        return {name for name, d in players.items() if not d.get('provisional', True)}
    except Exception:
        return set()


def load_previous_ratings(path: Path) -> dict:
    """
    Each player's rating from the last ratings.json run, used to compute
    this week's point change (displayed as "(+4)"/"(-3)"/"(0)" on the
    site). Same read-before-overwrite pattern as load_previously_graduated
    — reads the file this run is about to replace. Returns {} on the
    very first run, or if a player is new (no prior rating to compare).
    """
    try:
        data = json.loads(path.read_text())
        players = data.get('players', {})
        return {name: d.get('rating') for name, d in players.items() if 'rating' in d}
    except Exception:
        return {}


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] DGV VR Rating Calculator (v2) starting...")

    history_all      = json.loads(HISTORY_FILE.read_text())
    flagged          = load_flagged(FLAGGED_FILE)
    graduated        = load_previously_graduated(RATINGS_FILE)
    previous_ratings = load_previous_ratings(RATINGS_FILE)
    print(f"  History days (total):           {len(history_all)}")
    print(f"  Flagged players:                {len(flagged)}")
    print(f"  Previously graduated players:   {len(graduated)}")
    print(f"  Players with a previous rating: {len(previous_ratings)}")

    history = [d for d in history_all if d['date'] >= DATA_CUTOFF_DATE]
    print(f"  History days (post-cutoff):     {len(history)}")

    if not history:
        output = {
            "generated":    datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "generated_ts": datetime.now(timezone.utc).isoformat(),
            "cutoff_date":  DATA_CUTOFF_DATE,
            "method":       "SSA-anchored, PDGA-style compression — no par anchor",
            "propagators":  0,
            "players":      {},
        }
        RATINGS_FILE.write_text(json.dumps(output, separators=(",", ":")))
        print("  No post-cutoff data yet — empty ratings.json written.")
        return

    # ── Pass 1: preliminary ratings, no rating floor on propagators yet ──────
    candidate_pool  = build_propagator_pool(history_all, flagged)
    prelim_ssa      = build_daily_ssa_simple(history, candidate_pool, flagged)
    prelim_ratings_full = compute_all_ratings(history, prelim_ssa, flagged)
    prelim_ratings  = {name: d["rating"] for name, d in prelim_ratings_full.items()}

    print(f"  Pass 1 — candidate propagators (8+ rounds): {len(candidate_pool)}")

    # ── Pass 2: refine propagator pool to 8+ rounds AND 750+ rating ──────────
    final_propagators = {
        name for name in candidate_pool
        if prelim_ratings.get(name, 0) >= RATING_PROPAGATOR_MIN_RATING
    }
    print(f"  Pass 2 — final propagators (8+ rounds, {RATING_PROPAGATOR_MIN_RATING}+ rating): {len(final_propagators)}")

    final_ssa = build_daily_ssa_final(history, final_propagators, prelim_ratings, flagged)
    print(f"  Days with final SSA computed:   {sum(1 for v in final_ssa.values() if v is not None)}")

    players_out = compute_all_ratings(history, final_ssa, flagged)
    fallback_count = sum(1 for d in players_out.values() if d["used_fallback"])

    for name, d in players_out.items():
        is_provisional = d["rounds_counted"] < RATING_PROVISIONAL_ROUNDS
        if name in graduated:
            is_provisional = False
        d["provisional"] = is_provisional

        prev_rating = previous_ratings.get(name)
        d["change"] = (d["rating"] - prev_rating) if prev_rating is not None else None

    players_sorted = dict(
        sorted(players_out.items(), key=lambda x: x[1]['rating'], reverse=True)
    )

    output = {
        "generated":      datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_ts":   datetime.now(timezone.utc).isoformat(),
        "cutoff_date":    DATA_CUTOFF_DATE,
        "method":         "SSA-anchored, PDGA-style compression — no par anchor",
        "window_days":    RATING_WINDOW_DAYS,
        "propagators":    len(final_propagators),
        "players":        players_sorted,
    }

    RATINGS_FILE.write_text(json.dumps(output, separators=(",", ":")))
    print(f"  Ratings written:                {len(players_sorted)} players → {RATINGS_FILE}")
    print(f"  Using fallback window ({RATING_WINDOW_FALLBACK}d):  {fallback_count} players")
    print(f"  Top 5:")
    for i, (name, d) in enumerate(list(players_sorted.items())[:5]):
        prov = " (provisional)" if d["provisional"] else ""
        chg  = f" ({'+' if d['change'] > 0 else ''}{d['change']})" if d['change'] is not None else " (new)"
        print(f"    {i+1}. {name}: {d['rating']}{chg}{prov}")
    print("Done.")


if __name__ == "__main__":
    main()
