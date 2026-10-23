# AI collaboration log

Drafted with Claude Code from the working session and reviewed by me before submission. The code was written by
Claude Code (Claude Opus 5.5; Claude Fable 5.1 for part of the first review round) under my direction. I used a
separate AI agent to review the result. Below: the decisions I made, what independent reviews found, what the AI got
wrong and how that was caught.

## Decisions I made

| Decision | Why |
|---|---|
| Required a full reading of the brief and a plan before any code, and that everything be run and tested rather than claimed | A plan is cheap to change; untested AI code is exactly what the brief says will score poorly |
| After version 1 passed all its own checks, asked for a bug hunt anyway | Green checks written by the same author share its blind spots. Three fresh-context reviewers found 16 real bugs (below) |
| Had the finished project reviewed by a separate AI agent, and directed the next round against its findings, plan first | It scored the work 8/10 and named concrete gaps (table below). I prioritised them against my deadline |
| **Rejected** Claude's design of calling the model live on close calls (its threshold had been widened from 0.2 to 0.5), and chose log-only mode | The brief says to call models only when it reduces meaningful uncertainty: 607 calls changed 9 decisions with no evidence those were better. It made pages about 11x slower. Widening the threshold after the injected faults did not fire reads as tuning toward the test |
| **Rejected** the reviewing agent's suggestion to keep calling the model on the pack's fault triggers, after Claude flagged it | The agent would be reading the answer key, and the evaluators' second day has different traps. The general rule stays; the reviewing agent agreed and withdrew it |
| Kept Claude's proposal to randomise follow-up messages 50/50 | Their value was unknown (4 clicks from 290 messages); an even split measures it fastest |

## What the external review found, and what changed

| Finding | Change | Evidence now |
|---|---|---|
| Doing nothing was always forced by a rule, never chosen | Goals are scored as value × movability from a no-agent baseline; below a floor the agent records "not worth an intervention". A still-live page is not republished | 159 chosen no-actions on the sample day, all one case (first-paid-plan only), each with its numbers; 81 more leave an earlier page live, which is not a new choice. Cost, same code with the floor off: reach 73.0% → 61.8%, play lift +14.1 → +12.5 pp |
| Goal scores were hand-set; goal choice did not change outcomes | Only the no-agent baseline `p` comes from data (pilot holdout and history); goal values and the 4p(1−p) shape are choices. The paywall bonus was removed after the data contradicted it. G07, the most common goal, is halved once measured not to move | `impact_report.md` per-goal table; `learning_report.md` |
| Confidence was not calibrated | Every decision carries `p_without_agent`, checked against the holdout (calibration table, Brier score) | Accurate for G01/G02/G12, wrong for G07/G08/G09, and stated plainly |
| Learning only ran after the replay and was shown only in a unit test | `oua learn` updates the baseline from holdout outcomes and halves unmovable goals; `oua two-day` replays an unseen day 2 with and without it | Out of sample, the learned baseline predicts better: Brier 0.157 → 0.111, against 0.119 for a constant. 232 day-2 decisions change (208 in what the viewer gets), the same in two fresh runs; both day-2 audits clean. Whether those decisions are better is not measured |
| The rule correction never fired on the real day | `perturb --correct-goal` aims it at a goal that is open in volume | 249 running G07 goals re-pointed, 0 others, the judge agrees |
| No git history, a 72 MB sample run, long docs | Commits from this point on; regenerable files are not committed; docs rewritten shorter | — |

## What independent reviewers found in Claude's code

Three fresh-context Claude reviewers, each told to report only what they could reproduce, found bugs that the tests,
the audit and the pack's simulator had all missed. The worst two:
- "Continue X: episode 2 is ready" pushes about films or unstarted titles: 24 on an unseen day at 3x load.
- 187 messages sent in a dialect the viewer had switched away from.

Others:
- A purchase cancelled unrelated follow-ups.
- A withdrawal re-published superseded pages.
- The message cap counted queue time, not send time.
- Rule-scope parsing was a substring match.
- Renewals were counted as conversions.
- Holdout viewers got "re-plan" records.
- Unknown-channel opt-outs were ignored.
- An expired trial was read as active.
- One malformed model answer crashed the replay.
- The model could cite another goal's evidence.
- Checks were listed as passed that never ran.
- A re-run judge doubled learnings.
- Copy checks were bypassed by "Last-chance" and "99 rupees".
- The injection detector was evadable.

Each has a regression test in `tests/test_review_fixes.py`. All 23 fail on the pre-review code and pass now. On the
unseen day, the new audit flags 211 breaches in the old code's output; the old audit flagged 0, and the fixed code
produces 0.

## Mistakes the AI made and caught itself

- A first model-call rule that never let the injected faults fire. That led to the widening I later rejected.
- Follow-ups to 565 of about 860 visitors: disproportionate, so habitual viewers were excluded.
- A circuit-breaker test that "failed" because the test was wrong; it was fixed, with a retry assertion added.
- A mutation-test run that only proved imports failed; it was redone properly.
- A test that passed vacuously; it was rewritten.
- A 252 s stress run traced to missing indexes and eager work: 55 s afterwards, with identical output.
- An 85% "sure thing" do-nothing threshold that the data showed would never fire; it was replaced by the
  expected-value floor.
- A blind spot in that floor, found while measuring its cost (which the second review asked for). The floor skips
  first-paid-plan, predicted at 0.6% without the agent. Because both arms skipped it, no outcome was ever recorded,
  so learning could never correct the prediction. The floor-off run showed the real holdout rate is about 5%. Goals
  left alone are now tracked and feed calibration and learning.
- A claim that day 2 reproduced exactly once its compute time was fixed. A rerun gave 230 instead of 229. The
  cause was random ids for the copied triggers in the unseen-day builder, so each rebuild timed differently. Ids now
  come from the seed, a test covers it, and three runs give 232.

## How AI output was checked

- **Claims:** every number in the docs comes from a script over the pack or a generated report.
- **Real runs:** behaviour was inspected on the real replay, specific viewers were walked with `oua explain`, and
  unseen days were built with `oua perturb`, including at 3x and 5x load.
- **Two independent safety checks:** the pack's simulator and our audit, which shares no code with the agent.
- **Tests:** 75 tests, mutation checks, regression tests proven to fail on the old code, and two full runs that
  must match.
- **Never trusted to AI:** evidence (model output is never evidence, in the product or in the docs), and the data
  pack (treated as personal data, uploaded nowhere).
