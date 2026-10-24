# Known limitations

## Results

- **The impact is a simulator number.** The pack's simulator raises play probability for any valid page in the
  right dialect, so the size of the +12.5 pp is an upper bound; the sign and the method are the result. Purchases
  show no detectable change.
- **Doing nothing costs plays in the simulator.** With the do-nothing floor off (same code), 73.0% of agent-arm
  viewers were shown a personalised page and plays rose +14.1 pp; with it, 61.8% and +12.5 pp. The simulator
  rewards any valid page, so this cost may be overstated. Pursuing the skipped goal itself did not measurably
  move it (−0.4 pp, 95% upper bound +5.4 pp, n = 109). Whether the floor should withhold the page or only the
  message is a product call. To reproduce the floor-off numbers, copy `config/agent.json` with
  `goals.min_expected_value` set to 0 and run `python main.py run-all --config <copy> --out runs/floor_off`.
- **Goal-level lift is mostly unproven.** Per goal, the arms differ by a few points with wide intervals. Only G07
  has enough data to say anything, and it says the agent does not move it here (−1.1 pp). Learning acts on that.
- **The organic baseline is miscalibrated for G05, G07, G08 and G09.** It comes from the pilot and the 30-day
  history; in the simulator, for example, viewers resume series 4% of the time, not 13–49%. In sample it scores
  worse than a constant (Brier 0.157 vs 0.118).
- **Learning improves predictions, not provably decisions.** Out of sample, the updated baseline beats both the
  prior and a constant (Brier 0.111 vs 0.157 and 0.119). Day 2 has no simulated outcomes (below), so whether the
  232 changed day-2 decisions are better is not measured. A third day would judge them.
- **The day-2 comparison run is not frozen at day 1.** Without a learned table it re-fits the organic baseline from
  history that now includes day 1's simulated outcomes for both arms, treated viewers included (G07 26.7% → 17.1%,
  G08 10.1% → 16.8%). So the 232 changes compare the learned table with that refit, which is not a clean no-agent
  baseline. `twoday.py` and `learning_report.md` call that run "unchanged"; passing day 1's `baseline_table.json`
  to it would make the comparison clean.
- **Per-viewer attribution is descriptive.** "Plausible" means something reached the viewer before the success,
  nothing more; causal claims are made at arm level only.
- **Confidence** remains an evidence-quality score with stated reasons. The calibrated probability is
  `p_without_agent`, checked against the holdout.

## Runtime

- **Simulated concurrency.** The worker pool, queueing and dependency latency run on a virtual clock with latency
  from a hash. The load results are simulated, not a real load test.
- **The model is in log-only mode.** It is never followed, so its value is unmeasured. Measuring it needs
  following the model on a random share of disagreements; at about 3 disagreements a day that takes production
  traffic, not one replay day. It is still called about 600 times a day: log-only saves latency and risk, not
  money. If cost mattered, sample by a hash of the viewer, never by the evaluation's fault list.
- **The model's call rule has a history.** Calls first went to scores within 0.2, then were widened to 0.5 after
  none of the injected model faults fired (in hindsight, tuning toward the test). Log-only replaced both; the
  injected faults now fire wherever the general rule applies (2 of 5 on the sample day; the other 3 landed on
  opens that sat in the offline buffer for 9.5 and 15.3 hours and on a holdout viewer, where the model is never
  called).
- **The Claude adapter is untested against the live API.** No key was used.

## Tests

- **Five tests check less than their names say.**
  - `test_resume_copy_only_when_the_message_is_about_the_series` no longer gets a push to check: the goal now
    scores below the 0.2 follow-up floor.
  - `test_opt_out_with_missing_or_unusual_channel_blocks_every_channel` checks only push for `Push` and
    `notifications`, which by design block push alone.
  - `test_holdout_purchase_and_lapse_update_shadow_goal_without_replan_records` sends no purchase.
  - `test_model_timeout_still_publishes_within_page_budget` cannot fail in log-only mode, where the model is off
    the page path.
  - The end-to-end test checks that no trigger is decided twice, not that none is dropped.
- **`test_load` asserts on measured wall time** (each of 1,000 decisions under 1 s), so it can fail on a heavily
  loaded machine.

## Data and design choices

- **Day 2 of `two-day` is dated 15 days later** so day 1's outcome window is in the past, but the simulator
  hard-codes the replay date, so day 2 cannot be simulated. Viewers not triggered on day 1 have no activity
  recorded between 30 September and day 2, and many look absent (G12).
- **Hand-set values.** Only the no-agent probability `p` is estimated from data. Goal values (1.0 / 0.6 / 0.5), the
  movability shape 4p(1−p), the 0.1 do-nothing floor and the 0.2 follow-up floor are choices in config and code,
  not learned.
- **"Never did X" needs the whole history,** so G02 is rare for older viewers. G06 "recently lapsed" is read as
  within 30 days.
- **No dialect display names exist,** so WhatsApp's `dialect_name` carries the code. `wa_new_episode_v2` (claims a
  *new* episode) and `wa_trial_ending_v1` (needs a trial end date) are never used. In-app messages are supported
  but never chosen.
- **NA, missing or d7 app dialects get no action** (the app shows only its own dialect; d7 has no titles), as do
  stale offline-buffered opens.
- **The rule-correction scope is read from free text** (ambiguous wording is flagged for a human). On the sample
  day it touched 0 goals; `perturb --correct-goal G07` shows it at scale.
- **Withdrawals are pre-applied by look-ahead** for decisions in flight; in production the catalogue lookup at
  publish time does this.
- **`agent_state.sqlite` and the simulator's raw events are not committed.** `run-all` regenerates them. Decision
  timing uses measured compute time, so timestamps differ by milliseconds between runs; on the sample day, two runs
  made identical decisions, and two fresh `run-all`s gave the same day-2 comparison. `latency.fixed_compute_ms`
  removes the timing variation entirely (`two-day` uses it). `oua explain` falls back to the committed files when
  the SQLite memory is missing. On a fresh clone, run `explain` before any single step, or run `run-all` first:
  `judge`, `impact`, `learn` and `report` create an empty `agent_state.sqlite`, after which `explain` finds no
  journal.
- **`decisions.jsonl` is about 16.6 MB.** Every decision, including holdout shadow decisions and no-actions,
  carries its facts, the goals considered and its evidence. That is the audit trail, so it is kept whole.
