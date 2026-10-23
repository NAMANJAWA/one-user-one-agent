"""Catalogue index: availability (including mid-day withdrawals), language variants, popularity per dialect."""
from collections import defaultdict
from datetime import timedelta

from .timeutil import parse


class ContentIndex:
    def __init__(self, pack, cfg, event_store, replay_start):
        self.pack, self.cfg = pack, cfg
        self.withdrawn = {}
        self.margin = timedelta(minutes=cfg['freshness']['title_expiry_margin_min'])
        self.by_dialect = defaultdict(set)
        for t in pack.titles.values():
            self.by_dialect[t.dialect].add(t.slug)
        self.popularity = self._popularity(event_store, replay_start)
        # popularity is fixed for the day, so the ranking per dialect is computed once
        self._ranked = {d: sorted(slugs, key=lambda s: (-self.popularity.get(s, 0), s))
                        for d, slugs in self.by_dialect.items()}
        self._recent = {}
        self.quarantined = sorted(t.title_id for t in pack.titles.values() if t.quarantined)

    def _popularity(self, store, replay_start):
        """Distinct viewers who started each content_slug in the last N days of history. Only plays the
        platform logged count; nothing generated."""
        since = replay_start - timedelta(days=self.cfg['history']['popularity_days'])
        viewers = defaultdict(set)
        for evs in store.by_user.values():
            for e in evs:
                if e.event == 'play_started' and since <= e.est_at < replay_start:
                    t = self.pack.titles.get(e.props.get('title_id'))
                    if t:
                        viewers[t.slug].add(e.user_id)
        return {slug: len(v) for slug, v in viewers.items()}

    def withdraw(self, title_id, at):
        self.withdrawn[title_id] = at

    def available(self, title, at, margin=True):
        """Shown only if it is available now and, by default, stays available for the page's lifetime, so a
        page never carries a title that expires while it is live."""
        if title.quarantined:
            return False
        w = self.withdrawn.get(title.title_id)
        if w is not None and w <= at:
            return False
        if not title.available(at):
            return False
        if margin and title.available_until is not None and title.available_until <= at + self.margin:
            return False
        return True

    def pick_variant(self, slug, language, at):
        """One title_id per content: the language variant matching the app language, else the first available."""
        cands = [self.pack.titles[t] for t in self.pack.slug_titles.get(slug, [])]
        cands = [t for t in cands if self.available(t, at)]
        if not cands:
            return None
        cands.sort(key=lambda t: (t.language != language, t.language != 'hin', t.title_id))
        return cands[0]

    def ranked_slugs(self, dialect, at, predicate=None):
        ranked = self._ranked.get(dialect, [])
        if predicate is None:
            return ranked
        return [s for s in ranked if predicate(self.pack.titles[self.pack.slug_titles[s][0]])]

    def new_releases(self, dialect, at):
        days = self.cfg['goals']['new_release_days']
        key = (dialect, at.date())
        if key not in self._recent:
            lo, hi = (at - timedelta(days=days)).date(), at.date()
            self._recent[key] = self.ranked_slugs(
                dialect, at, lambda t: lo <= parse(t.release_date + 'T00:00:00Z').date() <= hi)
        return [s for s in self._recent[key] if self.pick_variant(s, 'hin', at) is not None]

    def has_content(self, dialect):
        return bool(dialect) and dialect in self.by_dialect
