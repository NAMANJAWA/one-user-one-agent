# Architecture

Every threshold below lives in `config/agent.json`, with its reason.

```
triggers + live events + control events, in delivery order
  → normalise: dedupe by event id; real time = received_at − (sent_at − occurred_at); freshness
  → viewer agent (one per viewer, one writer):
      idempotency → freshness → facts with provenance → holdout? shadow only
      → goal: continue the open one, or score every eligible goal; do nothing if none is worth it
      → page in the app's dialect (+ at most one follow-up, randomised)
  → independent checker (raw rows only) → publish / queue → re-check at publish and at send time
simulator → judge (versioned rules) → per-viewer learnings
          → impact vs holdout, calibration, follow-up experiment → learn → next day's policy
```

## Ownership

One state document per viewer (`viewer_state`), written only by the agent: facts with provenance, the single
active goal, the plan, every decision, action, outcome and learning. The harness serialises each viewer's work, so
two components can never pursue different goals for one viewer. Holdout viewers have a separate `shadow_goal`,
computed the same way and never acted on, so goal success can be compared between arms.

## Deciding

1. **Eligibility with evidence.** Each of the 12 goals is checked against cited rows. G10 (no success event) and
   G11 (`tv_app_open` never occurs) are excluded as unmeasurable. "Never did X" is claimed only when the viewer's
   whole history is visible; G06's undefined "recently lapsed" is read as a lapse within 30 days.
2. **Score = value × movability.** Only `p` comes from data: the probability of reaching the goal *without* the
   agent (the organic baseline, below). `value` is what reaching the goal is worth (paid conversion 1.0, keeping a
   payer engaged 0.6, activation 0.5): a product decision. `movability = 4p(1−p)` is a modelling assumption: sure
   things and near-impossible goals barely move, uncertain outcomes are where an intervention can matter. Example:
   trial-to-paid (p = 22.7%) scores 0.70, first-paid-plan (p = 0.6%) scores 0.02.
3. **Deliberate do-nothing.** If the best goal scores below 0.1, the agent records "not worth an intervention", with
   the numbers, and leaves the default page. On the sample day this is one case, 159 decisions: viewers whose only
   goal is first-paid-plan. It has a cost: with the floor off (same code), 73.0% of agent-arm viewers are shown a
   personalised page and plays rise +14.1 pp; with it, 61.8% and +12.5 pp. Pursuing first-paid-plan for them did not
   measurably move it (−0.4 pp vs holdout, 95% upper bound +5.4 pp, n = 109). The goal left alone is still tracked,
   in both arms, so its prediction is tested and corrected (below). Separately, a page for the same goal still live from an earlier visit is not
   republished (81 decisions; the viewer still sees it, so this is not a new choice). Forced no-actions (stale
   offline-buffered opens, unknown or contentless dialect, duplicates) are recorded separately.
4. **One goal at a time.** A goal is committed when its first action publishes. Later visits continue it. It is
   re-planned only when the viewer's situation changes: a success signal arrives, a purchase or renewal, a lapse,
   an opt-out, or the app's dialect changes.
5. **Page and follow-up.** Titles come deterministically from the catalogue in the app's dialect, available for the
   page's lifetime, one per content. A follow-up is considered only for goals scoring ≥ 0.2 and viewers who are not
   habitual. Eligible viewers are then assigned send or withhold at random (p = 0.5, stable per viewer), so the
   message effect is measured, not assumed. Copy and templates come only from the catalogue and `pricing.json`.
6. **The model is log-only.** It is consulted on every new goal choice with a real alternative, off the critical
   path. Its answer is validated (it must pick an eligible goal and cite that goal's evidence) and the verdict is
   recorded, but the agent always acts on the planner's choice. It agreed with the planner on 596 of 599 valid
   answers on the sample day; logging alone cannot show it adds value (see KNOWN_LIMITATIONS).

## Organic baseline and learning

- **The baseline.** `baseline.py` estimates p for each goal from pre-replay data only. The pilot's holdout arm gives
  true no-agent outcomes (G02, G04, G05, G06, G09). History "anchors" (fresh visits on days 8–23 and what followed)
  give G01, G03, G07, G08 and G12. Where the data supports it, p depends on the viewer (G07: episodes watched ×
  days idle; G08: play days), smoothed toward the goal's overall rate.
- **Calibration.** The holdout arm gets nothing, so its goals test the baseline (`impact_report.md`). Goals left
  alone below the floor are tracked too, otherwise a skipped goal could never be checked. The baseline is close
  for G01, G02 and G12 and badly off for G05, G07, G08 and G09: the history and the simulator behave differently.
  In sample its Brier score is 0.157, worse than a constant (0.118).
- **Learning (`oua learn`).**
  - Holdout outcomes update the baseline. The older prior is capped at 100 pseudo-observations, so current evidence
    can move it. Checked out of sample (learn from half the holdout, split by viewer, score the other half): Brier
    0.157 before, 0.111 after, 0.119 for a constant. Almost all of the gain is G07 and G09.
  - First-paid-plan (G05) was predicted at 0.6% and observed at 5.7% among the 105 holdout viewers the floor left
    alone. Learning moves it to 3.2%, which clears the floor, so on day 2 the agent pursues it.
  - A goal pursued for 100+ viewers per arm whose measured lift has a 95% upper bound below 3 pp has its score
    halved. On day 1 that is G07, resume a series: lift −1.1 pp, upper bound +1.7 pp. This rule rests on the
    measured lift alone; it is not checked out of sample.
- **Per-viewer learning.** An invalid push token or an opt-out suppresses that channel. A goal that failed despite
  exposure scores −0.1 next time.
- **Across days (`oua two-day`).** An unseen day 2 is replayed from day 1's memory, with and without the learned
  policy. The difference (`learning_report.md`) is what learning changed: 232 decisions (208 in what the viewer
  gets), mostly toward G05 and away from G07. Whether they do better is not measured, because the simulator cannot
  run day 2.

## Memory and sources

| Fact | Rule |
|---|---|
| Event time | `received_at − (sent_at − occurred_at)`: both phone timestamps share one clock, so a wrong clock cancels. A trigger buffered more than 600 s is stale (the viewer is gone) |
| Subscription | billing events after the snapshot > the snapshot row consistent with billing history > a single row > *unresolved* (status goals blocked). A trial counts only for its 3 days |
| Consent | an opt-out after the row was written beats the snapshot; an unrecognised opt-out channel means all channels; no row means no consent |
| Display dialect | the trigger's app setting (the app only shows its own dialect's page); profile and campaign are recorded as conflicts |

SQLite holds the state documents, an append-only compressed journal (`explain VIEWER --at T` shows what was known
at T), processed trigger ids (idempotency), and goal and title indexes, so a rule correction or withdrawal touches
only what it affects.

## Verification: three independent lines

1. **Pre-publish checker (`verify.py`).** It re-reads raw rows with its own, more conservative code and can
   overrule the planner (it does, on contradictory signup data). Checks: holdout, idempotency, eligibility,
   evidence resolves to real rows (never model text), freshness, dialect, availability, page rules, quarantine,
   consent, quiet hours, 1 message per IST day across channels, approved template in the viewer's language, copy
   (normalised; forbidden phrases, only real prices, no offers, resume wording only about a started series), and a
   runaway cap on messages per send hour. Only checks that ran are listed as passed.
2. **Publish- and send-time re-checks.** Withdrawals landing during a decision are removed before publishing.
   Queued messages are re-checked at send time against everything known then, including a later visit in another
   dialect.
3. **Independent audit (`audit.py`).** Shares no code with the agent and re-checks every action and decision. It
   found 0 problems on the sample day and both day-2 runs. On an unseen day at 3x load it flagged 211 breaches in
   the pre-review code's output; the fixed code had 0.

## Latency and failure

Publish time = delivery + queue + simulated dependency latency + measured compute (`oua two-day` uses a fixed 5 ms
instead, and builds day 2 from a fixed seed, so its comparison reproduces). The real constraint is the
viewer reaching home 3–40 s after opening, not the 60 s SLO, so the critical path is deterministic (two budgeted
reads, about 1 ms of planning) and the model is off it.

| Failure | Response |
|---|---|
| Duplicate / same-session trigger | recorded, never acted on twice |
| Offline-buffered trigger | no reads, no model, no page |
| Data store slow or down (1 s timeout) | cached read (flagged, no messages); else a non-personalised popular page and no goal; else nothing |
| Model 503 / timeout / malformed / unsupported claim | recorded and rejected; circuit opens after 3 failures for 120 s |
| Title withdrawn | the newest live page is re-published without it; older pages are superseded, never resurrected; queued messages are cancelled |
| Goal rule corrected | the scope is parsed from the wording (ambiguous → new goals only, flagged); goals whose window was running are re-pointed with the same rule function the judge uses. Stress run (`perturb --correct-goal G07`): exactly the 249 goals whose window was running, 0 others |
| Purchase, renewal, lapse, opt-out, dialect change | re-plan; only the affected goal's follow-up is cancelled; both arms' goals change identically |

Simulated load: 1,000 viewers in a minute and a 5x day stay inside the page budget. These are virtual-clock numbers,
not a real load test.

## Trust boundaries and scale

- **Data is content.** Descriptions never reach a prompt. Instruction-like metadata (T0002, T0522) is quarantined
  for human review. The detector normalises text first and spares ordinary plot text.
- **The model is untrusted advice.** It sees aliases, not ids or free text, and it is never followed.
- **The pack stays local.** It is treated as personal data, the default provider is a mock, and the audit viewer is
  a local file.
- **Never automated:** writing goal rules, releasing quarantined content, prices and offers, contacting opted-out
  viewers, rolling out on an impact estimate.
- **At 10 million viewers and 40 dialects:**
  - partition the event bus by viewer, with one writer per partition;
  - keep viewer documents in a key-value store;
  - precompute per-dialect candidate pools;
  - refit the baseline nightly from the holdout;
  - keep a persistent holdout with sequential tests.
