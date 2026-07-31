# Exhibit branching-depth eval — reactive adversary + real graph forks

This reruns the [depth eval](../eval_refinement/RESULTS.md) with two changes:
1. a **different natural experiment** — Newcastle United's October 2021 takeover (a discrete
   exogenous shock with a clear before/after and confounds to rule out, structurally like the
   COVID home-advantage case); and
2. the adversary can now **`/branch`** — fork a new question off an *earlier* turn's node
   instead of the latest one — so the investigation is a real **tree**, exercising the
   `run_question(parent_id=…)` capability and its ancestry-scoped context. The adversary was
   explicitly told to, at least once, spawn two sibling branches of **competing explanations**
   from one node and later compare them.

Harness: [`scripts/eval_branching.py`](../../scripts/eval_branching.py). Every dependent number
independently recomputed in DuckDB.

**Result: 8 turns, 476 s, $1.10 tool + $0.17 adversary, 5/8 turns were real branches. The
causal spine (league position) verified exact at every turn and consistent across branches.
Branching, ancestry-scoping, and the branch-comparison all worked. One real weakness surfaced —
a squad-value proxy that overcounts and drifts between branches — which the linear evals could
not have exposed.**

## The tree that emerged

```
T1  baseline: Newcastle position per season 2015–24  (root)
│
├─ T2  split 2021→22 before/after takeover  ── caught wrong season (labeling)
│   └─ T3  redo on the correct (2021) season   [branch←T2]
│       └─ T4  MANAGER channel: split 2021 by manager   [branch←T3]
│           └─ T5  SPENDING channel: net transfer spend  [branch←T4]  ── hit fee-coverage gap
│               └─ T6  SPENDING via squad market value    [branch←T5]
│
└─ T7  CONTROL: Aston Villa counterfactual        [branch←T1]  ← true sibling, forked to baseline

T8  compare branches head-to-head: manager (T4) vs squad-value (T6)  → causal synthesis
```

T7 is the structurally interesting one: it forks off **T1** while the active line was down at
T6, so its context is scoped to the baseline alone — it runs the counterfactual **blind to the
spending/manager work**, exactly as an independent control should. T8 then reads the T4 and T6
branches against each other.

## Scorecard

| T | Move | Fork | Question (short) | Executed | Numerically correct | Latency | Cost |
|---|---|---|---|---|---|---|---|
| 1 | drill | root | position per season 2015–24 | ✅ | ✅ exact | 30.4 s | $0.224 |
| 2 | drill | thread | split 2021→22 before/after Oct-2021 | ✅ | ✅ (correctly *refused* — wrong season) | 55.2 s | $0.131 |
| 3 | correct-def | ←T2 | redo split on the real 2021 campaign | ✅ | ✅ exact (before 17.57 / after 16.32) | 36.0 s | $0.087 |
| 4 | drill | ←T3 | split 2021 by manager | ✅ | ✅ exact (Bruce 17.75 / Jones 19.0 / Howe 15.93) | 32.5 s | $0.080 |
| 5 | branch-explore | ←T4 | net transfer spend vs position | ✅ | ✅ (correctly reported fee-coverage gap) | 51.7 s | $0.128 |
| 6 | correct-def | ←T5 | squad **market value** vs position | ✅ | ⚠️ series inflated (see weakness) | 69.1 s | $0.162 |
| 7 | branch-explore | ←T1 | Aston Villa control club | ✅ | ✅ exact (Villa 11.79→11.0; NUFC 16.55→4.50) | 35.6 s | $0.092 |
| 8 | compare-branches | thread | manager vs money: which leads? | ✅ | ✅ deltas exact; ⚠️ value base differs from T6 | 82.9 s | $0.194 |

## Branching-specific assessment

| Dimension | Verdict | Evidence |
|---|---|---|
| **Fork targeting** | ✅ | 5/8 turns set `branch_from`; T7 attached under T1's conclusion node (`4750ea34`) while the active line was at T6 — a genuine sibling, not the latest thread. |
| **Ancestry-scoped context** | ✅ | T7's counterfactual reasoned from the baseline only; it did not drag in the T2–T6 spending/manager context, so the control stayed independent (the whole point of the new scoping). |
| **Branch comparison** | ✅ | T8 correctly pulled the *manager* branch (T4) and the *squad-value* branch (T6) and read them head-to-head into a causal ordering. |
| **Cross-branch numeric consistency** | ⚠️ **1 real failure** | T6 and T8 both compute "Newcastle squad value" but with **different SQL** (name-match + `<=` vs club_id + `<`), giving **€859m→€1,343m** (T6) vs **€358m→€682m** (T8) for the same concept. Conclusion-preserving (both agree money *lags* the jump) but the absolute figures don't reconcile turn-to-turn. |

## The one real weakness (what branching newly exposed)

**The squad-value proxy is unsound, and the two branches operationalized it differently.**

- **Overcount:** both queries sum *every player ever valued while at Newcastle before the season
  cutoff* — 155 "players" (T6) and 56 (T8) — rather than the ~25-man season roster. Neither
  sanity-checked the implausible squad size.
- **Contamination:** T6's `current_club_name LIKE 'Newcastle United%'` also sweeps in **Newcastle
  United *Jets*** (an unrelated Australian A-League club). T8's `club_id` filter dodges some of
  this but still includes the Jets club_id and is cumulative-ever, not current-roster.
- **Definitional drift across branches:** because each turn regenerates its own SQL, T6 and T8
  disagree ~2.4× on the same quantity. A user comparing €859m and €358m across the two turns
  would be misled.

**Why it didn't sink the conclusion:** the *causal spine* is the league-position series, which is
exact and consistent everywhere (T1/T3/T4/T7/T8 all agree: 16.55→4.50, before/after 17.57/16.32,
managers 17.75/19.0/15.93, Villa flat). The squad value is only a supporting proxy, and Exhibit
hedged it correctly ("directional rather than lockstep") and reached the right causal ordering —
manager change leads the 2021→22 step-change, investment sustains it later. The proxy's absolute
values are the untrustworthy part, and Exhibit did not flag the overcount or the Jets contamination.

## Failure classification

| Class | Count | Detail |
|---|---|---|
| Schema understanding | ~1 | Season-labeling correctly diagnosed (T2), managers/positions used well — but missed that `'Newcastle United%'` matches the Australian *Jets*, and the `current_club_name`↔`current_club_id` linkage is messy (105 distinct club_ids carry a "Newcastle" name). |
| Planning | 0 | Coherent staged plans throughout. |
| SQL generation | 0 execution errors | All queries ran; the flaw is definitional (overcount), not syntactic. |
| Join selection | 0 | Multi-table joins (clubs↔club_games↔games, valuations) correct. |
| **Statistical / proxy misuse** | **1** | Squad-value proxy overcounts the roster and wasn't sanity-checked against squad size. Hedged, not flagged. |
| Interpretation | 0 major | Correct causal ordering; correct use of the control; correct diagnoses of the season-labeling and the 0.0-fee coverage gap. |
| Context / memory / branching | 0 | Forks targeted the right nodes; ancestry scoping kept the control independent; T8 compared branches coherently. |
| **Cross-branch consistency** | **1** | Same concept ("squad value"), two different operationalizations across branches → €859m vs €358m. |
| UX | 0 | Graceful throughout; the fee-gap and wrong-season cases surfaced as honest conclusions, not crashes. |

## Independent verification (DuckDB, exact unless noted)

- **T1:** NUFC GB1 avg own_position — 2015 ≈17.92 … 2021 16.55 → 2022 4.50 → 2023 8.05 → 2024 6.45 (2016 relegation season absent). ✅
- **T3:** before Oct-7-2021 = 17.57 over 7 winless games (Aug 15–Oct 2); after = 16.32 over 31 games. ✅
- **T4:** Steve Bruce 17.75 (8g) → Graeme Jones 19.0 (3g interim) → Eddie Howe 15.93 (28g). ✅
- **T6:** squad-value *series* reproduces its own SQL (859→915→1,180→1,343) — but that SQL overcounts (155 players) and includes Newcastle Jets. ⚠️
- **T7:** Aston Villa 11.79 (2021) → 11.0 (2022), no jump; NUFC 16.55 → 4.50. ✅
- **T8:** deltas reproduce exactly — pos −12.05 into 2022 on +€123.3m, +€151.5m into 2023 with a +3.55 *regression*, +€48.9m into 2024 flat — but on a value base (358→682) that differs from T6's (859→1,343). ✅ deltas / ⚠️ base.

Raw per-turn data (adversary move + fork target, plan, executed SQL, threading, latency, cost,
conclusion) is in [`exhibit_branch.json`](./exhibit_branch.json). Regenerate with
`python scripts/eval_branching.py` (football CSVs + `ANTHROPIC_API_KEY`; non-deterministic).

## Conclusions (verbatim)

**T1** — Using club_games own_position for Newcastle United (GB1), the data shows a club stuck in lower-mid-table or worse from 2015 through 2021 (averaging between 12.63 and 17.92, with the 2016 relegation season missing entirely). The decisive shift lands between the 2021 season (16.55) and the 2022 season (4.50) — a ~12-position leap that coincides with the October 2021 takeover — and the elevated performance holds in 2023 (8.05) and 2024 (6.45).

**T2** — The split did not answer the intended question because of exactly the season-labeling issue you flagged. The engine split what it calls 'season 2022', but those 38 games run from August 2022 to May 2023 … Under this dataset's start-year convention, the real takeover campaign (2021-22, containing the October 2021 change of ownership) is stored as season 2021 (avg 16.55), and season 2022 (avg 4.50) is the first complete season under the new owners. To properly test whether the rise was immediate or gradual, you need to split season 2021, not season 2022.

**T3** — Within the actual takeover campaign (season 2021 = 2021-22 EPL), the improvement was gradual, not immediate. Before the October 7 2021 takeover Newcastle averaged 17.57 across 7 winless games; in the 31 games after they averaged 16.32 — a barely-changed figure … the first win not landing until December 4. Any real climb came in the back half of the season rather than as a step-change on the ownership date.

**T4** — Splitting season 2021 by manager shows the turnaround aligns with — but was not triggered instantly by — the managerial change. Newcastle sat at 17.75 under Steve Bruce, dropped to their lowest point (19.0) during Graeme Jones's brief interim, then recovered to 15.93 across Eddie Howe's 28 games … the in-season gain of under two positions is gradual rather than a step-change.

**T5** — This step cannot answer the intended question because the spending channel didn't materialize in the data. Newcastle's incoming transfer fees come back as 0.0 in every window from January 2022 onward … a clear gap in the transfers table's fee coverage, not evidence that Newcastle spent nothing. The position series is trustworthy and reproduces the familiar arc (16.55 → 4.50 → 8.05 → 6.45 → 12.11) … The correlation question stays open until we source the inbound transfer fees from a more complete feed.

**T6** — Using season-start squad market value as a proxy sidesteps the broken fee feed and confirms the intended story: Newcastle's squad value climbed steadily from €859m in 2021 to €1,343m in 2024 while average league position leapt from 16.55 to 4.50 … The alignment is directional rather than lockstep: the decisive on-pitch jump into 2022 happened on only a small value increase, with the largest squad-value gains landing in 2023 and 2024 after the climb had already begun.

**T7** — The matched control does not reproduce Newcastle's pattern. Aston Villa … showed essentially no change across the same 2021→2022 window (11.79 → 11.0), whereas Newcastle leapt roughly 12 positions (16.55 → 4.50) coinciding with the October 2021 takeover … This strengthens — rather than undermines — the Newcastle-specific ownership story, though a single control club is suggestive rather than conclusive.

**T8** — Read head to head, the manager channel — Howe's arrival — leads the initial 2021→2022 step-change and the investment channel sustains it afterward. The decisive ~12-position leap (16.55 → 4.50) rode on only a moderate €123m squad-value bump, while the single biggest value increase (€151.5m into 2023) actually coincided with a slight position regression (+3.55) … the largest spending arrives after the climb and functions to sustain the elevated level, whereas the abruptly-timed on-pitch improvement lines up with the coaching change rather than with peak investment.
