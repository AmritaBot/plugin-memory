"""DMN 空闲窗口调度：7×12 热度图 + 冷段扫描 + 段内二分。

设计要点
- 观测：用户每条消息让对应 (星期几, 2h 槽) 权重 +1，并随时间衰减。
- 分层：fine(单片星期几) / mid(工作日-周末) / coarse(全局)，逐级降级。
- 收缩：fine/mid 向 coarse 按有效样本天数加权，避免稀薄样本失真。
- 规划：环形找连续冷槽 -> 段内用原始时间戳做分钟级二分 -> 扣 buffer 得可用窗口。
- 保底：冷段不足门槛时逐次减半放宽，最后强制取最长冷段。

本模块不依赖 nonebot，纯数据 + 纯函数，可脱离运行时单独验证。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
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

_LOG = logging.getLogger(__name__)

_WD_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def resolve_timezone(name: str | None) -> tuple[tzinfo, str, bool]:
    """把配置里的时区名解析成 tzinfo。

    返回 (tz, 实际生效的名字, 是否回退)。解析失败回退 UTC+08:00，
    会话侧与调度侧共用这一份，避免两边报出不同的钟点。
    """
    raw = (name or "").strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(raw), raw, False
    except Exception:
        return timezone(timedelta(hours=8)), "UTC+08:00", True


def _cell(wd: int, slot: int) -> int:
    return wd * SLOTS_PER_DAY + (slot % SLOTS_PER_DAY)


def slot_of(ts: float, tz: tzinfo) -> tuple[int, int]:
    """把 unix 时间戳换成 (星期几 0=周一, 槽位 0..11)。"""
    dt = datetime.fromtimestamp(ts, tz)
    wd = dt.weekday()
    slot = (dt.hour * 60 + dt.minute) // SLOT_MINUTES
    return wd, slot


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


def free_gaps(
    ts: Sequence[float], start: float, end: float
) -> list[tuple[float, float]]:
    """返回 [start, end) 内的全部无消息区间。"""
    if end <= start:
        return []
    inner = sorted(t for t in ts if start < t < end)
    pts = [start, *inner, end]
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


def longest_free(ts: Sequence[float], start: float, end: float) -> tuple[float, float]:
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


def _runs(
    mask: Sequence[bool], limit: int, circular: bool = True
) -> list[tuple[int, int]]:
    """找连续 True 段，返回 (起点, 长度)，长度不超过 limit。

    circular=True 用于单日 12 槽（跨午夜是本日内的相邻），
    拼接两天的 24 槽必须传 False——否则首尾会被当成相邻，选出一周之外的窗。
    """
    n = len(mask)
    if n == 0:
        return []
    if all(mask):
        return [(0, min(n, limit))] if not circular else [(0, limit)]
    if not any(mask):
        return []
    if not circular:
        out: list[tuple[int, int]] = []
        i = 0
        while i < n:
            if not mask[i]:
                i += 1
                continue
            j = i
            while j < n and mask[j]:
                j += 1
            out.append((i, min(j - i, limit)))
            i = j
        return out
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
    #: 热度图对应的时区名与目标用户，任一变化都要重建，否则星期几/钟点对不上
    tz_name: str = ""
    uid: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fine": [round(v, 6) for v in self.fine],
            "coarse": [round(v, 6) for v in self.coarse],
            "seen_days": self.seen_days[-_MAX_SEEN_DAYS:],
            "recent": self.recent[-_MAX_RECENT:],
            "last_update": self.last_update,
            "tz_name": self.tz_name,
            "uid": self.uid,
        }


class IdlePlanner:
    """基于热度图的空闲窗口规划器。"""

    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg
        tz_name = getattr(cfg, "idle_timezone", "Asia/Shanghai")
        self.tz, self.tz_name, self.tz_fallback = resolve_timezone(tz_name)
        self.uid = str(getattr(cfg, "target_user_id", "") or "")
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

    def load(self, payload: dict[str, Any]) -> None:
        raw = payload.get("idle_planner") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            return
        saved_tz = str(raw.get("tz_name", "") or "")
        saved_uid = str(raw.get("uid", "") or "")
        # 时区或目标用户换了，旧星期几/钟点的计数就不能再用
        if saved_tz and saved_tz != self.tz_name:
            _LOG.info(
                f"[Subconscious] Planner history dropped: tz {saved_tz} -> {self.tz_name}"
            )
            return
        if saved_uid and self.uid and saved_uid != self.uid:
            _LOG.info(
                f"[Subconscious] Planner history dropped: uid {saved_uid} -> {self.uid}"
            )
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
            self.state.tz_name = self.tz_name
            self.state.uid = self.uid
        except (TypeError, ValueError):
            self.state = PlannerState()

    def dump(self) -> dict[str, Any]:
        return {"idle_planner": self.state.to_dict()}

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
        self.state.tz_name = self.tz_name
        self.state.uid = self.uid
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

    def active_days_of(self, weekdays: Sequence[int]) -> int:
        """统计近期活跃天数：超过粗层半衰期 4 倍的老日期不再计入。

        不设上限的话，用户换作息后旧日期会永久撑着 fine/mid 层，
        稀薄但"看起来够量"的样本反而比全局层更不可信。
        """
        horizon = max(1, int(self.tau_coarse * 4))
        cutoff = datetime.now(self.tz) - timedelta(days=horizon)
        cnt = 0
        for key in set(self.state.seen_days):
            try:
                dt = datetime.strptime(key, "%Y-%m-%d")
            except ValueError:
                continue
            if dt.replace(tzinfo=self.tz) < cutoff:
                continue
            if dt.weekday() in weekdays:
                cnt += 1
        return cnt

    def _shrink(self, cells: Sequence[float], n_days: int) -> list[float]:
        """把细层分布按可信度向全局分布收缩。

        两层先各自归一化再混合：细层一天一次采样、全局层跨所有天累加，
        直接相加会让全局层凭绝对计数压过 alpha 想表达的信任度。
        """
        total = n_days + self.shrink_k
        alpha = n_days / total if total > 0 else 0.0
        base = _normalize(self.state.coarse)
        cur = _normalize(cells)
        return [alpha * cur[i] + (1 - alpha) * base[i] for i in range(SLOTS_PER_DAY)]

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

    def _tier_for_neighbor(self, tier: str, wd: int) -> str:
        """跨午夜扫描时，给相邻星期几单独挑一档可用分层。

        直接沿用当天的 tier，会在相邻那天样本不足时拿 0 计数参与判定，
        那些槽位会被误判成冷槽。
        """
        if tier == "fine" and self.active_days_of([wd]) < self.min_weeks_fine:
            return self._tier_for_neighbor("mid", wd)
        if tier == "mid":
            days = [0, 1, 2, 3, 4] if wd <= 4 else [5, 6]
            if self.active_days_of(days) < self.min_weeks_mid:
                return "coarse"
        return tier

    def _cold_runs(self, tier: str, wd: int) -> list[tuple[int, int, int]]:
        """返回 [(环形起点槽, 长度, 归属星期几)]，长度上限一天。"""
        weights, counts, _ = self.tier_table(tier, wd)
        pcts = _normalize(weights)
        mask = [
            pcts[s] <= self.cold_pct_max and counts[s] <= self.cold_abs_max
            for s in range(SLOTS_PER_DAY)
        ]
        # 跨午夜：接上下一星期几的表，否则 22:00-08:00 的安静段会被切断
        wd2 = (wd + 1) % DAYS
        tier2 = self._tier_for_neighbor(tier, wd2)
        w2, c2, _ = self.tier_table(tier2, wd2)
        p2 = _normalize(w2)
        mask2 = [
            p2[s] <= self.cold_pct_max and c2[s] <= self.cold_abs_max
            for s in range(SLOTS_PER_DAY)
        ]
        # 拼接表首尾在真实时间上相隔整整一天，不能按环形再接回去
        ring = mask + mask2
        return [
            (start, length, wd) for start, length in _runs(ring, SLOTS_PER_DAY, False)
        ]

    def _lightest_run(
        self, tier: str, wd: int, length_slots: int, now: datetime
    ) -> tuple[int, int] | None:
        """环形滑动窗口找总权重最低的连续 length_slots 槽段。

        权重相同时取最近一次将要到来的那段，避免全天打平时
        因为起点槽已过而把窗口推到下周。
        """
        weights, _, _ = self.tier_table(tier, wd)
        pcts = _normalize(weights)
        if sum(pcts) <= 1e-9:
            return None
        n = SLOTS_PER_DAY
        ring = pcts + pcts
        best: tuple[int, int] | None = None
        best_key: tuple[float, float] | None = None
        for start in range(n):
            total = sum(ring[start : start + length_slots])
            seg_start, _ = self._seg_bounds(start, length_slots, wd, now)
            delay = max(0.0, (seg_start - now.astimezone(self.tz)).total_seconds())
            key = (total, delay)
            if best_key is None or key < best_key:
                best_key = key
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

    def _seg_bounds(
        self, start_slot: int, length_slots: int, wd: int, now: datetime
    ) -> tuple[datetime, datetime]:
        """冷段对应的绝对时间区间；已过去的按下一个同类星期几算。"""
        day0 = self._next_occurrence(wd, now)
        seg_start = day0 + timedelta(minutes=start_slot * SLOT_MINUTES)
        seg_end = seg_start + timedelta(minutes=length_slots * SLOT_MINUTES)
        now_local = now.astimezone(self.tz)
        if seg_end <= now_local:
            seg_start += timedelta(days=DAYS)
            seg_end += timedelta(days=DAYS)
        return seg_start, seg_end

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
    ) -> Plan | None:
        """把冷段换成具体时间窗：段内找最长空闲，二分校核，扣 buffer。

        段内连一次运行都放不下时返回 None，交给调用方换下一段，
        而不是报出一个实际用不了的时间窗。
        """
        seg_start, seg_end = self._seg_bounds(start_slot, length_slots, wd, now)
        now_local = now.astimezone(self.tz)
        if seg_start < now_local:
            seg_start = now_local + timedelta(minutes=1)
        if seg_end <= seg_start:
            return None

        # 只信原始时间戳：连发消息先合并，再取段内最长无消息区间
        ts = merge_noise(self.state.recent, self.noise_gap)
        s_ts, e_ts = seg_start.timestamp(), seg_end.timestamp()
        f_start, f_end = longest_free(ts, s_ts, e_ts)
        capacity = bisect_free_minutes(ts, s_ts, e_ts)
        gap_minutes = int((f_end - f_start) // 60)
        # 二分给出理论上界，实际可用不得超出真实无消息区间
        free_minutes = min(capacity, gap_minutes)

        need = self.buffer_minutes + self.run_estimate
        # 偏好时刻早于段首就贴段首：加一天会把它推到段外，
        # 再被 latest_start 夹到段尾，整段空闲只用到最后 run_estimate 那点
        target = seg_start.replace(
            hour=self.target_hour, minute=0, second=0, microsecond=0
        )
        if target < seg_start:
            target = seg_start
        # 起点尽量靠近偏好时刻，但必须落在空闲段内且留够运行空间
        latest_start = max(f_start, f_end - need * 60)
        start_ts = min(max(f_start, target.timestamp()), latest_start)
        start_ts = max(start_ts, f_start, now.timestamp() + 60)

        note = ""
        if f_end - start_ts < need * 60:
            # 段内剩余空间放不下一次运行，退回段首再算一次
            start_ts = f_start
            note = "insufficient_free_space"
        if f_end - start_ts < 60:
            return None

        start = datetime.fromtimestamp(start_ts, self.tz)
        # 尾部 buffer 不纳入回报窗口，否则 end 会把安全余量也算成可用时间
        usable_end = datetime.fromtimestamp(f_end, self.tz) - timedelta(
            minutes=self.buffer_minutes
        )
        end = min(start + timedelta(minutes=free_minutes), usable_end)
        length_minutes = int((end - start).total_seconds() // 60)
        if length_minutes < 1:
            return None
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

    def _fallback(self, now: datetime, penalty: int, note: str) -> Plan:
        local = now.astimezone(self.tz)
        target = local.replace(hour=self.target_hour, minute=0, second=0, microsecond=0)
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

    def has_history(self) -> bool:
        """热度图是否有可用样本。"""
        return sum(self.state.coarse) > 1e-6

    def fallback_plan(
        self, penalty: int = 1, now: datetime | None = None, note: str = "no_history"
    ) -> Plan:
        """直接给出保底窗口，不做冷段扫描。"""
        now = now or datetime.now(timezone.utc)
        return self._fallback(now.astimezone(self.tz), penalty, note)

    def plan(self, penalty: int = 1, now: datetime | None = None) -> Plan:
        result = self._plan(penalty, now)
        self._last_plan = result
        return result

    def _want_slots(self, penalty: int) -> int:
        """按惩罚值算本次要求的冷段槽数，指数增长但先封顶再取整。"""
        exp = max(0, penalty - 1)
        try:
            want = self.base_slots * (self.multiplier**exp)
        except OverflowError:
            # 惩罚值离谱时直接给上限，不要让一次计算失败把调度整条打断
            return self.max_slots
        if want != want:  # NaN 保护
            return self.base_slots
        return max(1, min(round(want), self.max_slots))

    def _pick_run(
        self, runs: Sequence[tuple[int, int, int]], now: datetime
    ) -> tuple[int, int, int] | None:
        """在满足门槛的冷段里挑一段：优先最近的、尚未来临的、较长的。"""
        best: tuple[int, int, int] | None = None
        best_key: tuple[int, float, int] | None = None
        for start_slot, length_slots, owner in runs:
            seg_start, _ = self._seg_bounds(start_slot, length_slots, owner, now)
            delay = max(0.0, (seg_start - now.astimezone(self.tz)).total_seconds())
            key = (0 if delay > 0 else 1, delay, -length_slots)
            if best_key is None or key < best_key:
                best_key = key
                best = (start_slot, length_slots, owner)
        return best

    def _plan(self, penalty: int = 1, now: datetime | None = None) -> Plan:
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(self.tz)
        wd = local.weekday()

        if sum(self.state.coarse) <= 1e-6:
            return self._fallback(local, penalty, "no_history")

        want = self._want_slots(penalty)

        tiers = self._tiers_for(wd)
        for step in range(self.max_relax + 1):
            need_slots = max(1, want >> step)
            for tier in tiers:
                runs = [r for r in self._cold_runs(tier, wd) if r[1] >= need_slots]
                picked = self._pick_run(runs, now)
                if picked is None:
                    continue
                plan = self._refine(
                    picked[0],
                    picked[1],
                    picked[2],
                    now,
                    tier,
                    penalty,
                    step,
                    forced=False,
                )
                if plan is not None:
                    return plan

        # 全部放宽仍不足：强制取最长冷段
        for tier in tiers:
            runs = sorted(self._cold_runs(tier, wd), key=lambda r: -r[1])
            picked = self._pick_run(runs, now)
            if picked is None:
                continue
            plan = self._refine(
                picked[0],
                picked[1],
                picked[2],
                now,
                tier,
                penalty,
                self.max_relax,
                forced=True,
            )
            if plan is not None:
                return plan

        # 完全没有可用冷段（用户几乎全天活跃）：退到热度最低的连续段
        for tier in tiers:
            floor_run = self._lightest_run(tier, wd, 1, now)
            if floor_run is None:
                continue
            plan = self._refine(
                floor_run[0],
                floor_run[1],
                wd,
                now,
                tier,
                penalty,
                self.max_relax,
                forced=True,
            )
            if plan is not None:
                return plan
        return self._fallback(local, penalty, "no_cold_segment")


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
