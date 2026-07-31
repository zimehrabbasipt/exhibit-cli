# Exhibit investigation-DEPTH eval — reactive adversary, one deepening investigation

The [football eval](../eval_football/RESULTS.md) tested *breadth under context pressure*:
8 loosely-coupled questions in one long session. It did **not** test the core product
thesis — a user **refining, correcting, and branching a single investigation**, where each
turn depends on the last: does a correction propagate, does scope carry forward, does it
reuse prior evidence instead of silently recomputing, does the graph stay coherent.

This eval tests exactly that.

## Method

A **fresh adversary model** (Claude, no knowledge of Exhibit's internals) plays a demanding
senior analyst refining **one** investigation. It is rooted in a finding we already
ground-truthed — PL (GB1) home advantage inverted in the COVID-2020 season — and then
**drives every subsequent turn reactively**: each turn it is shown Exhibit's *actual*
previous conclusion and told to push on its weakest or most suspicious part (drill, filter,
branch, break down, **correct a weak definition the tool introduced**, and finally restate).
No turn was authored by me. Harness: [`scripts/eval_refinement.py`](../../scripts/eval_refinement.py).

Every dependent number was **independently recomputed in DuckDB** against the raw CSVs.

**Result: 7 reactive turns, 353 s, $0.90 (tool) + $0.11 (adversary). Every number exact.
Every correction propagated. The chain stayed coherent throughout.** One real weakness
(T2 self-check) and one product-shape gap (true branching), below.

## The investigation that emerged

| T | Move | Adversary targeted | What Exhibit did | Threaded under prior? |
|---|---|---|---|---|
| 1 | drill (seed) | — | Season rates 2016–2021 + attendance; confirmed 2020 inversion (home 37.89% < away 40.26%; attendance 39,315→5,086→39,870) | n/a (first) |
| 2 | branch/reframe | the 5,086 avg & empty-stadium claim | Split crowd vs empty; found the 31 crowd games keep normal home advantage (54.84%/29.03%) — **but flagged the empty bucket was inferred, not shown** | ✅ |
| 3 | drill (correction) | "you inferred it — show the empty bucket directly" | Ran the direct `<1000` query → **0 rows**, and correctly diagnosed *why*: empties are logged as NULL, not a sub-1000 value | ✅ |
| 4 | correct definition | "empties are NULL — redo with NULL as empty" | Re-split on `attendance IS NULL`: **349 empty (home 36.39% < away 41.26%)** vs 31 crowd; empty bucket ≈92% of season drives the inversion | ✅ |
| 5 | branch/reframe | "the 31 crowd games are just pre-lockdown fixtures" | Pulled dates and **refuted the adversary's own hypothesis**: empties start 12 Sep 2020, first crowd match 5 Dec 2020; crowd games fall in two later windows (14 in Dec, 17 in May) | ✅ |
| 6 | correct definition | selection bias + "don't use this season's results — use squad value / prior-season finish" | Point-in-time squad value (valuations dated **before** the season — no lookahead): only ~7% lean (€315.3m vs €294.9m); concluded too small to manufacture the effect — **but disclosed it did squad value only, deferring prior-season finish** | ✅ |
| 7 | correct definition (rerun) | "you dodged prior-season finish — do it" | Ranked hosts by 2019 finish: crowd 8.78 vs empty 9.02 avg; top-half 45.16% vs 50.43% → **no strong-team skew; crowd-effect interpretation survives** | ✅ |

## Refinement-specific scorecard

| Dimension | Verdict | Evidence |
|---|---|---|
| **Context carry** | ✅ strong | Scope (GB1, season 2020, the two buckets) held across all 7 turns without re-statement; T2–T7 each built on the prior turn's frame. |
| **Correction compliance** | ✅ (1 deferred, disclosed, then completed) | NULL-empty redefinition (T4) and prior-season-finish (T7) both **actually changed the computation and stuck**. T6 did one of two requested measures and *said so*; T7 finished the second when pushed. No correction was silently ignored. |
| **Scope integrity** | ✅ | No leakage — the PL filter, the 2020 window, and the crowd/empty exclusion never silently dropped across turns. |
| **Silent recompute** | ✅ none | No Bale-class error (recomputing the wrong population). Each turn recomputed *because the definition/filter changed* — correct behavior, not a regression. |
| **Definition drift** | ✅ none | Once corrected, `empty := attendance IS NULL` and `quality := prior-season finish` were applied consistently. |
| **Threading** | ✅ coherent (linear) | All 6 follow-ups threaded under the immediately-prior conclusion → a clean deepening chain. **Caveat below.** |
| **Statistical care** | ✅ notably strong | Point-in-time valuations avoid lookahead bias; observational-not-randomised caveats stated at T5/T6; promoted clubs correctly treated as non-2019-top-half at T7. |
| **Numeric correctness** | ✅ 7/7 exact | Every figure re-derived in DuckDB matched (T7's top-half %, where Exhibit's NULL-handling was *more* correct than my first independent cut). |

## Failure / weakness classification

Over 7 turns, **zero hard failures.** Two things worth naming:

1. **Self-check weakness (the one real miss) — T2.** It chose an attendance threshold
   (`<1000`) that could never populate the "empty" bucket, because empties are NULL. It
   **disclosed** the gap ("empty split is inferred rather than directly shown") rather than
   faking a number — but it took the adversary's push (T3) to fix it. A stronger system
   profiles the column's null-ness *before* bucketing and catches this itself on T2.
   Category: interpretation / self-verification, gracefully disclosed.

2. **Product-shape gap — true branching.** The engine threads every NL follow-up under the
   *latest* conclusion, so the investigation is a **linear deepening chain**. The adversary's
   "branch/reframe" turns (T2, T5) were reframes *along that line*, not sibling branches
   forking from an earlier node. Exploring two independent forks from one point (e.g. "from
   the T4 split, branch A: by referee; branch B: by scoreline") isn't reachable through
   conversation today — it needs explicit parent selection (a `/branch <node>` affordance).
   The data model supports it (`parent_id` is arbitrary); the REPL doesn't expose it. This is

## Why this is the stronger eval

The single most telling turn is **T5**: handed a plausible, confidently-worded wrong
hypothesis ("those 31 games are just pre-lockdown fixtures"), Exhibit didn't agree to please
the questioner — it pulled the dates and **refuted them with evidence**. Combined with T4
and T7 (corrections that measurably change the answer and persist), this is direct evidence
that the investigation substrate does what the thesis claims: it accumulates and revises a
*position*, not a transcript.

## Independent verification (all exact)

- **T1:** 2016 home 49.21% / 2019 att 39,315 / 2020 home 37.89% away 40.26% att 5,086 / 2021 home 42.89% away 33.95% att 39,870. ✅
- **T3:** 0 PL-2020 matches with attendance <1000. ✅
- **T4:** NULL bucket 349 games (home 36.39% / away 41.26%); non-NULL 31 (home 54.84% / away 29.03%). ✅
- **T5:** empties 2020-09-12 → 2021-05-23; first crowd 2020-12-05; 14 crowd games Dec 2020, 17 May 2021. ✅
- **T6:** crowd hosts avg €315.3m / median €261.3m; empty €294.9m / €251.6m (point-in-time, pre-season). ✅
- **T7:** crowd avg 2019 finish 8.78 vs 9.02; top-half 45.16% vs 50.43% (promoted clubs = 4 crowd / 53 empty matches, counted as non-top-half). ✅

Raw per-turn data (adversary move + target, plan, executed SQL, threading, latency, cost,
conclusion) is in [`exhibit_refine.json`](./exhibit_refine.json). Regenerate with
`python scripts/eval_refinement.py` (requires the football CSVs + `ANTHROPIC_API_KEY`; the
adversary's questions are non-deterministic, so a re-run will explore a different chain).

## Conclusions (verbatim)

**T1** — The anomaly is confirmed. Across 2016–2019 the Premier League showed a stable home advantage (home wins ~45–49% vs away wins ~28–34%). In the 2020 season this inverted sharply — home wins dropped to 37.89% while away wins climbed to 40.26%, making away wins more common than home wins for the first (and only) time in the period. This coincided almost exactly with the near-total loss of crowds: average attendance fell from 39,315 in 2019 to just 5,086 in 2020 (~87% decline). By 2021 attendance recovered to ~39,870 and home advantage returned (42.89% home vs 33.95% away), reinforcing that the 2020 inversion was a one-season disruption strongly associated with the absence of fans.

**T2** — Within the 2020 Premier League season, the handful of matches actually played with fans (31 games, ≥1,000 attendance) retained a strong, normal home advantage of 54.84% home wins versus 29.03% away wins — consistent with the pre-pandemic 2016–2019 pattern. This contrasts starkly with the full-season 2020 figures, where away wins (40.26%) edged out home wins (37.89%). Because the crowded subset behaves normally while the season as a whole inverted, the evidence points squarely at the absence of crowds as the driver of the 2020 home-advantage collapse. The one caveat is that the explicit 'empty' (<1,000) bucket was not returned in these results, so the empty-match win split is inferred rather than directly shown.

**T3** — The empty-match bucket genuinely comes back with zero rows: no 2020 Premier League match has a recorded attendance below 1,000, so the direct home/draw/away split you asked for cannot be produced this way. The issue is a data-encoding one — matches played without fans appear to have been logged with missing (NULL) attendance rather than a value under 1,000, which is why they were invisible in both the '≥1000' subset and the '<1000' filter. To show the empty bucket on its own, we need to define 'empty' as attendance IS NULL (or NULL-or-below-1000) rather than a numeric under-1,000 threshold.

**T4** — With attendance-empty defined correctly as NULL, the 2020 Premier League splits cleanly into 349 fan-less matches and 31 with a real crowd. The empty bucket shows the inversion directly — away wins at 41.26% edge out home wins at 36.39% — while the 31 crowded matches retain a strong, normal home advantage of 54.84% home vs 29.03% away. Because empty games make up roughly 92% of the season, the empty-bucket behaviour drives the full-season inversion. This is the direct, side-by-side evidence you asked for and it squarely attributes the 2020 collapse in home advantage to the absence of crowds.

**T5** — The dates overturn the pre-lockdown theory rather than confirm it. The 2020 season (Sep 2020–May 2021) actually began with empty stadiums on 12 September 2020, and the first match with a real crowd did not occur until 5 December 2020 — so the crowded games cannot be start-of-season fixtures. Instead they fall into two later windows when spectators were briefly permitted: 14 games in December 2020 and 17 in May 2021, while every match from September–November 2020 and January–April 2021 was played behind closed doors. The crowd/empty contrast is therefore not distorted by an early-season timing bias, though it is still an observational comparison confined to two short periods rather than a fully randomised one.

**T6** — Checking the 31 crowded matches against a stable squad-value measure, there is only a mild lean toward stronger home sides — crowded-bucket hosts averaged €315.3m in squad value versus €294.9m for empty-bucket hosts (~7% higher; medians €261.3m vs €251.6m, ~4%). Crucially, the crowded games are spread across 18 of the 20 clubs and include plenty of weaker sides (West Brom €84.5m, Southampton €113.3m, Brighton €123.3m, Chelsea €142.6m), while some strong clubs like Aston Villa (€472.3m) hosted no crowded games at all. This does not look like a strong-team selection bias large enough to manufacture the observed 54.84%/29.03% home advantage in the crowded bucket, so the crowd-effect interpretation survives this check — though the comparison remains observational and I could only apply the squad-value proxy, not the requested prior-season league finish.

**T7** — Ranking the hosts by their 2019 final league position shows no meaningful skew of the crowded-bucket games toward strong sides, and specifically no lean toward top-half finishers. The crowded hosts averaged a 2019 finish of 8.78 versus 9.02 for empty-bucket hosts — a negligible ~0.24-place difference — and only 45.16% of crowded matches were hosted by 2019 top-half clubs, actually below the empty bucket's 50.43%. The 31 crowded games are drawn from across the table (champions Liverpool through 16th-placed West Ham, plus promoted sides), rather than concentrated among the elite. This directly answers the deferred prior-season-finish check and reinforces the squad-value result: the crowd/empty contrast is not distorted by a strong-team selection bias, so the crowd-effect interpretation of the 2020 home-advantage collapse survives.
