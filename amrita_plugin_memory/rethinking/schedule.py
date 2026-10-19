"""DMN 空闲窗口调度：7×12 热度图 + 冷段扫描 + 段内二分。

设计要点
- 观测：用户每条消息让对应 (星期几, 2h 槽) 权重 +1，并随时间衰减。
- 分层：fine(单片星期几) / mid(工作日-周末) / coarse(全局)，逐级降级。
- 收缩：fine/mid 向 coarse 按有效样本天数加权，避免稀薄样本失真。
- 规划：环形找连续冷槽 -> 段内用原始时间戳做分钟级二分 -> 扣 buffer 得可用窗口。
- 保底：冷段不足门槛时逐次减半放宽，最后强制取最长冷段。

本模块不依赖 nonebot，纯数据 + 纯函数，便于独立单测。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

SLOT_MINUTES = 120
SLOTS_PER_DAY = 12
DAYS = 7
CELLS = DAYS * SLOTS_PER_DAY
MINUTES_PER_DAY = SLOT_MINUTES * SLOTS_PER_DAY

#: 最近消息时间戳缓冲上限（分钟级细化用，不能无限涨）
_MAX_RECENT = 500
#: 活跃日期登记上限
_MAX_SEEN_DAYS = 240

_WD_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _cell(wd: int, slot: int) -> int:
    return wd * SLOTS_PER_DAY + (slot % SLOTS_PER_DAY)


def slot_of(ts: float, tz: ZoneInfo) -> tuple[int, int]:
    """把 unix 时间戳换成 (星期几 0=周一, 槽位 0..11)。"""
    dt = datetime.fromtimestamp(ts, tz)
    wd = dt.weekday()
    slot = (dt.hour * 60 + dt.minute) // SLOT_MINUTES
    return wd, slot


# ---------------------------------------------------------------- 纯函数工具


def merge_noise(ts: Sequence[float], gap_seconds: float) -> list[float]:
    """把一波连发的消息压成单点：间距小于 gap 的后来者直接并掉。"""
    out: list[float] = []
    prev: float | None = None
    for t in sorted(ts):
        if prev is not None and t - prev < gap_seconds:
            continue
        out.append(t)
        prev = t
    return out


def free_gaps(ts: Sequence[float], start: float, end: float) -> list[tuple[float, float]]:
    """返回 [start, end) 内的全部无消息区间。"""
    if end <= start:
        return []
    inner = sorted(t for t in ts if start < t < end)
    pts = [start, *inner, end]
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


def longest_free(
    ts: Sequence[float], start: float, end: float
) -> tuple[float, float]:
    """[start, end) 内最长的无消息区间。"""
    gaps = free_gaps(ts, start, end)
    if not gaps:
        return start, end
    return max(gaps, key=lambda g: g[1] - g[0])


def feasible_minutes(
    ts: Sequence[float], start: float, end: float, minutes: int
) -> bool:
    """是否存在一段连续 >= minutes 的空闲。（随 minutes 单调递减）"""
    if minutes <= 0:
        return end > start
    need = minutes * 60
    return any(b - a >= need for a, b in free_gaps(ts, start, end))


def bisect_free_minutes(
    ts: Sequence[float], start: float, end: float, hi: int | None = None
) -> int:
    """二分求窗口内最长连续空闲（分钟）。判定函数单调，收敛到分钟精度。"""
    if end <= start:
        return 0
    top = hi if hi is not None else int((end - start) // 60)
    lo = 0
    while lo < top:
        mid = (lo + top + 1) // 2
        if feasible_minutes(ts, start, end, mid):
            lo = mid
        else:
            top = mid - 1
    return lo


def _normalize(values: Sequence[float]) -> list[float]:
    total = sum(values)
    if total <= 1e-9:
        return [0.0] * len(values)
    return [v / total for v in values]


def _runs(mask: Sequence[bool], limit: int) -> list[tuple[int, int]]:
    """环形找连续 True 段，返回 (起点, 长度)，长度不超过 limit。"""
    n = len(mask)
    if n == 0:
        return []
    if all(mask):
        return [(0, limit)]
    if not any(mask):
        return []
    start = mask.index(False)
    seq = list(mask[start:]) + list(mask[:start])
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(seq):
        if not seq[i]:
            i += 1
            continue
        j = i
        while j < len(seq) and seq[j]:
            j += 1
        out.append(((start + i) % n, min(j - i, limit)))
        i = j
    return out


# ---------------------------------------------------------------- 规划结果


@dataclass
class Plan:
    """一次调度规划的结果。"""

    start: datetime
    end: datetime
    length_minutes: int
    tier: str = "coarse"
    relaxed_steps: int = 0
    forced: bool = False
    penalty: int = 0
    note: str = ""

    @property
    def delay_seconds(self) -> int:
        return max(1, int((self.start - datetime.now(timezone.utc)).total_seconds()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "length_minutes": self.length_minutes,
            "tier": self.tier,
            "relaxed_steps": self.relaxed_steps,
            "forced": self.forced,
            "penalty": self.penalty,
            "note": self.note,
        }


@dataclass
class PlannerState:
    """可持久化的热度图状态。"""

    fine: list[float] = field(default_factory=lambda: [0.0] * CELLS)
    coarse: list[float] = field(default_factory=lambda: [0.0] * SLOTS_PER_DAY)
    seen_days: list[str] = field(default_factory=list)
    recent: list[float] = field(default_factory=list)
    last_update: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fine": [round(v, 6) for v in self.fine],
            "coarse": [round(v, 6) for v in self.coarse],
            "seen_days": self.seen_days[-_MAX_SEEN_DAYS:],
            "recent": self.recent[-_MAX_RECENT:],
            "last_update": self.last_update,
        }


# ---------------------------------------------------------------- 规划器


class IdlePlanner:
    """基于热度图的空闲窗口规划器。"""

    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg
        tz_name = getattr(cfg, "idle_timezone", "Asia/Shanghai")
        try:
            self.tz = ZoneInfo(tz_name)
        except Exception:
            self.tz = timezone(timedelta(hours=8))
        self.state = PlannerState()

        self.noise_gap = getattr(cfg, "idle_noise_gap_minutes", 15) * 60
        self.tau_fine = max(1.0, getattr(cfg, "idle_fine_tau_days", 28))
        self.tau_coarse = max(1.0, getattr(cfg, "idle_coarse_tau_days", 14))
        self.shrink_k = max(0.0, getattr(cfg, "idle_shrink_k", 4))
        self.cold_pct_max = getattr(cfg, "idle_cold_pct_max", 0.05)
        self.cold_abs_max = getattr(cfg, "idle_cold_abs_max", 1.0)
        self.min_weeks_fine = getattr(cfg, "idle_min_sample_days_fine", 6)
        self.min_weeks_mid = getattr(cfg, "idle_min_sample_days_mid", 3)
        self.target_hour = getattr(cfg, "idle_target_hour", 3)
        self.base_slots = getattr(cfg, "idle_base_slots", 3)
        self.max_slots = getattr(cfg, "idle_max_slots", 6)
        self.multiplier = getattr(cfg, "idle_penalty_multiplier", 1.5)
        self.max_relax = getattr(cfg, "idle_max_relax_steps", 3)
        self.buffer_minutes = getattr(cfg, "idle_buffer_minutes", 15)
        self.run_estimate = getattr(cfg, "idle_run_estimate_minutes", 10)
        self.fallback_minutes = getattr(cfg, "idle_fallback_minutes", 120)
        self._last_plan: Plan | None = None

    # ------------------------------------------------------------ 持久化

    def load(self, payload: dict[str, Any]) -> None:
        raw = payload.get("idle_planner") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            return
        try:
            fine = [float(v) for v in raw.get("fine", [])]
            coarse = [float(v) for v in raw.get("coarse", [])]
            if len(fine) == CELLS:
                self.state.fine = fine
            if len(coarse) == SLOTS_PER_DAY:
                self.state.coarse = coarse
            seen = raw.get("seen_days", [])
            if isinstance(seen, list):
                self.state.seen_days = [str(d) for d in seen][-_MAX_SEEN_DAYS:]
            recent = raw.get("recent", [])
            if isinstance(recent, list):
                self.state.recent = [float(v) for v in recent][-_MAX_RECENT:]
            self.state.last_update = float(raw.get("last_update", 0.0))
        except (TypeError, ValueError):
            self.state = PlannerState()

    def dump(self) -> dict[str, Any]:
        return {"idle_planner": self.state.to_dict()}

    # ------------------------------------------------------------ 观测

    def _decay(self, now_ts: float) -> None:
        if self.state.last_update <= 0:
            self.state.last_update = now_ts
            return
        dt_days = (now_ts - self.state.last_update) / 86400
        if dt_days <= 1e-6:
            return
        f_fine = 2 ** (-dt_days / self.tau_fine)
        f_coarse = 2 ** (-dt_days / self.tau_coarse)
        self.state.fine = [v * f_fine for v in self.state.fine]
        self.state.coarse = [v * f_coarse for v in self.state.coarse]
        self.state.last_update = now_ts

    def observe(self, ts: float | None = None) -> None:
        """记录一条用户消息，加深该时间窗口的热点。"""
        now_ts = time.time() if ts is None else float(ts)
        self._decay(now_ts)
        wd, slot = slot_of(now_ts, self.tz)
        self.state.fine[_cell(wd, slot)] += 1.0
        self.state.coarse[slot] += 1.0
        day_key = datetime.fromtimestamp(now_ts, self.tz).strftime("%Y-%m-%d")
        if day_key not in self.state.seen_days:
            self.state.seen_days.append(day_key)
            if len(self.state.seen_days) > _MAX_SEEN_DAYS:
                self.state.seen_days = self.state.seen_days[-_MAX_SEEN_DAYS:]
        self.state.recent.append(now_ts)
        if len(self.state.recent) > _MAX_RECENT:
            self.state.recent = self.state.recent[-_MAX_RECENT:]

    def last_plan(self) -> Plan | None:
        return self._last_plan

    def last_plan_dict(self) -> dict[str, Any] | None:
        plan = self._last_plan
        if plan is None:
            return None
        local_end = plan.end.astimezone(self.tz)
        return {
            "window_start": plan.start.strftime("%Y-%m-%d %H:%M"),
            "window_end": local_end.strftime("%Y-%m-%d %H:%M"),
            "planned_minutes": plan.length_minutes,
            "tier": plan.tier,
            "relaxed_steps": plan.relaxed_steps,
            "forced": plan.forced,
            "note": plan.note,
        }

    def note_window_taken(self, start_ts: float) -> None:
        """窗口实际被占用后，把该时段计入一次观测（避免反复选同一段）。"""
        self.observe(start_ts + 60)

    # ------------------------------------------------------------ 分层分值

    def active_days_of(self, weekdays: Sequence[int]) -> int:
        cnt = 0
        keys = set(self.state.seen_days)
        for key in keys:
            try:
                dt = datetime.strptime(key, "%Y-%m-%d")
            except ValueError:
                continue
            if dt.weekday() in weekdays:
                cnt += 1
        return cnt

    def _shrink(self, cells: Sequence[float], n_days: int) -> list[float]:
        alpha = n_days / (n_days + self.shrink_k) if (n_days + self.shrink_k) > 0 else 0.0
        base = self.state.coarse
        return [alpha * cells[i] + (1 - alpha) * base[i] for i in range(SLOTS_PER_DAY)]

    def tier_table(self, tier: str, wd: int) -> tuple[list[float], list[float], int]:
        """返回 (槽位权重, 槽位原始计数, 有效样本天数)。"""
        if tier == "fine":
            cells = self.state.fine[wd * SLOTS_PER_DAY : (wd + 1) * SLOTS_PER_DAY]
            n_days = self.active_days_of([wd])
            return self._shrink(cells, n_days), list(cells), n_days
        if tier == "mid":
            days = [0, 1, 2, 3, 4] if wd <= 4 else [5, 6]
            cells = [
                sum(self.state.fine[_cell(d, s)] for d in days) / len(days)
                for s in range(SLOTS_PER_DAY)
            ]
            n_days = self.active_days_of(days)
            return self._shrink(cells, n_days), cells, n_days
        cells = list(self.state.coarse)
        return cells, cells, self.active_days_of(list(range(DAYS)))

    def _tiers_for(self, wd: int) -> list[str]:
        tiers: list[str] = []
        if self.active_days_of([wd]) >= self.min_weeks_fine:
            tiers.append("fine")
        days = [0, 1, 2, 3, 4] if wd <= 4 else [5, 6]
        if self.active_days_of(days) >= self.min_weeks_mid:
            tiers.append("mid")
        tiers.append("coarse")
        return tiers

    # ------------------------------------------------------------ 冷段

    def _cold_runs(
        self, tier: str, wd: int
    ) -> list[tuple[int, int, int]]:
        """返回 [(环形起点槽, 长度, 归属星期几)]，长度上限一天。"""
        weights, counts, _ = self.tier_table(tier, wd)
        pcts = _normalize(weights)
        mask = [
            pcts[s] <= self.cold_pct_max and counts[s] <= self.cold_abs_max
            for s in range(SLOTS_PER_DAY)
        ]
        # 跨午夜：接上下一星期几的表，否则 22:00-08:00 的安静段会被切断
        w2, c2, _ = self.tier_table(tier, (wd + 1) % DAYS)
        p2 = _normalize(w2)
        mask2 = [
            p2[s] <= self.cold_pct_max and c2[s] <= self.cold_abs_max
            for s in range(SLOTS_PER_DAY)
        ]
        ring = mask + mask2
        return [(start, length, wd) for start, length in _runs(ring, SLOTS_PER_DAY)]

    def _lightest_run(
        self, tier: str, wd: int, length_slots: int
    ) -> tuple[int, int] | None:
        """环形滑动窗口找总权重最低的连续 length_slots 槽段。"""
        weights, _, _ = self.tier_table(tier, wd)
        pcts = _normalize(weights)
        if sum(pcts) <= 1e-9:
            return None
        n = SLOTS_PER_DAY
        ring = pcts + pcts
        best: tuple[int, int] | None = None
        best_sum = float("inf")
        for start in range(n):
            total = sum(ring[start : start + length_slots])
            if total < best_sum - 1e-12:
                best_sum = total
                best = (start, length_slots)
        return best

    def _next_occurrence(self, wd: int, now: datetime) -> datetime:
        """本周/下周该星期几的 00:00（不早于 now 所在日期）。"""
        today = now.astimezone(self.tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        delta = (wd - today.weekday()) % DAYS
        cand = today + timedelta(days=delta)
        if cand + timedelta(days=1) <= now.astimezone(self.tz):
            cand += timedelta(days=DAYS)
        return cand

    def _refine(
        self,
        start_slot: int,
        length_slots: int,
        wd: int,
        now: datetime,
        tier: str,
        penalty: int,
        relaxed: int,
        forced: bool,
    ) -> Plan:
        """把冷段换成具体时间窗：段内找最长空闲，二分校核，扣 buffer。"""
        day0 = self._next_occurrence(wd, now)
        seg_start = day0 + timedelta(minutes=start_slot * SLOT_MINUTES)
        seg_end = seg_start + timedelta(minutes=length_slots * SLOT_MINUTES)
        now_local = now.astimezone(self.tz)
        if seg_end <= now_local:
            seg_start += timedelta(days=DAYS)
            seg_end += timedelta(days=DAYS)
        if seg_start < now_local:
            seg_start = now_local + timedelta(minutes=1)

        # 只信原始时间戳：连发消息先合并，再取段内最长无消息区间
        ts = merge_noise(self.state.recent, self.noise_gap)
        s_ts, e_ts = seg_start.timestamp(), seg_end.timestamp()
        f_start, f_end = longest_free(ts, s_ts, e_ts)
        # 二分只为确认这段空闲的上界（段内可能有更细的分布）
        capacity = bisect_free_minutes(ts, s_ts, e_ts)
        gap_minutes = int((f_end - f_start) // 60)
        # 二分给出理论上界，实际可用不得超出真实无消息区间
        free_minutes = min(capacity, gap_minutes)

        need = self.buffer_minutes + self.run_estimate
        target = seg_start.replace(
            hour=self.target_hour, minute=0, second=0, microsecond=0
        )
        if target < seg_start:
            target += timedelta(days=1)
        # 起点尽量靠近偏好时刻，但必须落在空闲段内且留够运行空间
        latest_start = max(f_start, f_end - need * 60)
        start_ts = min(max(f_start, target.timestamp()), latest_start)
        start_ts = max(start_ts, f_start, now.timestamp() + 60)

        if f_end - start_ts < need * 60:
            # 段内剩余空间放不下一次运行，标记但不放弃（保底语义）
            length_minutes = max(1, int((f_end - start_ts) // 60))
            note = "insufficient_free_space"
            start_ts = f_start
            length_minutes = max(1, int((f_end - f_start) // 60))
        else:
            # 结束时间严格受空闲段与冷段双重约束
            length_minutes = int((f_end - start_ts) // 60)
            if free_minutes < length_minutes:  # 保守取真实空闲长度
                length_minutes = free_minutes
            note = ""

        start = datetime.fromtimestamp(start_ts, self.tz)
        end = min(start + timedelta(minutes=length_minutes),
                  datetime.fromtimestamp(f_end, self.tz))
        return Plan(
            start=start,
            end=end,
            length_minutes=length_minutes,
            tier=tier,
            relaxed_steps=relaxed,
            forced=forced,
            penalty=penalty,
            note=note,
        )

    # ------------------------------------------------------------ 主流程

    def _fallback(self, now: datetime, penalty: int, note: str) -> Plan:
        local = now.astimezone(self.tz)
        target = local.replace(
            hour=self.target_hour, minute=0, second=0, microsecond=0
        )
        if target <= local + timedelta(minutes=1):
            target += timedelta(days=1)
        return Plan(
            start=target,
            end=target + timedelta(minutes=self.fallback_minutes),
            length_minutes=self.fallback_minutes,
            tier="fallback",
            forced=True,
            penalty=penalty,
            note=note,
        )

    def plan(self, penalty: int = 1, now: datetime | None = None) -> Plan:
        result = self._plan(penalty, now)
        self._last_plan = result
        return result

    def _plan(self, penalty: int = 1, now: datetime | None = None) -> Plan:
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(self.tz)
        wd = local.weekday()

        if sum(self.state.coarse) <= 1e-6:
            return self._fallback(local, penalty, "no_history")

        want = self.base_slots * (self.multiplier ** max(0, penalty - 1))
        want = max(1, min(round(want), self.max_slots))

        tiers = self._tiers_for(wd)
        for step in range(self.max_relax + 1):
            need_slots = max(1, want >> step)
            for tier in tiers:
                runs = [
                    r for r in self._cold_runs(tier, wd) if r[1] >= need_slots
                ]
                if not runs:
                    continue
                start_slot, length_slots, owner = max(
                    runs, key=lambda r: (r[1], -r[0])
                )
                return self._refine(
                    start_slot,
                    length_slots,
                    owner,
                    now,
                    tier,
                    penalty,
                    step,
                    forced=False,
                )

        # 全部放宽仍不足：强制取最长冷段
        best: tuple[int, int, int, str] | None = None
        for tier in tiers:
            for run in self._cold_runs(tier, wd):
                if best is None or run[1] > best[1]:
                    best = (run[0], run[1], run[2], tier)
        if best is not None:
            return self._refine(
                best[0], best[1], best[2], now, best[3], penalty,
                self.max_relax, forced=True,
            )
        # 完全没有冷槽（用户几乎全天活跃）：退到热度最低的连续段
        for tier in tiers:
            floor_run = self._lightest_run(tier, wd, 1)
            if floor_run is not None:
                return self._refine(
                    floor_run[0], floor_run[1], wd, now, tier, penalty,
                    self.max_relax, forced=True,
                )
        return self._fallback(local, penalty, "no_cold_segment")
        return self._refine(
            best[0],
            best[1],
            best[2],
            now,
            best[3],
            penalty,
            self.max_relax,
            forced=True,
        )


def describe(plan: Plan) -> str:
    """日志用的一行描述。"""
    local = plan.start
    wd = _WD_NAMES[local.weekday()]
    return (
        f"{wd} {local.strftime('%H:%M')}-{plan.end.strftime('%H:%M')} "
        f"({plan.length_minutes}min, tier={plan.tier}, "
        f"relax={plan.relaxed_steps}, forced={plan.forced}"
        f"{', ' + plan.note if plan.note else ''})"
    )
