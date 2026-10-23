"""Replay harness: feeds triggers, live events and control events to the agent in delivery order.

Time model: the replay runs faster than real time on a virtual clock. Each decision's publish time is
    deliver_at + queue wait + simulated dependency latency (data store, model) + measured compute time.
A pool of N workers (config latency.workers) is simulated, so bursts build real queues, and a viewer's decisions are
serialised (a new trigger for a viewer waits for that viewer's in-flight decision).
"""
import json
import os
import platform
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import timedelta

from .agent import Agent
from .baseline import OrganicBaseline
from .content import ContentIndex
from .deps import DataStoreClient
from .events import EventStore, normalize
from .goals import GoalRegistry, Planner
from .llm import ModelGateway, make_provider
from .memory import Memory
from .pack import Pack, load_json, read_jsonl
from .timeutil import IST, add_ms, iso, ist_hour, parse
from .verify import Verifier


def load_config(path=None):
    path = path or os.path.join(os.path.dirname(__file__), '..', '..', 'config', 'agent.json')
    with open(path, encoding='utf-8') as f:
        return json.load(f)


class Context:
    pass


def public(action):
    return {k: v for k, v in action.items() if not k.startswith('_')}


class Replay:
    def __init__(self, pack_dir, out_dir, cfg=None, provider='mock', log=print, learned=None, carry_state=None):
        self.cfg = cfg or load_config()
        self.out = out_dir
        self.log = log
        os.makedirs(out_dir, exist_ok=True)
        t0 = time.perf_counter()
        ctx = Context()
        ctx.cfg = self.cfg
        ctx.pack = Pack(pack_dir)
        ctx.events = EventStore()
        n_hist = ctx.events.load_history(ctx.pack.paths['events'])
        # Carrying state from an earlier run: what the platform saw since (that run's simulated outcomes) is
        # history now, and the agent keeps its memory of every viewer (goals, outcomes, learnings).
        self.carry_state = carry_state
        if carry_state:
            extra = os.path.join(carry_state, 'sim', 'outcome_events.jsonl')
            if os.path.exists(extra):
                n_hist += ctx.events.load_history(extra)
        self.learned = learned or {}
        self.triggers = [normalize(r, 'trigger') for r in read_jsonl(ctx.pack.paths['triggers'])]
        self.live = [normalize(r, 'live') for r in read_jsonl(ctx.pack.paths['live'])]
        self.controls = list(read_jsonl(ctx.pack.paths['control'])) if os.path.exists(ctx.pack.paths['control']) \
            else []
        times = ([t.known_at for t in self.triggers] or [e.known_at for e in self.live]
                 or [parse(c['at']) for c in self.controls])
        if not times:
            raise SystemExit('oua: the replay files contain no triggers, live events or control events')
        first = min(times)
        day = first.astimezone(IST).date()
        from .timeutil import at_ist
        self.replay_start = at_ist(day, 0)
        observed = {e.event for evs in ctx.events.by_user.values() for e in evs}
        ctx.registry = GoalRegistry(ctx.pack.goals, observed)
        ctx.content = ContentIndex(ctx.pack, self.cfg, ctx.events, self.replay_start)
        t_b = time.perf_counter()
        table_path = (self.cfg.get('baseline') or {}).get('table')
        if self.learned.get('baseline_table'):
            table = self.learned['baseline_table']          # learned from an earlier day wins over config
        else:
            table = load_json(table_path) if table_path else None
        self.cfg['_goal_multipliers'] = self.learned.get('goal_multipliers', {})
        ctx.baseline = OrganicBaseline(ctx.pack, self.cfg, ctx.events, self.replay_start, table=table)
        self.baseline_s = time.perf_counter() - t_b
        ctx.planner = Planner(ctx.pack, self.cfg, ctx.registry, ctx.content, ctx.baseline)
        state_path = os.path.join(out_dir, 'agent_state.sqlite')
        if carry_state:
            if os.path.exists(state_path):
                os.remove(state_path)
            shutil.copyfile(os.path.join(carry_state, 'agent_state.sqlite'), state_path)
        ctx.memory = Memory(state_path, fresh=not carry_state)
        ctx.verifier = Verifier(ctx.pack, self.cfg, ctx.events, ctx.content, ctx.memory)
        ctx.datastore = DataStoreClient(ctx.pack, ctx.events)
        ctx.provider = make_provider(provider, ctx.pack.paths['faults'])
        ctx.gateway = ModelGateway(ctx.provider, self.cfg)
        ctx.log_event = self._log_event
        ctx.emit_decision = self._emit_decision
        self.ctx = ctx
        self.agent = Agent(ctx)
        self.run_log = []
        self.decisions = []
        self.actions = []
        self.control_impacts = []
        self.setup_s = time.perf_counter() - t0
        self.n_hist = n_hist
        self.provider_name = provider

    def _log_event(self, kind, uid, doc):
        self.run_log.append({'kind': kind, 'viewer_id': uid, **doc})

    def _emit_decision(self, rec):
        self.decisions.append(rec)

    def _emit_actions(self, actions):
        for a in actions:
            self.actions.append(public(a))

    # ------------------------------------------------------------------ stream
    def stream(self):
        items = []
        for i, c in enumerate(self.controls):
            items.append((parse(c['at']), 0, i, 'control', c))
        for i, e in enumerate(self.live):
            items.append((e.known_at, 1, i, 'live', e))
        for i, e in enumerate(self.triggers):
            items.append((e.known_at, 2, i, 'trigger', e))
        items.sort(key=lambda x: (x[0], x[1], x[2]))
        return items

    def run(self):
        ctx, agent, cfg = self.ctx, self.agent, self.cfg
        workers = [self.replay_start] * cfg['latency']['workers']
        busy = {}
        fixed_ms = cfg['latency'].get('fixed_compute_ms')
        wall0 = time.perf_counter()
        now = self.replay_start
        items = self.stream()
        withdrawals = sorted((parse(c['at']), c['title_id']) for c in self.controls if c['type'] == 'title_withdrawn')
        for at, _, _, kind, item in items:
            now = at
            if kind == 'control':
                # A control takes effect at its instant: apply it before releasing messages due at the same moment.
                self._control(item, now)
                self._emit_actions(agent.release_due(now))
            elif kind == 'live':
                self._emit_actions(agent.release_due(now))
                if not ctx.events.ingest(item):
                    self.run_log.append({'kind': 'duplicate_live_event', 'event_id': item.event_id})
                    continue
                rec = agent.on_event(item, now)
                if rec:
                    self.decisions.append(rec)
            else:
                self._emit_actions(agent.release_due(now))
                ctx.events.ingest(item)   # a redelivered trigger is deduplicated here and again by the agent
                # Per-viewer serialisation: the job is ready when the viewer's previous decision is done. Take the
                # worker that frees up latest but still by then (best fit), so waiting for one viewer does not
                # hold a worker that other viewers could use; otherwise the earliest-free worker.
                ready = max(now, busy.get(item.user_id, now))
                fits = [w for w in workers if w <= ready]
                free = max(fits) if fits else min(workers)
                workers.remove(free)
                start = max(ready, free)
                queue_ms = int((start - now).total_seconds() * 1000)
                t = time.perf_counter()
                p = agent.decide(item, start, queue_ms)
                compute_ms = (time.perf_counter() - t) * 1000
                # The virtual clock normally advances by the measured compute time. A fixed value makes a replay
                # reproduce exactly (used by `oua two-day`, whose two runs are compared decision by decision).
                clock_ms = fixed_ms if fixed_ms is not None else compute_ms
                published = add_ms(start, p.deps_ms + clock_ms)
                workers.append(published)
                busy[item.user_id] = published
                # Withdrawals that land between this decision's start and its publish time are known to the
                # catalogue at publish time; pre-apply them so the publish-time re-check can drop those titles.
                for w_at, tid in withdrawals:
                    if now < w_at <= published:
                        agent.content.withdraw(tid, w_at)
                timing = {'queue_ms': queue_ms, 'dependency_ms': p.deps_ms, 'compute_ms': round(clock_ms, 2),
                          'model_off_path_ms': getattr(p, 'shadow_model_ms', 0),
                          'decided_in_ms': int(round((published - item.known_at).total_seconds() * 1000))}
                pages = agent.finalize(p, published, timing)
                self.decisions.append(p.rec)
                self._emit_actions(pages)
            ctx.memory.commit()
        end = max(now, self.replay_start + timedelta(days=1))
        self._emit_actions(agent.flush_outbox(end))
        ctx.memory.commit()
        self.wall_s = time.perf_counter() - wall0
        return self

    def close(self):
        self.ctx.memory.close()

    def _control(self, c, now):
        t = c['type']
        if t == 'title_withdrawn':
            impact, published = self.agent.on_title_withdrawn(c, now)
            self._emit_actions(published)
            self.control_impacts.append(impact)
        elif t == 'goal_rule_corrected':
            self.control_impacts.append(self.agent.on_goal_rule_corrected(c, now))
        elif t in ('dependency_unavailable', 'dependency_degraded'):
            # Harness-only: the agent is not told; it has to notice the symptoms.
            if c['dependency'] == 'model_provider':
                self.ctx.provider.unavailable_until = parse(c['until'])
            else:
                self.ctx.datastore.add_window(c)
            self.control_impacts.append({'type': t, 'dependency': c['dependency'], 'at': c['at'],
                                         'until': c['until'], 'handled_by': 'harness fault injection'})
        else:
            self.control_impacts.append({'type': t, 'at': c.get('at'), 'note': 'unknown control type; ignored'})

    # ------------------------------------------------------------------ outputs
    def write(self):
        out = self.out
        dec = sorted(self.decisions, key=lambda d: (d.get('published_at') or d.get('decided_at'), d['decision_id']))
        with open(os.path.join(out, 'decisions.jsonl'), 'w', encoding='utf-8') as f:
            for d in dec:
                f.write(json.dumps(d, ensure_ascii=False, sort_keys=True, default=str, separators=(',', ':')) + '\n')
        acts = sorted(self.actions, key=lambda a: (a['published_at'], a['action_id']))
        with open(os.path.join(out, 'actions.jsonl'), 'w', encoding='utf-8') as f:
            for a in acts:
                f.write(json.dumps(a, ensure_ascii=False, sort_keys=True) + '\n')
        with open(os.path.join(out, 'control_impact.json'), 'w', encoding='utf-8') as f:
            json.dump(self.control_impacts, f, indent=2, default=str)
        with open(os.path.join(out, 'run_log.jsonl'), 'w', encoding='utf-8') as f:
            for r in self.run_log:
                f.write(json.dumps(r, default=str) + '\n')
        with open(os.path.join(out, 'baseline_table.json'), 'w', encoding='utf-8') as f:
            json.dump(self.ctx.baseline.table(), f, indent=1)
        lat = self.latency_report(dec)
        with open(os.path.join(out, 'latency_report.json'), 'w', encoding='utf-8') as f:
            json.dump(lat, f, indent=2)
        manifest = {
            'command': ' '.join(sys.argv), 'python': platform.python_version(), 'provider': self.provider_name,
            'pack': self.ctx.pack.root, 'pack_fingerprint': self.ctx.pack.fingerprint(), 'config': self.cfg,
            'history_events_loaded': self.n_hist, 'history_duplicates_dropped': self.ctx.events.duplicates,
            'replay_start': iso(self.replay_start), 'setup_s': round(self.setup_s, 2),
            'replay_wall_s': round(self.wall_s, 2), 'decisions': len(dec), 'actions': len(acts),
            'carry_state': os.path.abspath(self.carry_state) if self.carry_state else None,
            'learned': {'goal_multipliers': self.learned.get('goal_multipliers', {}),
                        'baseline_changes': self.learned.get('baseline_changes', {})} if self.learned else None,
            'quarantined_titles': self.ctx.content.quarantined,
            'organic_baseline': self.ctx.baseline.table(), 'baseline_fit_s': round(self.baseline_s, 2),
            'goal_corrections': self.ctx.registry.corrections,
            'unmeasurable_goals': {g: self.ctx.registry.measurable(g)[1] for g in self.ctx.registry.goals
                                   if not self.ctx.registry.measurable(g)[0]},
            'model_gateway': self.ctx.gateway.stats, 'data_store': self.ctx.datastore.stats}
        with open(os.path.join(out, 'run_manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(manifest, f, indent=2, default=str)
        return manifest

    def latency_report(self, dec):
        slo = self.cfg['latency']['publish_slo_ms']
        fail = self.cfg['latency']['hard_fail_ms']
        trig = [d for d in dec if d['kind'] == 'trigger' and d.get('latency')]

        def pct(xs, q):
            if not xs:
                return None
            return xs[min(len(xs) - 1, int(q * len(xs)))]

        def summary(ds):
            xs = sorted(d['decided_in_ms'] for d in ds)
            return {'n': len(xs), 'p50_ms': pct(xs, 0.5), 'p95_ms': pct(xs, 0.95), 'p99_ms': pct(xs, 0.99),
                    'max_ms': xs[-1] if xs else None, 'over_60s': sum(x > slo for x in xs),
                    'over_5min': sum(x > fail for x in xs)}
        pages = [d for d in trig if str(d.get('action', '')).startswith('HOME_PAGE')]
        by_status = defaultdict(list)
        for d in trig:
            by_status[d['status']].append(d)
        by_hour = defaultdict(list)
        for d in trig:
            by_hour['%02d' % ist_hour(parse(d['trigger']['deliver_at']))].append(d)
        windows = {}
        for c in self.controls:
            if c['type'].startswith('dependency'):
                a, b = parse(c['at']), parse(c['until'])
                ds = [d for d in trig if a <= parse(d['trigger']['deliver_at']) < b]
                windows['%s %s (%s-%s UTC)' % (c['type'], c['dependency'], c['at'][11:16], c['until'][11:16])] = dict(
                    summary(ds), statuses=dict(Counter(d['status'] for d in ds)),
                    model_outcomes=dict(Counter(str(d.get('goal_selection', {}).get('model', {}).get('outcome'))
                                                for d in ds)),
                    degraded_decisions=sum(1 for d in ds if d.get('degraded')))
        # Busiest 10 minutes, found from the data rather than assumed: once over all triggers (this catches the
        # offline-buffer burst) and once over fresh triggers only (the real evening peak).
        stale_s = self.cfg['freshness']['stale_trigger_s']
        for label, pool in (('all triggers', trig),
                            ('fresh triggers only', [d for d in trig if d['trigger']['buffered_s'] <= stale_s])):
            times = sorted(parse(d['trigger']['deliver_at']) for d in pool)
            best, j = (0, None), 0
            for i, t in enumerate(times):
                while times[j] < t - timedelta(minutes=10):
                    j += 1
                if i - j + 1 > best[0]:
                    best = (i - j + 1, t)
            if best[1] is None:
                continue
            a, b = best[1] - timedelta(minutes=10), best[1]
            ds = [d for d in pool if a < parse(d['trigger']['deliver_at']) <= b]
            avg = len(pool) / 144.0
            windows['busiest 10 minutes, %s (%s-%s IST)' % (label, a.astimezone(IST).strftime('%H:%M'),
                                                          b.astimezone(IST).strftime('%H:%M'))] = dict(
                summary(ds), traffic_vs_daily_average=round(len(ds) / avg, 1),
                max_queue_ms=max((d['latency']['queue_ms'] for d in ds), default=0),
                statuses=dict(Counter(d['status'] for d in ds)))
        return {
            'definition': 'decided_in_ms = published_at - deliver_at = queue wait + simulated dependency latency + '
                          'measured compute. Includes duplicates and no-action decisions (they are decisions too).',
            'slo': {'publish_within_ms': slo, 'hard_fail_ms': fail,
                    'page_useful_within_ms': 3000,
                    'why_page_target': 'the simulator shows a personalised page only if it is live when the viewer '
                                       'reaches home, 3-40 s after opening the app'},
            'workers': self.cfg['latency']['workers'],
            'all_trigger_decisions': summary(trig),
            'home_page_decisions': dict(summary(pages), published_within_3s=sum(
                d['decided_in_ms'] <= 3000 for d in pages)),
            'by_status': {k: summary(v) for k, v in sorted(by_status.items())},
            'by_hour_ist': {k: summary(v) for k, v in sorted(by_hour.items())},
            'windows': windows,
            'model_gateway': self.ctx.gateway.stats,
            'data_store': self.ctx.datastore.stats,
            'max_queue_ms': max((d['latency']['queue_ms'] for d in trig), default=0),
            'replay_wall_clock_s': round(self.wall_s, 2),
        }
