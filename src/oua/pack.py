"""Loads the evidence pack. Nothing here interprets the data beyond typing it; precedence and conflict
rules live in facts.py, and the independent checks re-read the raw rows in verify.py."""
import csv
import functools
import hashlib
import json
import os
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from .timeutil import parse

VALID_DIALECT = re.compile(r'^d\d+$')

# Text inside the data is content, never instruction (messaging_policy.md rule 6). We never put descriptions
# into a model prompt, so this detector is defence in depth: it quarantines titles whose metadata looks tampered
# with, and lists them in the ops report for a human.
#
# Flag: explicit prompt-style markers; or rule-override wording addressed to an AI/system; or override wording
# combined with bulk messaging; or bulk messaging about "this title". Plot text such as "learns to ignore the rules
# her father set" or "sends a letter to everyone in the village" is not flagged.
_MARKERS = re.compile(r'\bsystem (note|prompt|message|instruction)s?\b|\[(assistant|system|ai|agent)\b[^\]]*\]|'
                      r'\bpre ?approved\b|\bskip (all )?(the )?checks\b|\bmark every\w*\b[^.]{0,40}\beligible\b')
_AI = re.compile(r'\b(ai|llm|language model|assistant|agent|bot|chatbot)s?\b')
_OVERRIDE = re.compile(r'\b(ignore|disregard|override|bypass|forget|skip)\b[^.]{0,60}\b(rules?|instructions?|checks?|'
                       r'polic(y|ies)|consent|guardrails?|filters?|opt ?outs?|opted out)\b')
_BULK = re.compile(r'\b(send|message|notify|push|whatsapp|text|email|sms)\b[^.]{0,80}\b(every ?one|everybody|'
                   r'every (user|viewer|subscriber|customer)s?|all (users|viewers|subscribers|customers))\b')
_THIS_TITLE = re.compile(r'\bthis (title|show|film|series|micro drama)\b')
_ZW = dict.fromkeys(map(ord, '​‌‍⁠﻿­'))


def _clean(text):
    t = unicodedata.normalize('NFKC', text or '').translate(_ZW).lower()
    return re.sub(r'\s+', ' ', re.sub(r'[\-_‐-―]+', ' ', t))


@functools.lru_cache(maxsize=None)
def looks_like_instruction(text):
    t = _clean(text)
    if not t:
        return False
    bulk = bool(_BULK.search(t))
    override = bool(_OVERRIDE.search(t))
    return bool(_MARKERS.search(t) or (override and _AI.search(t)) or (override and bulk)
                or (bulk and _THIS_TITLE.search(t)))


def load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def load_csv(path):
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def read_jsonl(path):
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def norm_dialect(value):
    """App settings arrive as 'd3', 'NA', None or ''. Anything that is not a dialect code is unknown."""
    if isinstance(value, str) and VALID_DIALECT.match(value):
        return value
    return None


@dataclass
class Title:
    title_id: str
    slug: str
    name: str
    dialect: str
    language: str
    content_type: str
    genres: list
    release_date: str
    available_from: object
    available_until: object
    premium: bool
    description: str
    quarantined: bool = False
    n_episodes: int = 0
    episode_minutes: list = field(default_factory=list)

    def available(self, at):
        return self.available_from <= at and (self.available_until is None or at < self.available_until)


class Pack:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        p = lambda *a: os.path.join(self.root, *a)
        self.paths = {
            'events': p('data', 'events.jsonl'),
            'profiles': p('data', 'profiles_snapshot.csv'),
            'catalogue': p('data', 'catalogue.csv'),
            'episodes': p('data', 'episodes.csv'),
            'goals': p('config', 'goals.json'),
            'interventions': p('config', 'interventions.json'),
            'pricing': p('config', 'pricing.json'),
            'arms': p('replay', 'arms.csv'),
            'triggers': p('replay', 'triggers.jsonl'),
            'live': p('replay', 'live_events.jsonl'),
            'control': p('replay', 'control_events.jsonl'),
            'faults': p('replay', 'model_faults.jsonl'),
            'simulator': p('simulator', 'simulate_outcomes.py'),
        }
        with open(self.paths['goals'], encoding='utf-8') as f:
            self.goals = json.load(f)
        with open(self.paths['interventions'], encoding='utf-8') as f:
            self.interventions = json.load(f)
        with open(self.paths['pricing'], encoding='utf-8') as f:
            self.pricing = json.load(f)
        self._load_catalogue()
        self._load_profiles()
        self._load_arms()

    def _load_catalogue(self):
        self.titles = {}
        self.slug_titles = defaultdict(list)
        with open(self.paths['catalogue'], encoding='utf-8') as f:
            for r in csv.DictReader(f):
                t = Title(
                    title_id=r['title_id'], slug=r['content_slug'], name=r['title'], dialect=r['dialect'],
                    language=r['language'], content_type=r['content_type'], genres=r['genres'].split('|'),
                    release_date=r['release_date'], available_from=parse(r['available_from']),
                    available_until=parse(r['available_until']) if r['available_until'] else None,
                    premium=r['is_premium'] == 'true', description=r['description'],
                    quarantined=looks_like_instruction(r['description']))
                self.titles[t.title_id] = t
                self.slug_titles[t.slug].append(t.title_id)
        self.episodes = defaultdict(list)
        with open(self.paths['episodes'], encoding='utf-8') as f:
            for r in csv.DictReader(f):
                self.episodes[r['content_slug']].append((int(r['season']), int(r['episode']), r['episode_id'],
                                                         int(r['minutes'])))
        for slug, eps in self.episodes.items():
            eps.sort()
            for tid in self.slug_titles.get(slug, []):
                self.titles[tid].n_episodes = len(eps)
                self.titles[tid].episode_minutes = [e[3] for e in eps]
        self.episode_index = {}
        for slug, eps in self.episodes.items():
            for i, e in enumerate(eps):
                self.episode_index[e[2]] = (slug, i + 1, len(eps))
        self.catalogue_dialects = sorted({t.dialect for t in self.titles.values()})

    def _load_profiles(self):
        """Keeps every row. Two rows for one viewer is a conflict to resolve, not something to overwrite."""
        self.profile_rows = defaultdict(list)
        with open(self.paths['profiles'], encoding='utf-8') as f:
            for r in csv.DictReader(f):
                self.profile_rows[r['user_id']].append(r)
        for rows in self.profile_rows.values():
            rows.sort(key=lambda r: r['updated_at'])

    def _load_arms(self):
        self.arms = {}
        with open(self.paths['arms'], encoding='utf-8') as f:
            for r in csv.DictReader(f):
                self.arms[r['user_id']] = r['arm']

    def plan_price(self, plan_id):
        for p in self.pricing['plans']:
            if p['plan_id'] == plan_id:
                return p['price_inr']
        return None

    def fingerprint(self):
        """Hashes of every input file, recorded in the run manifest so a run can be reproduced."""
        out = {}
        for k, path in self.paths.items():
            if os.path.exists(path):
                h = hashlib.sha256()
                with open(path, 'rb') as f:
                    for chunk in iter(lambda: f.read(1 << 20), b''):
                        h.update(chunk)
                out[k] = h.hexdigest()[:16]
        return out
