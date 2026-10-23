"""Rebuilds what the post-replay commands (judge, impact, explain, report) need from a finished run."""
import json
import os

from .events import EventStore
from .goals import GoalRegistry
from .memory import Memory
from .pack import Pack, read_jsonl


class RunContext:
    pass


def load_ctx(pack_dir, run_dir, cfg):
    ctx = RunContext()
    ctx.cfg = cfg
    ctx.pack = Pack(pack_dir)
    ctx.run_dir = run_dir
    store = EventStore()
    store.load_history(ctx.pack.paths['events'])
    observed = {e.event for evs in store.by_user.values() for e in evs}
    ctx.registry = GoalRegistry(ctx.pack.goals, observed)
    for c in read_jsonl(ctx.pack.paths['control']):
        if c['type'] == 'goal_rule_corrected':
            ctx.registry.apply_correction(c)
    ctx.memory = Memory(os.path.join(run_dir, 'agent_state.sqlite'))
    with open(os.path.join(run_dir, 'run_manifest.json'), encoding='utf-8') as f:
        ctx.manifest = json.load(f)
    return ctx
