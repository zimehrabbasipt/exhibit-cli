# Exhibit adversarial eval — real football data

**Data:** the Transfermarkt football dataset (a third-party dataset), 12 CSV tables
(games, appearances, clubs, club_games, players, player_valuations, transfers,
game_events, game_lineups, competitions, club_games, …), ~740 MB, 88,958 games
spanning 2006-06-09 → 2026-07-06 across 70 competitions.

**Method.** The 8 questions below were written by a **fresh subagent** given only the
schema and a sports-analyst persona — no knowledge of Exhibit's internals or which
questions it could answer — specifically to remove context bias from the author.
They escalate from simple scoping (Q1) to open-ended strategy synthesis (Q8). All 8
ran **sequentially in one investigation** (so cumulative memory is exercised) via
`scripts/eval_football.py`. Objective numbers (Q1–Q5) were **independently recomputed
in DuckDB** against the raw CSVs; the open-ended questions (Q6–Q8) were judged on
approach + soundness, since there is no single correct figure.

**Headline:** every objectively-checkable number was exactly right. One graceful,
well-understood tool limitation (Q3). Strong, self-critical reasoning on the
open-ended questions. **Total: 419 s (~7 min), $1.18, all 8 on the full path.**

---

## Scorecard

| Q | Ask (short) | Expected approach | Expected core facts (ground truth) | Executed? | Numerically correct? | Conclusion supported? | Useful follow-up? | Latency | Cost | Path |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | scope: games / date range / competitions | `COUNT` + `MIN/MAX(date)` + distinct competitions | 88,958 games; 2006-06-09 → 2026-07-06; 70 competitions | ✅ | ✅ exact | ✅ | ✅ | 23.2 s | $0.206 | full |
| 2 | top-10 scorers + games-per-player | `SUM(goals)`, `COUNT(DISTINCT game)` per player | Lewandowski 528/665, Messi 458/529, Ronaldo 434/485 | ✅ | ✅ exact | ✅ + efficiency framing | ✅ | 18.9 s | $0.052 | full |
| 3 | home advantage (win% + goals) | win% by hosting side + avg goals | home 45.1% / away 33.2% / draw 21.7%; goals 1.60 vs 1.33 | ⚠️ tool step errored | ✅ (SQL step correct) | ✅ | ✅ | 24.6 s | $0.063 | full |
| 4 | PL (GB1) attendance + home/away PPG gap by season | per-season aggregation | COVID 2020 season: attendance 5,086; PPG gap −0.07 (away > home) | ✅ | ✅ exact | ✅ **surfaced COVID natural experiment** | ✅ | 35.9 s | $0.092 | full + chart |
| 5 | net spend vs league finish | parse messy `net_transfer_record` string → €, correlate vs position | corr **0.153** (n=793); >€50m-spend avg pos 4.19; break-even 11.13; net sellers 9.91 | ✅ | ✅ **verified exact** | ✅ nuanced (threshold effect, not linear) | ✅ | 69.4 s | $0.166 | full |
| 6 | biggest peak-value vs transfer-fee gap; patterns | join valuations + transfers + players | open-ended | ✅ | approach sound* | ✅ **self-caught the peak-value timing artifact** | ✅ | 53.0 s | $0.151 | full |
| 7 | define & rank "big-game players" | fuzzy-metric design, per-90 lift, joins | open-ended | ✅ | approach sound* | ✅ **effect *reverses*; added small-sample guards** | ✅ | 86.3 s | $0.221 | full |
| 8 | buy-low/sell-high strategy for a small club | synthesize 4 tables into advice | open-ended | ✅ | approach sound* | ✅ grounded + caveated multiple-inflation bias | ✅ | 93.1 s | $0.234 | full |

\* Q6–Q8 are judgment questions with no single correct number; the SQL logic was
verified sound and the self-caveats correct, but not every returned figure was
independently reproduced.

---

## Failure classification

Over 8 questions / ~21 plan steps, exactly one real failure:

| Failure class | Count | Detail |
|---|---|---|
| Schema understanding | 0 | Correctly used `club_games.hosting`, `competition_id='GB1'`, position fields, `player_name`, `net_transfer_record`, valuation joins. |
| Planning | 0 | Plans were coherent and appropriately staged (headline → decompose → validate). |
| SQL generation | 0 | Every query ran; the `net_transfer_record` sign-parsing regex was correct. |
| Join selection | 0 | Multi-table joins in Q5–Q8 (clubs↔club_games, valuations↔transfers↔players) all correct. |
| **Statistical / tool misuse** | **1** | **Q3:** planner routed `t_test(value="own_goals", group="hosting")`, but analysis tools run against the **primary table** (`appearances`), which has no `own_goals` (that column lives in `club_games`) → `column 'own_goals' not found`. The parallel SQL step succeeded, so the final answer was still correct. This is the known **"tools only see the primary base table"** limitation. |
| Interpretation | 0 major | 1 minor: Q5 used *average game-position across all games* as a proxy for "final league finish" (defensible — there is no clean final-finish field — and lightly flagged, but not loudly). |
| Context / memory | 0 | The single cumulative session held across all 8; later questions built on earlier scope without recomputation errors. |
| UX | 0 | Auto-charted the headline result on Q2–Q8; the Q3 tool error surfaced as a clean `error` node, not a crash. |

**The one crack is the expected one:** analysis tools can't reach columns outside the
primary table. It degraded gracefully (SQL backfilled the answer). This eval is the

---

## Economics

- ~$0.15 and ~52 s per question on 740 MB across 12 tables.
- Hard synthesis questions (Q7, Q8) cost ~$0.22–0.23 and ~90 s.
- Q1's $0.206 is the one-time catalog **cache write**; every question after rode the
  cache (`cache_read_tokens` 190,589 vs `cache_write_tokens` 27,227 over the run).
- Session totals: 21 LLM calls, 32,380 input / 30,293 output tokens,
  190,589 cache-read / 27,227 cache-write tokens.

---

## Conclusions (verbatim from the run)

### Q1 — dataset scope
> The dataset comprises 88,958 games spread across 70 competitions, with match dates ranging from 2006-06-09 to 2026-07-06 (about 18 seasons). Coverage is dominated by the major European domestic leagues — England (GB1), Italy (IT1) and Spain (ES1) each contribute 5,320 games — alongside domestic cups and continental competitions such as the Champions League and Europa League. The evidence directly answers the count, date span, and competition breakdown.

### Q2 — top scorers
> Across all appearances in the dataset, Robert Lewandowski is the most prolific scorer with 528 goals in 665 games, followed by Lionel Messi (458 in 529) and Cristiano Ronaldo (434 in 485). Beyond raw totals, the per-game rates tell a clearer story of efficiency: Ronaldo (0.89) and Messi (0.87) convert most frequently, while Lewandowski (0.79), Mbappé (0.77) and Kane (0.74) form a strong second tier; the lower-ranked names such as Immobile (0.52) and Lukaku (0.49) reached their totals through both volume and longevity rather than elite conversion.

### Q3 — home advantage
> _(SQL step correct; the `t_test` tool step errored on a non-primary-table column — see failure classification.)_

### Q4 — Premier League attendance & PPG gap by season
> _(Surfaced the COVID-2020 empty-stadium season as a natural experiment: attendance collapsed to ~5,086 and the home/away PPG gap inverted to −0.07, i.e. away teams outperformed home teams that season.)_

### Q5 — net spend vs league position
> _(Correlation 0.153 over 793 clubs — weak positive; relationship is a threshold effect, not linear: only >€50m net spenders cluster near the top (avg position 4.19), break-even clubs finish worst (11.13), and net sellers actually do slightly better (9.91).)_

### Q6 — peak value vs transfer fee
> _(Identified players sold well below peak value and broke the pattern down by position, age band, and selling club — while explicitly flagging that "peak value" is measured at a player's career peak, which post-dates many sales and inflates the apparent gap.)_

### Q7 — "big-game players"
> The honest answer is that the 'big-game player' effect largely evaporates — and even reverses — once you measure it carefully. Defining 'big' as top-6 opponents OR knockout rounds and comparing goals+assists per 90 shows the average player is actually less productive in big games (0.186 vs 0.248), because facing top-6 league sides suppresses output (0.157) far more than knockout football lifts it (0.268). A workable definition therefore ranks players by their per-90 (goals+assists) lift in big versus ordinary games, but it must be guarded with a meaningful minimum-appearance floor in BOTH buckets and a non-zero ordinary baseline; without that, the leaderboard fills with small-sample youngsters whose 'lift' is a statistical artifact. Once those guards are applied, only high-volume names like Julián Álvarez, Raúl García and Hyeon-gyu Oh look like true big-game performers. Confidence is medium: the aggregate direction is robust across ~1.9M appearances, but the individual ranking is fragile and threshold-dependent.

### Q8 — buy-low/sell-high strategy
> For a budget-constrained mid-table club, the data points to buying cheap, creative attackers out of high-multiple developmental markets — Brazil, Argentina, Mexico and MLS — rather than chasing the leagues with the biggest absolute gains (GB1, ES1), where entry prices are already high. Attacking midfielders and wingers offer the best-supported appreciation, while goalkeepers and full-backs offer the least. The clubs and agents topping the value-creation tables (Real Madrid, Barcelona, Bayern; Pimenta, Gestifute) are essentially the elite end of the market where value is realised or where top talent is already concentrated — so they read better as *exit routes* (clubs to sell to, like Bilbao/Leverkusen who multiply modest inputs 13-14x) than as places a small club can source bargains. Important caveat: the entry-to-peak method systematically inflates multiples for players signed young and cheap whose peak came years later, so the eye-popping 60x–220x figures overstate the realistic, at-the-time upside.

---

## Independent verification (Q1–Q5)

Recomputed directly in DuckDB against the raw CSVs; all matched Exhibit exactly:

- **Q1:** 88,958 games / 70 competitions / 2006-06-09 → 2026-07-06. ✅
- **Q2:** Lewandowski 528 goals in 665 games; Messi 458/529; Ronaldo 434/485. ✅
- **Q3:** home win 45.1% / away 33.2% / draw 21.7%; avg goals 1.60 (home) vs 1.33 (away). ✅
- **Q4:** GB1 per-season attendance; COVID-2020 attendance 5,086 and home/away PPG gap −0.07. ✅
- **Q5:** `CORR(net_eur, avg own_position) = 0.153` over 793 clubs; tiers `>€50m spend` 4.19,
  `some spend` 9.33, `break-even` 11.13, `net seller` 9.91. The sign in messy strings like
  `€-25.00m` and `+€5.90m` parsed correctly (minus is adjacent to the digits, so
  `REGEXP_EXTRACT(…, '-?[0-9.]+')` captures it). ✅

Raw per-question data (plan steps, executed SQL, tool calls, latency, cost, errors)
is in [`exhibit_eval.json`](./exhibit_eval.json). Regenerate with
`python scripts/eval_football.py` (requires the football CSVs and `ANTHROPIC_API_KEY`).
