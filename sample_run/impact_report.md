# Impact report

All numbers below are computed by `oua impact` from `sample_run/sim/outcome_events.jsonl` (simulator seed `oua-practice`) and the randomised arms in `replay/arms.csv`. Nothing here is hand-edited; the conclusions are generated from the intervals.

## Headline (intent-to-treat, agent arm vs randomised holdout)

Population: every viewer who sent a trigger on the replay day, in the arm they were randomised to, whether or not the agent acted on them. Differences are in percentage points (pp) unless noted.

| Metric | n agent / holdout | Agent | Holdout | Difference (95% CI) | p (permutation) | Adjusted for past activity (95% CI) |
|---|---|---|---|---|---|---|
| play_24h: started any episode within 24 h of the first trigger | 863 / 883 | 49.7% | 37.3% | **+12.5** (+7.8 to +17.1) | 0.001 | +13.1 (+8.7 to +17.6) |
| complete_72h: completed an episode within 72 h | 863 / 883 | 35.7% | 28.9% | **+6.8** (+2.4 to +11.2) | 0.004 | +7.3 (+3.3 to +11.3) |
| active_days_14: distinct IST days with an app open in the 14 days after the replay day | 863 / 883 | 4.52 days | 4.47 days | **+0.05** (-0.25 to +0.36) | 0.728 | +0.14 (-0.07 to +0.36) |
| purchase_7d: bought a paid plan within 7 days | 863 / 883 | 2.7% | 3.6% | **-1.0** (-2.6 to +0.7) | 0.293 | -1.0 (-2.7 to +0.6) |
| goal_success: reached the goal the deterministic planner assigned (shadow goal for holdout) | 591 / 594 | 16.4% | 15.2% | **+1.3** (-2.9 to +5.4) | 0.595 | n/a |

goal_success is measured only for viewers who got a goal (identically in both arms), hence its smaller n.

Viewers: 863 agent, 883 holdout. The smallest effect this sample can reliably detect (80% power, 5% significance) is about 6.6 pp for play_24h and 2.3 pp for purchase_7d: smaller true effects can look like zero here.

**Effect on viewers the agent actually reached.** Only 61.8% of agent-arm viewers saw a personalised page or received a message (the rest arrived stale, in a dialect with no content, or never reached home while the page was live). Dividing the intent-to-treat effect by that share gives the effect on reached viewers: +20.2 pp for play_24h. This holds because holdout viewers cannot be reached by construction.

## What this does and does not show

- Detectable increases (95% interval above zero): play_24h (+12.5 pp), complete_72h (+6.8 pp).
- No detectable change (interval includes zero): active_days_14 (+0.05 days), purchase_7d (-1.0 pp), goal_success (+1.3 pp). The sample cannot rule out effects smaller than the minimum detectable effect.
- No revenue claim is made.
- Any effect here is an effect inside the pack's simulator, which raises play probability for every valid row on a live page in the right dialect; real viewers respond less mechanically. Treat a size as an upper bound on what this policy could do, and the sign and the method as the result.
- The simulator picks the title a viewer plays without looking at our page (unless a message was clicked), so goals tied to a specific title, such as resuming a series, are hard to move here.

## Were the arms comparable before the replay?

| Pre-period covariate (from data/events.jsonl) | Agent | Holdout | Difference |
|---|---|---|---|
| pre_play_days_14 | 1.801 | 1.903 | -0.102 |
| pre_active_days_14 | 3.608 | 3.716 | -0.107 |
| pre_completions_30 | 2.319 | 2.405 | -0.087 |
| pre_ever_paid | 0.377 | 0.392 | -0.015 |

Randomisation balances arms only on average. Where the arms differ before the replay, the adjusted column above removes the part of the outcome explained by past activity (CUPED regression adjustment). It narrows the interval and corrects for chance imbalance without introducing selection.

## Why simpler comparisons would mislead

| Comparison (play_24h) | Value | What is wrong with it |
|---|---|---|
| Reached vs not reached, agent arm only | +48.1 pp | Reaching a viewer depends on them being active (a fresh visit, reaching home). Reached viewers had 4.13 active days in the previous 14 vs 2.77 for the others, so the gap is mostly who they are. |
| Reached vs everyone else (the pilot report's method) | +35.5 pp | Same selection, plus it mixes in holdout and unreached viewers. This is the method behind the pilot's "2.3x uplift". |
| **Agent arm vs holdout (this report)** | **+12.5 pp** | Randomised, so the only systematic difference is the agent. |

Two more traps: (1) per-goal success rates without a holdout reward easy goals. The pilot's weekly habit goal (G09) succeeded 67.3% of the time in the holdout with no agent at all. (2) Before/after comparisons confuse the agent with day-of-week, campaigns and viewers who were already about to come back.

## Goal success by intended goal (same deterministic planner in both arms)

The holdout viewers got a *shadow* decision: the agent computed the goal it would have chosen, then did nothing. That makes goal success comparable goal by goal. Small cells are noisy, so read the intervals.

| Goal | n agent | n holdout | Agent | Holdout | Difference (95% CI) |
|---|---|---|---|---|---|
| G01 | 55 | 48 | 41.8% | 29.2% | +12.7 (-5.8 to +31.1) |
| G02 | 21 | 20 | 23.8% | 15.0% | +8.8 (-15.8 to +33.4) |
| G04 | 1 | 1 | 0.0% | 0.0% | too few viewers to compare |
| G06 | 8 | 9 | 0.0% | 11.1% | too few viewers to compare |
| G07 | 365 | 361 | 3.3% | 4.4% | -1.1 (-4.0 to +1.7) |
| G08 | 34 | 46 | 32.4% | 23.9% | +8.4 (-11.8 to +28.7) |
| G09 | 67 | 56 | 34.3% | 30.4% | +4.0 (-12.7 to +20.7) |
| G12 | 39 | 53 | 59.0% | 52.8% | +6.1 (-14.6 to +26.8) |

## Is the organic baseline right? (calibration on the holdout)

The agent scores goals with p_without_agent, estimated only from pre-replay data (pilot holdout + 30-day history). Holdout viewers received nothing, so their shadow goals show what actually happens without the agent in this environment. Goals left alone as "not worth an intervention" are tracked too: otherwise a goal the floor always skips would never be observed and its prediction never corrected.

| Goal | Holdout goals | Predicted p | Observed (95% CI) | Agent-arm goals | Agent-arm observed |
|---|---|---|---|---|---|
| G01 | 48 | 33.1% | 29.2% (16.3 to 42.0) | 55 | 41.8% |
| G02 | 20 | 19.7% | 15.0% (-0.6 to 30.6) | 21 | 23.8% |
| G04 | 1 | 22.7% | 0.0% (n/a to n/a) | 1 | 0.0% |
| G05 | 105 (105 left alone) | 0.6% | 5.7% (1.3 to 10.2) | 0 | n/a |
| G06 | 9 | 2.9% | 11.1% (-9.4 to 31.6) | 8 | 0.0% |
| G07 | 361 | 31.5% | 4.4% (2.3 to 6.6) | 365 | 3.3% |
| G08 | 46 | 10.8% | 23.9% (11.6 to 36.2) | 34 | 32.4% |
| G09 | 56 | 67.3% | 30.4% (18.3 to 42.4) | 67 | 34.3% |
| G12 | 53 | 47.1% | 52.8% (39.4 to 66.3) | 39 | 59.0% |

Brier score on 699 holdout goals: 0.157, against 0.118 for predicting the overall rate for everyone (lower is better).

Miscalibrated (prediction outside the 95% interval, n >= 20): G05, G07, G08, G09. The history these rates come from and the simulator that produced the outcomes do not behave the same way for these goals. `oua learn` folds these holdout outcomes back into the baseline, so the next day is scored with what happens here; `learning_report.md` checks out of sample whether that improves the predictions.

## Do follow-up messages help? (randomised within the agent arm)

Viewers eligible for a follow-up were assigned "send" or "withhold" at random (p=0.5): 98 sent, 98 withheld. Sent messages drew 2 clicks.

| Metric | Sent | Withheld | Difference (95% CI) | p | Smallest detectable |
|---|---|---|---|---|---|
| goal_success | 24.5% | 18.4% | +6.1 (-5.4 to +17.6) | 0.37 | 16.5 pp |
| play_72h | 63.3% | 56.1% | +7.1 (-6.6 to +20.9) | 0.36 | 19.7 pp |

With this few eligible viewers only a large effect could be detected; the design, not one day, is the point: keep randomising and the estimate tightens.

## Robustness across simulator seeds

Evaluators use a different seed. For each seed the simulator ran twice, with our actions and with no actions. Because it is a simulator we can compute the true effect on the same agent-arm viewers (with minus without actions), which is impossible in production, and check that the intent-to-treat interval contains it.

The holdout's outcomes are identical in both runs (each viewer has their own random stream and the simulator ignores actions on holdout viewers). So the no-action A/A difference is exactly the ITT minus the true effect: the two columns are one check, not two, and "covers the truth" means the chance imbalance between arms stayed inside the interval.

| Seed | play_24h ITT (95% CI) | True simulated effect | Chance imbalance (= no-action A/A) | CI covers truth |
|---|---|---|---|---|
| seed-1 | +9.4 (+4.7 to +14.0) | +10.2 | -0.8 | yes |
| seed-2 | +11.6 (+7.0 to +16.2) | +10.1 | +1.5 | yes |
| seed-3 | +10.9 (+6.3 to +15.5) | +9.2 | +1.8 | yes |
| seed-4 | +11.7 (+7.1 to +16.3) | +9.8 | +1.9 | yes |
| seed-5 | +9.8 (+5.2 to +14.4) | +8.8 | +1.0 | yes |

The interval contained the true simulated effect in 5 of 5 seeds.

## The pilot report, re-analysed

The internal pilot report claimed **40.1% vs 17.7%, a "2.3x uplift"**, comparing 217 served viewers with 3674 "other users". That compares viewers the agent chose to serve with everyone else, including the holdout.

The randomised comparison in the same file: treatment 18.3% vs holdout 19.6%, **difference -1.3 pp (95% CI -3.8 to +1.2, p=0.29)**. Only 11.3% of the treatment arm was ever served, so even the implied effect on served viewers (-11.5 pp) is indistinguishable from zero. **The pilot shows no evidence that agent v0 helped.**

Where the 2.3x came from: served viewers were concentrated in easy goals.

| Goal | Share of served | Share of all | Served success | Treatment success | Holdout success |
|---|---|---|---|---|---|
| G02 | 6.9% | 9.3% | 6.7% | 19.2% | 19.7% |
| G04 | 0.5% | 3.3% | 0.0% | 23.8% | 22.7% |
| G05 | 38.7% | 54.4% | 1.2% | 1.2% | 0.6% |
| G06 | 5.5% | 8.5% | 0.0% | 0.6% | 2.9% |
| G09 | 48.4% | 24.5% | 81.0% | 60.9% | 67.3% |

## Guardrails (from the simulator report)

| Guardrail | Value |
|---|---|
| Policy problems flagged by the simulator | 0 |
| Actions on holdout viewers | 0 |
| home_page_published_but_not_shown | 13 |
| push_undeliverable_token | 8 |
| Problems found by our independent audit (`oua audit`) | 0 |

