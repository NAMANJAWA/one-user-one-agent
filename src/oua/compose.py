"""Builds the proposed intervention: a home page in the dialect the app will request, plus at most one follow-up
message. Titles are chosen deterministically from the catalogue and logged plays. No model picks titles."""
import hashlib
from datetime import timedelta

from .timeutil import at_ist, ist_day, ist_hour, in_quiet_hours

ROW_PLANS = {
    'G01': ['popular_in_dialect', 'new_releases', 'genre', 'because_you_watched'],
    'G03': ['popular_in_dialect', 'new_releases', 'genre', 'because_you_watched'],
    'G02': ['next_episode', 'popular_in_dialect', 'because_you_watched', 'new_releases', 'genre'],
    'G04': ['popular_in_dialect', 'new_releases', 'because_you_watched', 'genre'],
    'G05': ['popular_in_dialect', 'new_releases', 'because_you_watched', 'genre'],
    'G06': ['popular_in_dialect', 'new_releases', 'because_you_watched', 'genre'],
    'G07': ['next_episode', 'because_you_watched', 'popular_in_dialect', 'new_releases', 'genre'],
    'G08': ['new_releases', 'because_you_watched', 'popular_in_dialect', 'genre'],
    'G09': ['because_you_watched', 'next_episode', 'popular_in_dialect', 'new_releases', 'genre'],
    'G12': ['because_you_watched', 'next_episode', 'popular_in_dialect', 'new_releases', 'genre'],
}
DEFAULT_PLAN = ['popular_in_dialect', 'new_releases', 'because_you_watched', 'genre']

PUSH_COPY = {
    'en': {
        'resume': ('Continue {name}', '{Epname} of {name} is ready when you are.'),
        'paid': ('{name}', 'Watch {name} with the monthly plan at Rs {price}.'),
        'default': ('{name}', 'Picked for you: {name}. Open the app to watch.'),
        'fallback_title': 'Picked for you',
    },
    'hin': {
        'resume': ('{name} jaari rakhiye', '{name} ka {epname} aapka intezaar kar raha hai.'),
        'paid': ('{name}', '{name} dekhiye, monthly plan Rs {price} mein.'),
        'default': ('{name}', 'Aapke liye chuna gaya: {name}. App kholkar dekhiye.'),
        'fallback_title': 'Aapke liye',
    },
}


class Composer:
    def __init__(self, pack, cfg, content, planner):
        self.pack, self.cfg, self.content, self.planner = pack, cfg, content, planner

    # ------------------------------------------------------------------ home page
    def home_page(self, view, goal, at, minimal=False):
        """Returns (page, why) where page is None when no valid page can be built."""
        dialect = view.value('display_dialect')
        lang = view.value('display_language')
        if not dialect:
            return None, 'display dialect unknown'
        if not self.content.has_content(dialect):
            return None, 'no titles exist in dialect %s' % dialect
        gid = goal['goal_id'] if goal else None
        used = set(self._finished_slugs(view))
        popular = self.content.ranked_slugs(dialect, at)
        titles = lambda slugs: (t for t in (self.content.pick_variant(s, lang, at) for s in slugs) if t)
        top_genres = list(view.genre_affinity)[:2]
        pools = {
            'popular_in_dialect': popular,
            'new_releases': self.content.new_releases(dialect, at),
            'because_you_watched': [s for s in popular if top_genres and set(top_genres) &
                                    set(self.pack.titles[self.pack.slug_titles[s][0]].genres)],
            'next_episode': [p['slug'] for p in sorted(view.progress.values(), key=lambda p: p['last_play'] or at,
                                                        reverse=True)
                             if p['dialect'] == dialect and p['max_ep'] < p['total']
                             and p['type'] in ('show', 'micro_drama')],
        }
        genres = (top_genres + [g for g in self._dialect_genres(popular) if g not in top_genres])
        genres = genres[:self.cfg['home_page'].get('genre_rows', 1)]
        genre_pools = [(g, [s for s in popular if g in self.pack.titles[self.pack.slug_titles[s][0]].genres])
                       for g in genres]
        if gid in ('G04', 'G05', 'G06'):
            prem = lambda s: self.pack.titles[self.pack.slug_titles[s][0]].premium
            pools['popular_in_dialect'] = [s for s in popular if prem(s)] + [s for s in popular if not prem(s)]
        if gid in ('G01', 'G02', 'G03'):
            short = lambda s: self.pack.titles[self.pack.slug_titles[s][0]].content_type == 'micro_drama'
            pools['popular_in_dialect'] = [s for s in popular if short(s)] + [s for s in popular if not short(s)]

        hero = self._hero(view, gid, goal, pools, dialect, lang, at, used)
        if hero:
            used.add(hero.slug)
        rows = []
        plan = ROW_PLANS.get(gid, DEFAULT_PLAN)
        max_rows = 1 if minimal else self.cfg['home_page']['rows']
        # A thin catalogue (d3 has 13 titles) is spread over several rows of >= 3 instead of one long row.
        per_row = max(3, min(self.cfg['home_page']['titles_per_row'], (len(popular) - 1) // max(1, max_rows)))
        slots = []
        for row_type in plan + [r for r in DEFAULT_PLAN if r not in plan]:
            if row_type == 'genre':
                slots.extend(('genre', g, pool) for g, pool in genre_pools)
            else:
                slots.append((row_type, None, pools.get(row_type, [])))
        for row_type, label, pool in slots:
            if len(rows) >= max_rows:
                break
            picked = []
            for t in titles(pool):
                if t.slug in used:
                    continue
                picked.append(t)
                if len(picked) >= per_row:
                    break
            if len(picked) < 3:
                continue  # the platform drops rows under 3 titles; do not publish them
            for t in picked:
                used.add(t.slug)
            row = {'row_type': row_type, 'title_ids': [t.title_id for t in picked]}
            if label:
                row['label'] = label
            rows.append(row)
        if not rows and not hero:
            return None, 'fewer than 3 valid titles for any row in %s' % dialect
        page = {'dialect': dialect, 'hero_title_id': hero.title_id if hero else None, 'rows': rows}
        return page, None

    def _finished_slugs(self, view):
        return [p['slug'] for p in view.progress.values() if p['max_ep'] >= p['total'] and p['type'] == 'film']

    def _dialect_genres(self, popular):
        counts = {}
        for s in popular[:30]:
            for g in self.pack.titles[self.pack.slug_titles[s][0]].genres:
                counts[g] = counts.get(g, 0) + 1
        return sorted(counts, key=lambda g: (-counts[g], g))

    def _hero(self, view, gid, goal, pools, dialect, lang, at, used):
        if gid == 'G07' and goal.get('detail', {}).get('series'):
            t = self.content.pick_variant(goal['detail']['series']['slug'], lang, at)
            if t and t.dialect == dialect:
                return t
        if gid in ('G01', 'G02', 'G03') and view.campaign:
            t = self.pack.titles.get(view.campaign['title_id'])
            if t and t.dialect == dialect:
                v = self.content.pick_variant(t.slug, lang, at)
                if v:
                    return v
        order = {'G04': 'popular_in_dialect', 'G05': 'popular_in_dialect', 'G06': 'popular_in_dialect',
                 'G08': 'new_releases', 'G09': 'because_you_watched', 'G12': 'because_you_watched'}.get(
                     gid, 'popular_in_dialect')
        for pool in (order, 'popular_in_dialect'):
            for s in pools.get(pool, []):
                if s in used:
                    continue
                t = self.content.pick_variant(s, lang, at)
                if t:
                    return t
        return None

    # ------------------------------------------------------------------ message
    def send_time(self, at, viewer_id=''):
        """Follow-up a few hours after the visit, inside the allowed window; never in quiet hours. Follow-ups pushed
        to the next morning are spread over a window (stable per viewer) instead of all firing at 09:00:00."""
        m = self.cfg['messaging']
        t = at + timedelta(hours=m['followup_delay_h'])
        clamped = False
        if in_quiet_hours(t) or ist_hour(t) >= m['latest_send_hour_ist']:
            day = ist_day(t) if ist_hour(t) < m['earliest_send_hour_ist'] else ist_day(t) + timedelta(days=1)
            t, clamped = at_ist(day, m['earliest_send_hour_ist']), True
        elif ist_hour(t) < m['earliest_send_hour_ist']:
            t, clamped = at_ist(ist_day(t), m['earliest_send_hour_ist']), True
        if clamped:
            spread = m.get('morning_spread_min', 120)
            h = int(hashlib.sha256(('spread:%s' % viewer_id).encode()).hexdigest()[:8], 16)
            t += timedelta(minutes=h % max(1, spread))
        return t

    def next_episode_label(self, slug, watched, lang):
        """'Episode 4' / 'Season 2, episode 1' for the episode after the last one watched, or None if none is left."""
        eps = self.pack.episodes.get(slug, [])
        if watched >= len(eps):
            return None
        season, ep = eps[watched][0], eps[watched][1]
        multi = len({e[0] for e in eps}) > 1
        if lang == 'en':
            return ('Season %d, episode %d' % (season, ep)) if multi else 'Episode %d' % ep
        return ('season %d ka episode %d' % (season, ep)) if multi else 'episode %d' % ep

    def message(self, view, goal, page, at, suppressed=()):
        """Returns (message, why_not). Channel order: WhatsApp (approved template, Hindi viewers), then push."""
        if not page or not page.get('hero_title_id'):
            return None, 'no hero title to message about'
        title = self.pack.titles[page['hero_title_id']]
        gid = goal['goal_id']
        lang = view.value('display_language')
        if lang not in PUSH_COPY:
            return None, 'app language %r is unknown; no message rather than a guess at the language' % lang
        send_at = self.send_time(at, view.user_id)
        if view.value('consent_whatsapp') and lang == 'hin' and 'whatsapp' not in suppressed:
            tpl = 'wa_recommend_title_v1'
            return {'type': 'whatsapp', 'send_at': send_at, 'template_id': tpl, 'title_id': title.title_id,
                    'variables': {'title_name': title.name, 'dialect_name': title.dialect}, '_language': lang}, None
        if view.value('consent_push') and 'push' not in suppressed:
            copy = PUSH_COPY[lang]
            price = self.pack.plan_price('P_MONTH')
            series = (goal.get('detail') or {}).get('series') or {}
            epname = None
            # "Continue X / episode N" only when the message really is about the viewer's unfinished series (the
            # hero falls back to a popular title when the series cannot be shown) and that episode exists.
            if gid == 'G07' and series.get('slug') == title.slug:
                epname = self.next_episode_label(title.slug, series.get('next_episode', 1) - 1, lang)
            if epname:
                key = 'resume'
            elif gid in ('G04', 'G05', 'G06') and title.premium:
                key = 'paid'
            else:
                key = 'default'
            head, body = copy[key]
            fmt = dict(name=title.name, epname=epname, Epname=(epname or '')[:1].upper() + (epname or '')[1:],
                       price=price)
            text_title = head.format(**fmt)
            if len(text_title) > 40:
                text_title = copy['fallback_title']
            text_body = body.format(**fmt)
            if len(text_body) > 120:
                text_body = body.format(**dict(fmt, name=title.name[:40]))[:120]
            return {'type': 'push', 'send_at': send_at, 'title_id': title.title_id,
                    'text': {'title': text_title, 'body': text_body},
                    'deeplink': 'ott://title/%s' % title.title_id, '_language': lang,
                    '_resume': {'slug': title.slug, 'episode': epname} if epname else None}, None
        if suppressed:
            return None, 'channels suppressed by what was learned for this viewer: %s' % sorted(suppressed)
        return None, 'no messaging consent (push and WhatsApp)'
