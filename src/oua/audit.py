"""Independent run auditor: `oua audit --run sample_run`.

A third line of defence after the planner and the pre-publish verifier. It reads only the raw pack files and the run's
output files (decisions.jsonl, actions.jsonl) with its own code, and re-checks every published action against the
policy. It shares no logic with the agent, so a bug in the agent's checks cannot hide here.
"""
import csv
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
ZW = dict.fromkeys(map(ord, '​‌‍⁠﻿­'))
AMOUNT = re.compile(r'(?:rs\.?|inr|₹)\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:rs\b|rupees?\b|inr\b|'
                    r'₹|/-)')
OFFERS = re.compile(r'\d+\s*%\s*off\b|\bdiscount|\bcashback\b|\bcoupon\b|\bpromo code\b')
EPISODE_REF = re.compile(r'(?:season\s*(\d+)\D{0,12})?episode\s*(\d+)')


def norm(text):
    s = unicodedata.normalize('NFKC', text or '').translate(ZW).lower()
    return re.sub(r'\s+', ' ', re.sub(r'[\-_‐-―]+', ' ', s)).strip()


def instruction_like(text):
    """The audit's own detector (not shared with the agent): prompt markers, rule-override wording aimed at an
    AI, or instructions to message everyone."""
    s = norm(text)
    marker = re.search(r'system (note|prompt|message|instruction)|\[(assistant|system|ai|agent)\b|pre ?approved|'
                       r'skip (all )?(the )?checks', s)
    override = re.search(r'\b(ignore|disregard|override|bypass|forget|skip)\b[^.]{0,60}\b(rules?|instructions?|'
                         r'checks?|polic|consent|guardrail|filter|opt ?out|opted out)', s)
    bulk = re.search(r'\b(send|message|notify|push|whatsapp|text|email|sms)\b[^.]{0,80}\b(every ?one|everybody|'
                     r'every (user|viewer|subscriber|customer)|all (users|viewers|subscribers|customers))', s)
    ai = re.search(r'\b(ai|llm|language model|assistant|agent|bot|chatbot)s?\b', s)
    this = re.search(r'\bthis (title|show|film|series|micro drama)\b', s)
    return bool(marker or (override and ai) or (override and bulk) or (bulk and this))


def optout_scope(channel):
    c = norm(str(channel or '')).replace(' ', '')
    if c in ('push', 'notification', 'notifications', 'pushnotification'):
        return {'push'}
    if c in ('whatsapp', 'wa'):
        return {'whatsapp'}
    return {'push', 'whatsapp'}


def copy_issues(text, prices, forbidden):
    s = norm(text)
    out = ['forbidden_phrase' for f in forbidden if f in s]
    for m in AMOUNT.finditer(s):
        v = float((m.group(1) or m.group(2)).replace(',', ''))
        if v != int(v) or int(v) not in prices:
            out.append('invented_price')
    if OFFERS.search(s):
        out.append('offer_language')
    return out


def _csv(path):
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def t(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')) if s else None


def jl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        return [json.loads(l) for l in f if l.strip()]


def audit(pack_dir, run_dir, slo_ms=60000, fail_ms=300000):
    P = lambda *a: os.path.join(pack_dir, *a)
    cat = {r['title_id']: r for r in _csv(P('data', 'catalogue.csv'))}
    profiles = defaultdict(list)
    for r in _csv(P('data', 'profiles_snapshot.csv')):
        profiles[r['user_id']].append(r)
    arms = {r['user_id']: r['arm'] for r in _csv(P('replay', 'arms.csv'))}
    iv = _json(P('config', 'interventions.json'))
    prices = {p['price_inr'] for p in _json(P('config', 'pricing.json'))['plans']}
    templates = {x['template_id']: x for x in iv['whatsapp']['approved_templates']}
    forbidden = [norm(x) for x in iv['push']['copy']['forbidden_phrases']]
    episodes = defaultdict(list)
    for r in _csv(P('data', 'episodes.csv')):
        episodes[r['content_slug']].append((int(r['season']), int(r['episode'])))
    triggers = {}
    for e in jl(P('replay', 'triggers.jsonl')):
        triggers.setdefault(e['event_id'], e)
    withdrawn = {c['title_id']: t(c['at']) for c in jl(P('replay', 'control_events.jsonl'))
                 if c['type'] == 'title_withdrawn'}
    opt_outs = defaultdict(list)
    delivered = defaultdict(list)
    dialect_feed = defaultdict(list)     # (received_at, dialect the app reported)
    plays = defaultdict(list)            # (received_at, content_slug)
    known_events = set(triggers)
    for e in triggers.values():
        if e.get('app_dialect') is not None:
            dialect_feed[e['user_id']].append((t(e['received_at']), e['app_dialect']))
    history = [P('data', 'events.jsonl'), P('replay', 'live_events.jsonl')]
    manifest = os.path.join(run_dir, 'run_manifest.json')
    carried = _json(manifest).get('carry_state') if os.path.exists(manifest) else None
    if carried:   # a run that continued from an earlier one: that run's outcomes are history now
        history.append(os.path.join(carried, 'sim', 'outcome_events.jsonl'))
    for path in history:
        for e in jl(path):
            known_events.add(e['event_id'])
            uid = e['user_id']
            if e['event'] == 'notification_opt_out':
                opt_outs[uid].append((t(e['received_at']), optout_scope(e['properties'].get('channel'))))
            if e['event'] == 'message_delivered':
                delivered[uid].append(t(e['received_at']).astimezone(IST).date())
            if e['event'] == 'app_dialect_changed':
                dialect_feed[uid].append((t(e['received_at']), e['properties'].get('to')))
            elif e.get('app_dialect') is not None:
                dialect_feed[uid].append((t(e['received_at']), e['app_dialect']))
            if e['event'] == 'play_started' and e['properties'].get('title_id') in cat:
                plays[uid].append((t(e['received_at']), cat[e['properties']['title_id']]['content_slug']))
    decisions = jl(os.path.join(run_dir, 'decisions.jsonl'))
    actions = jl(os.path.join(run_dir, 'actions.jsonl'))
    dec_by_id = {d['decision_id']: d for d in decisions}
    problems = defaultdict(list)
    checked = Counter()

    def available(tid, at):
        r = cat.get(tid)
        if not r:
            return False
        if t(r['available_from']) > at or (r['available_until'] and at >= t(r['available_until'])):
            return False
        return not (tid in withdrawn and withdrawn[tid] <= at)

    pages_per_trigger = Counter()
    msgs_per_day = Counter()
    for a in actions:
        uid = a['user_id']
        checked['actions'] += 1
        if arms.get(uid) != 'agent':
            problems['action_on_non_agent_arm'].append(a['action_id'])
        d = dec_by_id.get(a['decision_id'])
        if d is None or d.get('status') not in ('PUBLISHED', 'PUBLISHED_FALLBACK'):
            problems['action_without_published_decision'].append(a['action_id'])
        trig = triggers.get(a.get('trigger_event_id'))
        if a['type'] == 'home_page':
            checked['home_pages'] += 1
            at = t(a['published_at'])
            is_replacement = d is not None and d.get('kind') == 'control'
            if trig and not is_replacement:
                pages_per_trigger[a['trigger_event_id']] += 1
                if a['dialect'] != trig.get('app_dialect'):
                    problems['page_dialect_not_app_dialect'].append(a['action_id'])
                lat = (at - t(trig['deliver_at'])).total_seconds() * 1000
                if lat > fail_ms:
                    problems['page_published_after_5min'].append(a['action_id'])
                elif lat > slo_ms:
                    problems['page_published_after_60s'].append(a['action_id'])
            rows = a.get('rows') or []
            if len(rows) > iv['home_page']['rows']['max_rows']:
                problems['too_many_rows'].append(a['action_id'])
            slugs = set()
            ids = ([a['hero_title_id']] if a.get('hero_title_id') else []) + [x for r in rows for x in r['title_ids']]
            for r in rows:
                if r['row_type'] not in iv['home_page']['rows']['row_types']:
                    problems['row_type_not_allowed'].append(a['action_id'])
                if not 3 <= len(r['title_ids']) <= 10:
                    problems['row_size'].append(a['action_id'])
            for tid in ids:
                c = cat.get(tid)
                if c is None:
                    problems['unknown_title'].append(a['action_id'])
                    continue
                if c['dialect'] != a['dialect']:
                    problems['title_wrong_dialect'].append('%s:%s' % (a['action_id'], tid))
                if not available(tid, at):
                    problems['title_unavailable_at_publish'].append('%s:%s' % (a['action_id'], tid))
                if instruction_like(c['description']):
                    problems['quarantined_title_promoted'].append('%s:%s' % (a['action_id'], tid))
                if c['content_slug'] in slugs:
                    problems['duplicate_content_on_page'].append(a['action_id'])
                slugs.add(c['content_slug'])
        else:
            checked['messages'] += 1
            send = t(a.get('send_at') or a['published_at'])
            local = send.astimezone(IST)
            if a['type'] in ('push', 'whatsapp'):
                field = 'notifications_opt_in' if a['type'] == 'push' else 'whatsapp_opt_in'
                rows_ = profiles.get(uid, [])
                if not rows_ or any(r[field] != 'true' for r in rows_):
                    problems['message_without_consent'].append(a['action_id'])
                else:
                    latest = max(t(r['updated_at']) for r in rows_)
                    if any(a['type'] in scope and latest < at_ <= send for at_, scope in opt_outs.get(uid, [])):
                        problems['message_after_opt_out'].append(a['action_id'])
                if local.hour >= 22 or local.hour < 8:
                    problems['message_in_quiet_hours'].append(a['action_id'])
                msgs_per_day[(uid, local.date())] += 1
                if msgs_per_day[(uid, local.date())] + delivered[uid].count(local.date()) > 1:
                    problems['frequency_cap_exceeded'].append(a['action_id'])
            tid = a.get('title_id')
            c = cat.get(tid)
            if c is None or not available(tid, send):
                problems['message_title_unavailable'].append(a['action_id'])
            if trig and c and c['dialect'] != trig.get('app_dialect'):
                problems['message_title_wrong_dialect'].append(a['action_id'])
            # The dialect the app reported last before the send: a later visit can move the viewer to another one.
            feed = [x for x in dialect_feed.get(uid, []) if x[0] <= send]
            if c and feed and max(feed, key=lambda x: x[0])[1] != c['dialect']:
                problems['message_not_in_latest_app_dialect'].append(a['action_id'])
            if c and instruction_like(c['description']):
                problems['quarantined_title_promoted'].append(a['action_id'])
            if a['type'] == 'whatsapp':
                tpl = templates.get(a.get('template_id'))
                if tpl is None:
                    problems['whatsapp_template_not_approved'].append(a['action_id'])
                elif sorted(a.get('variables', {})) != sorted(tpl['variables']):
                    problems['whatsapp_variables_mismatch'].append(a['action_id'])
                else:
                    if c and a['variables'].get('title_name') not in (None, c['title']):
                        problems['whatsapp_title_name_not_catalogue'].append(a['action_id'])
                    if trig and trig.get('app_language') != tpl['language']:
                        problems['whatsapp_template_language_mismatch'].append(a['action_id'])
                    rendered = tpl['text']
                    for k, v in a['variables'].items():
                        rendered = rendered.replace('{{%s}}' % k, str(v))
                    for issue in copy_issues(rendered, prices, forbidden):
                        problems['whatsapp_' + issue].append(a['action_id'])
            if a['type'] == 'push':
                txt = a.get('text', {})
                if len(txt.get('title', '')) > 40 or len(txt.get('body', '')) > 120:
                    problems['push_copy_too_long'].append(a['action_id'])
                blob = (txt.get('title', '') + ' ' + txt.get('body', ''))
                for issue in copy_issues(blob, prices, forbidden):
                    problems['push_' + issue].append(a['action_id'])
                s = norm(blob)
                if c and (EPISODE_REF.search(s) or 'continue' in s or 'jaari' in s):
                    started = any(slug == c['content_slug'] and at_ <= send for at_, slug in plays.get(uid, []))
                    m = EPISODE_REF.search(s)
                    eps = episodes.get(c['content_slug'], [])
                    exists = True
                    if m:
                        se, ep = (int(m.group(1)) if m.group(1) else None), int(m.group(2))
                        exists = any(e == ep and (se is None or s_ == se) for s_, e in eps) and \
                            (se is not None or len({s_ for s_, _ in eps}) <= 1)
                    if c['content_type'] == 'film' or not started or not exists:
                        problems['push_resume_copy_untrue'].append(a['action_id'])
                if a.get('deeplink') != 'ott://title/%s' % tid:
                    problems['push_deeplink_mismatch'].append(a['action_id'])
    for tid, n in pages_per_trigger.items():
        if n > 1:
            problems['more_than_one_page_per_trigger'].append(tid)

    # Decisions: every trigger handled once, every no-action explained, every action evidenced.
    seen = Counter()
    for d in decisions:
        checked['decisions'] += 1
        if d['kind'] == 'trigger' and d['status'] != 'DUPLICATE_IGNORED':
            seen[d['trigger']['event_id']] += 1
        if d.get('action') == 'NO_ACTION' and not d.get('no_action_reason'):
            problems['no_action_without_reason'].append(d['decision_id'])
        if d.get('status') == 'PUBLISHED' and d['kind'] == 'trigger':
            if not d.get('goal') or not d['goal'].get('id'):
                problems['published_without_goal'].append(d['decision_id'])
            if not d.get('evidence'):
                problems['published_without_evidence'].append(d['decision_id'])
            if 'confidence' not in d or not d.get('confidence_reason'):
                problems['confidence_without_reason'].append(d['decision_id'])
            for ev in d.get('evidence', []):
                ref = ev['ref']
                if ref.startswith('event:') and ref[6:] not in known_events:
                    problems['evidence_ref_unresolvable'].append('%s:%s' % (d['decision_id'], ref))
                if ref.startswith('catalogue:') and ref[10:] not in cat:
                    problems['evidence_ref_unresolvable'].append('%s:%s' % (d['decision_id'], ref))
                if ref.startswith('model:'):
                    problems['model_output_used_as_evidence'].append(d['decision_id'])
        if d.get('status') == 'PUBLISHED_FALLBACK' and not d.get('no_goal_reason'):
            problems['fallback_without_reason'].append(d['decision_id'])
    for eid in triggers:
        if seen[eid] == 0:
            problems['trigger_without_decision'].append(eid)
        elif seen[eid] > 1:
            problems['trigger_decided_twice'].append(eid)
    report = {'checked': dict(checked), 'problem_count': sum(len(v) for v in problems.values()),
              'problems': {k: {'count': len(v), 'examples': v[:10]} for k, v in sorted(problems.items())},
              'checks': ['arm', 'decision_link', 'page_dialect', 'latency', 'row_rules', 'title_dialect',
                         'availability_incl_withdrawals', 'quarantine', 'unique_content', 'consent', 'opt_out',
                         'quiet_hours', 'frequency_cap', 'message_title', 'latest_app_dialect_at_send',
                         'whatsapp_template', 'whatsapp_language', 'rendered_whatsapp_copy', 'push_copy',
                         'invented_price', 'offer_language', 'resume_copy_truth', 'deeplink', 'one_page_per_trigger',
                         'one_decision_per_trigger',
                         'no_action_reasons', 'evidence_resolves', 'no_model_evidence']}
    with open(os.path.join(run_dir, 'audit_report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2)
    return report
