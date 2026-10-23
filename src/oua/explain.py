"""`oua explain VIEWER [--at T]`: what the agent knew, decided and observed for one viewer, as of any moment.

Reads only the append-only journal in agent_state.sqlite, so the answer for time T contains nothing the agent learned
after T (outcome judgments appear only after the observation window closes)."""
import json
import os
import sqlite3

from .memory import journal_doc


def _short(s, n=160):
    s = str(s)
    return s if len(s) <= n else s[:n - 3] + '...'


def _from_journal(path, viewer, at):
    db = sqlite3.connect(path)
    q = 'SELECT at, kind, ref, doc FROM journal WHERE user_id=?'
    args = [viewer]
    if at:
        q += ' AND at<=?'
        args.append(at)
    rows = [(t, k, r, journal_doc(d)) for t, k, r, d in db.execute(q + ' ORDER BY seq', args).fetchall()]
    db.close()
    return rows, 'agent_state.sqlite journal'


def _from_files(run_dir, viewer, at):
    """Fallback for a fresh clone (the SQLite memory is not committed): rebuild the same timeline from the committed
    decisions.jsonl, viewer_state/<viewer>.json and outcomes.jsonl."""
    rows = []
    vs = os.path.join(run_dir, 'viewer_state', viewer + '.json')
    state = {}
    if os.path.exists(vs):
        with open(vs, encoding='utf-8') as f:
            state = json.load(f)
    for gi in state.get('goal_instances', []):
        if not gi.get('shadow'):
            rows.append((gi['started_at'], 'goal_committed', gi['instance_id'], gi))
    for a in state.get('actions_detail', []):
        kind = 'action_published' if a['type'] == 'home_page' else 'action_queued'
        rows.append((a.get('published_at') or '', kind, a['action_id'], a))
    path = os.path.join(run_dir, 'decisions.jsonl')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                if '"%s"' % viewer not in line:
                    continue
                d = json.loads(line)
                if d['viewer_id'] == viewer:
                    kind = {'trigger': 'decision', 'replan': 'replan', 'control': 'control_decision'}[d['kind']]
                    rows.append((d.get('published_at') or d.get('decided_at'), kind, d['decision_id'], d))
    path = os.path.join(run_dir, 'outcomes.jsonl')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                if '"%s"' % viewer in line:
                    o = json.loads(line)
                    if o['viewer_id'] == viewer and not o.get('shadow'):
                        rows.append((o['judged_at'], 'outcome', o['goal_instance_id'], o))
    order = {'goal_committed': 0, 'decision': 1, 'action_published': 2, 'action_queued': 2}
    rows.sort(key=lambda r: (r[0], order.get(r[1], 3)))
    if at:
        rows = [r for r in rows if r[0] <= at]
    return rows, 'committed files (decisions.jsonl, viewer_state, outcomes.jsonl); run `run-all` for the full journal'


def explain(run_dir, viewer, at=None):
    path = os.path.join(run_dir, 'agent_state.sqlite')
    if os.path.exists(path):
        rows, source = _from_journal(path, viewer, at)
    elif os.path.exists(os.path.join(run_dir, 'decisions.jsonl')):
        rows, source = _from_files(run_dir, viewer, at)
    else:
        return ('%s has no agent memory and no decisions. Build a run with:\n'
                '  python main.py run-all --out %s' % (run_dir, run_dir))
    if not rows:
        return 'no journal entries for %s%s' % (viewer, ' before ' + at if at else '')
    out = ['Viewer %s%s' % (viewer, ' as known at ' + at if at else ' (full history)'),
           'source: ' + source, '=' * 72]
    last_facts = None
    for t, kind, ref, d in rows:
        if kind == 'decision':
            out.append('')
            trig = d.get('trigger') or {}
            out.append('%s  DECISION %s  [%s]  %s' % (t, ref, d.get('status'), d.get('action')))
            if trig:
                out.append('    trigger: %s %s, delivered %s, event age %ss, buffered %ss, app dialect %r' % (
                    trig.get('event'), trig.get('event_id'), trig.get('deliver_at'), trig.get('event_age_s'),
                    trig.get('buffered_s'), trig.get('app_dialect_raw')))
            if d.get('facts'):
                last_facts = d['facts']
            if d.get('dialect'):
                dl = d['dialect']
                out.append('    dialect: used %s (%s); conflicts %s' % (dl.get('used'), dl.get('source'),
                                                                      dl.get('conflicts') or 'none'))
            gs = d.get('goal_selection') or {}
            if gs.get('considered'):
                el = [c for c in gs['considered'] if c['eligible']]
                out.append('    eligible goals: %s' % ', '.join('%s(%.2f)' % (c['goal_id'], c.get('score', 0))
                                                             for c in el) or 'none')
            if gs.get('model'):
                m = gs['model']
                out.append('    model: %s %s' % (m.get('outcome'), _short(m.get('detail', ''), 120)))
            if d.get('goal') and d['goal'].get('id'):
                g = d['goal']
                out.append('    goal: %s %s (%s) until %s, judged by %s' % (
                    g['id'], g.get('name'), g.get('committed'), g.get('deadline'),
                    json.dumps(g.get('success_signal'))))
            if d.get('confidence') is not None:
                out.append('    confidence %.2f: %s' % (d['confidence'], d.get('confidence_reason')))
            if d.get('checks'):
                ch = d['checks']
                out.append('    checks passed: %s%s' % (', '.join(ch.get('passed', [])),
                                                         ('; FAILED: ' + '; '.join(
                                                             '%s (%s)' % (f['check'], _short(f['reason'], 80))
                                                             for f in ch['failed'])) if ch.get('failed') else ''))
            if d.get('no_action_reason'):
                out.append('    no action because: %s' % _short(d['no_action_reason'], 300))
            if d.get('why_not'):
                out.append('    not done: %s' % d['why_not'])
            if d.get('latency'):
                out.append('    latency: %s' % d['latency'])
        elif kind == 'goal_committed':
            out.append('%s  GOAL COMMITTED %s %s (rule %s) deadline %s' % (t, ref, d['goal_id'], d['rule_version'],
                                                                         d['deadline']))
        elif kind in ('action_published', 'action_queued'):
            status = (' [%s]' % d['status']) if d.get('status') else ''
            if d['type'] == 'home_page':
                rows = d.get('rows')
                out.append('%s  PAGE %s%s%s%s' % (
                    t, ref, status, (' in %s' % d['dialect']) if d.get('dialect') else '',
                    (': hero %s, rows %s' % (d.get('hero_title_id'), [(r['row_type'], len(r['title_ids']))
                                                                      for r in rows])) if rows is not None else ''))
            else:
                out.append('%s  %s %s%s for %s%s' % (t, d['type'].upper(), ref, status or ' QUEUED', d.get('send_at'),
                                                   (' about %s' % d['title_id']) if d.get('title_id') else ''))
        elif kind == 'message_released':
            out.append('%s  MESSAGE SENT %s (re-checked at send time)' % (t, ref))
        elif kind == 'message_cancelled':
            out.append('%s  MESSAGE CANCELLED %s: %s' % (t, ref, d.get('reason')))
        elif kind == 'replan':
            out.append('')
            out.append('%s  RE-PLAN %s after %s: %s; cancelled %s' % (t, ref, d['cause']['event'],
                                                                    '; '.join(d['changes']),
                                                                    [c['action_id'] for c in d['cancelled_actions']]))
        elif kind == 'control_decision':
            out.append('%s  CONTROL %s: %s' % (t, ref, d.get('why') or d.get('no_action_reason')))
        elif kind == 'abstention_tracked':
            out.append('%s  LEFT ALONE %s (p without agent %s); outcome tracked until %s to check the baseline' % (
                t, d['goal_id'], d.get('p_without_agent'), d['deadline']))
        elif kind == 'goal_rule_corrected':
            out.append('%s  RULE CORRECTED for %s: now %s' % (t, ref, d['new_rule']))
        elif kind == 'outcome':
            out.append('')
            out.append('%s  OUTCOME %s %s: %s (%s). %s' % (t, ref, d['goal_id'], d['status'].upper(),
                                                           d.get('rule_version_used'), d.get('attribution')))
            for l in d.get('learned', []):
                out.append('    learned: %s' % l['text'])
    if last_facts:
        out.append('')
        out.append('Facts at the last decision (value, source, age):')
        for k, v in last_facts.items():
            extra = ('  conflicts: %s -> %s' % (v['conflicts'], _short(v.get('resolution'), 100))
                     if v.get('conflicts') else '')
            out.append('    %-20s %-28s %-14s %s%s' % (k, _short(v.get('value'), 28), v.get('source'),
                                                      ('%ss' % v['age_s']) if 'age_s' in v else '', extra))
    return '\n'.join(out)
