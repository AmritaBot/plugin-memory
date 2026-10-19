"""DMN 空闲窗口调度的纯函数与规划器单测。

只测 schedule.py，不加载 nonebot，便于快速回归。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

from amrita_plugin_memory.rethinking.schedule import (
    CELLS,
    SLOTS_PER_DAY,
    IdlePlanner,
    _runs,
    bisect_free_minutes,
    feasible_minutes,
    free_gaps,
    longest_free,
    merge_noise,
    slot_of,
)

TZ = ZoneInfo("Asia/Shanghai")


def make_cfg(**over) -> NS:
    base = dict(
        idle_timezone="Asia/Shanghai",
        idle_noise_gap_minutes=15,
        idle_fine_tau_days=28.0,
        idle_coarse_tau_days=14.0,
        idle_shrink_k=4.0,
        idle_cold_pct_max=0.05,
        idle_cold_abs_max=1.0,
        idle_min_sample_days_fine=6,
        idle_min_sample_days_mid=3,
        idle_target_hour=3,
        idle_base_slots=3,
        idle_max_slots=6,
        idle_penalty_multiplier=1.5,
        idle_max_relax_steps=3,
        idle_buffer_minutes=15,
        idle_run_estimate_minutes=10,
        idle_fallback_minutes=120,
    )
    base.update(over)
    return NS(**base)


def seed_daytime(planner: IdlePlanner, days: int = 14) -> None:
    """灌入白天活跃、夜间安静的历史。"""
    base = datetime(2026, 10, 5, tzinfo=TZ)
    for d in range(days):
        for h in range(9, 22, 2):
            for mi in (0, 40):
                planner.observe((base + timedelta(days=d, hours=h, minutes=mi)).timestamp())


def test_merge_noise_collapses_bursts():
    assert merge_noise([0, 60, 600, 1000], 900) == [0, 1000]


def test_free_gaps_and_longest():
    assert free_gaps([100, 200], 0, 300) == [(0, 100), (100, 200), (200, 300)]
    assert longest_free([100, 200], 0, 300) == (0, 100)
    assert free_gaps([], 0, 0) == []


def test_feasible_is_monotonic():
    ts = [3600 * 2]
    for minutes in range(0, 240, 10):
        if feasible_minutes(ts, 0, 3600 * 4, minutes):
            assert feasible_minutes(ts, 0, 3600 * 4, max(0, minutes - 10))


def test_bisect_matches_bruteforce():
    ts = [3600 * 2]
    got = bisect_free_minutes(ts, 0, 3600 * 4)
    brute = max(m for m in range(0, 241) if feasible_minutes(ts, 0, 3600 * 4, m))
    assert got == brute == 120


def test_ring_runs_wrap_around():
    assert _runs([True] * 24, 12) == [(0, 12)]
    mask = [True, True] + [False] * 20 + [True, True]
    assert dict(_runs(mask, 24))[22] == 4


def test_slot_of_two_hour_grid():
    ts = datetime(2026, 10, 19, 23, 30, tzinfo=TZ).timestamp()
    wd, slot = slot_of(ts, TZ)
    assert wd == 0 and slot == 11


def test_cold_start_falls_back_to_target_hour():
    planner = IdlePlanner(make_cfg())
    plan = planner.plan(1, datetime(2026, 10, 19, 9, 0, tzinfo=TZ))
    assert plan.tier == "fallback"
    assert plan.forced
    assert plan.start.hour == 3


def test_plan_lands_in_night_and_respects_capacity():
    planner = IdlePlanner(make_cfg())
    seed_daytime(planner)
    now = datetime(2026, 10, 19, 9, 0, tzinfo=TZ)
    plan = planner.plan(1, now)
    assert not plan.forced
    assert plan.start >= now
    assert plan.end > plan.start
    assert plan.end.hour <= 9


def test_penalty_raises_requirement_but_stays_in_window():
    planner = IdlePlanner(make_cfg())
    seed_daytime(planner)
    now = datetime(2026, 10, 19, 9, 0, tzinfo=TZ)
    plans = [planner.plan(p, now) for p in (1, 3, 8)]
    assert all(not p.forced for p in plans)
    assert all(p.start.hour == 3 for p in plans)


def test_no_deadlock_when_fully_active():
    """用户几乎全天活跃时也必须给出窗口，不能返回空。"""
    planner = IdlePlanner(make_cfg())
    base = datetime(2026, 10, 5, tzinfo=TZ)
    for d in range(21):
        for s in range(SLOTS_PER_DAY):
            planner.observe((base + timedelta(days=d, hours=s * 2)).timestamp())
    plan = planner.plan(6, datetime(2026, 10, 19, 9, 0, tzinfo=TZ))
    assert plan.start is not None
    assert plan.forced


def test_state_roundtrip():
    planner = IdlePlanner(make_cfg())
    seed_daytime(planner, days=5)
    dumped = planner.dump()
    assert len(dumped["idle_planner"]["fine"]) == CELLS
    other = IdlePlanner(make_cfg())
    other.load(dumped)
    assert other.state.seen_days == planner.state.seen_days
    assert abs(sum(other.state.coarse) - sum(planner.state.coarse)) < 1e-2


def test_decay_reduces_history():
    planner = IdlePlanner(make_cfg())
    seed_daytime(planner, days=7)
    before = sum(planner.state.coarse)
    planner.observe(datetime(2026, 11, 30, tzinfo=TZ).timestamp())
    assert sum(planner.state.coarse) < before


def test_sparse_history_still_plans():
    planner = IdlePlanner(make_cfg())
    planner.observe(datetime(2026, 10, 12, 15, 0, tzinfo=TZ).timestamp())
    plan = planner.plan(1, datetime(2026, 10, 19, 9, 0, tzinfo=TZ))
    assert plan.start is not None
    assert plan.length_minutes >= 1
