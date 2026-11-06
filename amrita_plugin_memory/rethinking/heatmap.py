"""热度图渲染：把 IdlePlanner 的 7×12 作息热度画成 PNG。

纯渲染模块，只依赖 Pillow 与 schedule 的数据结构，不碰 nonebot，
方便脱离运行时用临时脚本出图核对。

版式是竖版的：纵轴是时间（一天 12 个两小时槽），横轴是星期几，
和常见的作息表读法一致——从上往下看一天，从左往右看一周。

整张图按 SCALE 倍超采样绘制：小字号中文在 1x 下笔画会糊在一起，
把画布和字号一起放大再出图，边缘才立得住。

格内数字是落盘时点的累计计数，颜色按半衰期折算到当前时刻并按线性映射取色：
平方根压缩会把 1 条和 6 条拉成同一个亮度，冷热就分不出来了。
规划窗口按槽取整高亮：框线必须压在槽边界上，精确起止交给下面板里的文字。

字体按候选路径查找，找不到中文字体时自动退回英文标签（PIL 自带位图字体
没有 CJK 字形，硬画会出一片豆腐块）。
"""

from __future__ import annotations

import io
import os
from datetime import datetime, timedelta
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .schedule import DAYS, SLOT_MINUTES, SLOTS_PER_DAY, IdlePlanner, Plan

#: 超采样倍率：1x 下 12px 中文基本看不清，整体放大后出图
SCALE = 2
_LOGICAL_W = 760
_LOGICAL_H = 814

WIDTH = _LOGICAL_W * SCALE
HEIGHT = _LOGICAL_H * SCALE
PAD = 36 * SCALE
GRID_LEFT = 84 * SCALE
GRID_TOP = 132 * SCALE
CELL_W = (_LOGICAL_W - 84 - 36) / DAYS * SCALE
CELL_H = 44 * SCALE
GRID_BOTTOM = GRID_TOP + CELL_H * SLOTS_PER_DAY

BG = (15, 20, 26)
PANEL = (22, 29, 38)
BORDER = (38, 51, 65)
TEXT = (214, 224, 236)
MUTED = (138, 160, 184)
ACCENT = (141, 180, 216)
HOT_MARK = (242, 209, 139)
NOW_MARK = (224, 122, 95)

#: 冷 -> 热的取色节点，最后一档偏暖，让热点一眼可见
_RAMP = (
    (0.0, (26, 34, 44)),
    (0.3, (32, 62, 90)),
    (0.55, (58, 110, 150)),
    (0.8, (141, 180, 216)),
    (1.0, (232, 176, 106)),
)

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)

_WD_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_WD_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _px(v: float) -> float:
    """逻辑像素换算成实际画布像素。"""
    return v * SCALE


@lru_cache(maxsize=1)
def _font_path() -> str | None:
    """按环境变量、候选路径顺序找第一个可用的字体文件。"""
    env = os.environ.get("AMRITA_MEMORY_HEATMAP_FONT", "").strip()
    if env and os.path.exists(env):
        return env
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


@lru_cache(maxsize=16)
def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    path = _font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _t(zh: str, en: str) -> str:
    """有中文字体时用中文，否则退回英文，避免整张图变成豆腐块。"""
    return zh if _font_path() else en


def _ramp(v: float) -> tuple[int, int, int]:
    v = max(0.0, min(1.0, v))
    for i in range(len(_RAMP) - 1):
        lo, c_lo = _RAMP[i]
        hi, c_hi = _RAMP[i + 1]
        if v <= hi:
            span = hi - lo
            t = 0.0 if span <= 0 else (v - lo) / span
            return (
                int(c_lo[0] + (c_hi[0] - c_lo[0]) * t),
                int(c_lo[1] + (c_hi[1] - c_lo[1]) * t),
                int(c_lo[2] + (c_hi[2] - c_lo[2]) * t),
            )
    return _RAMP[-1][1]


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return int(right - left), int(bottom - top)


def _center(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    text: str,
    font,
    fill: tuple[int, int, int],
) -> None:
    w, h = _text_size(draw, text, font)
    draw.text((cx - w / 2, cy - h / 2), text, font=font, fill=fill)


def _right(
    draw: ImageDraw.ImageDraw,
    x: float,
    cy: float,
    text: str,
    font,
    fill: tuple[int, int, int],
) -> None:
    w, h = _text_size(draw, text, font)
    draw.text((x - w, cy - h / 2), text, font=font, fill=fill)


def _humanize(seconds: float) -> str:
    if seconds <= 0:
        return _t("已到时间", "due now")
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    if _font_path():
        if hours and minutes:
            return f"{hours} 小时 {minutes} 分后"
        if hours:
            return f"{hours} 小时后"
        return f"{minutes} 分后"
    if hours:
        return f"in {hours}h{minutes:02d}m"
    return f"in {minutes}m"


def _y_of(moment: datetime) -> float:
    """把一天里的时刻换算成网格纵坐标（0:00 在顶，24:00 在底）。"""
    minutes = moment.hour * 60 + moment.minute + moment.second / 60
    return GRID_TOP + minutes / SLOT_MINUTES * CELL_H


def _cell_box(wd: int, slot: int) -> tuple[float, float, float, float]:
    """一个槽的绘制矩形（左, 上, 右, 下），与格子本身的内缩一致。"""
    x0 = GRID_LEFT + wd * CELL_W + _px(3)
    x1 = GRID_LEFT + (wd + 1) * CELL_W - _px(3)
    y0 = GRID_TOP + slot * CELL_H + _px(3)
    y1 = GRID_TOP + (slot + 1) * CELL_H - _px(3)
    return x0, y0, x1, y1


def _window_slots(plan: Plan, tz) -> list[tuple[int, int, int]]:
    """把计划窗口换成 (星期几, 起始槽, 结束槽)，按槽取整。

    高亮是给"哪几个槽被占用"看的，框线必须落在槽边界上；
    精确到分钟的起止写在图下面板里，两者不要混着表达。
    """
    start = plan.start.astimezone(tz)
    end = plan.end.astimezone(tz)
    out: list[tuple[int, int, int]] = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(DAYS + 1):
        if day > end:
            break
        seg_start = max(start, day)
        seg_end = min(end, day + timedelta(days=1))
        if seg_end > seg_start:
            first = (seg_start.hour * 60 + seg_start.minute) // SLOT_MINUTES
            if seg_end == day + timedelta(days=1):
                last = SLOTS_PER_DAY - 1
            else:
                minutes = seg_end.hour * 60 + seg_end.minute
                last = (minutes + SLOT_MINUTES - 1) // SLOT_MINUTES - 1
            first = min(max(first, 0), SLOTS_PER_DAY - 1)
            last = min(max(last, first), SLOTS_PER_DAY - 1)
            out.append((day.weekday(), first, last))
        day += timedelta(days=1)
    return out


def render_heatmap_png(
    planner: IdlePlanner,
    *,
    next_run: datetime | None = None,
    plan: Plan | None = None,
    penalty: int = 0,
    now: datetime | None = None,
) -> bytes:
    """渲染热度图，返回 PNG 字节。

    next_run 是调度器里待执行任务的触发时间（可能为空：没有待执行任务），
    plan 是上一次规划的窗口，用于在网格上叠加预计占用的时间区间。
    """
    state = planner.state
    tz = planner.tz
    now_local = (now or datetime.now(tz)).astimezone(tz)
    if next_run is not None and next_run.tzinfo is None:
        # 调度器给的是带时区的时刻；裸时间按本地时区解释，别猜成 UTC
        next_run = next_run.replace(tzinfo=tz)

    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    f_title = _font(_px(26))
    f_sub = _font(_px(14))
    f_cell = _font(_px(13))
    f_axis = _font(_px(13))
    f_label = _font(_px(13))
    f_value = _font(_px(22))
    f_small = _font(_px(13))

    draw.text(
        (PAD, _px(26)),
        _t("潜意识热度图", "Subconscious heatmap"),
        font=f_title,
        fill=TEXT,
    )
    _right(draw, WIDTH - PAD, _px(40), planner.tz_name, f_sub, MUTED)

    observed = round(sum(state.coarse))
    tier_text = plan.tier if plan is not None else "-"
    subtitle = _t(
        f"观测 {observed} 条 · 活跃 {len(state.seen_days)} 天 · "
        f"惩罚 #{penalty} · 分层 {tier_text}",
        f"observations {observed} · active {len(state.seen_days)}d · "
        f"penalty #{penalty} · tier {tier_text}",
    )
    draw.text((PAD, _px(72)), subtitle, font=f_sub, fill=MUTED)

    dt_days = 0.0
    if state.last_update:
        dt_days = max(0.0, (now_local.timestamp() - state.last_update) / 86400)
    decay = 2 ** (-dt_days / planner.tau_fine) if dt_days else 1.0
    weights = [max(0.0, v) * decay for v in state.fine]
    peak = max(weights) if weights else 0.0

    for wd in range(DAYS):
        cx = GRID_LEFT + (wd + 0.5) * CELL_W
        _center(draw, cx, GRID_TOP - _px(18), _t(_WD_ZH[wd], _WD_EN[wd]), f_axis, MUTED)

    for slot in range(SLOTS_PER_DAY):
        cy = GRID_TOP + (slot + 0.5) * CELL_H
        _right(draw, GRID_LEFT - _px(12), cy, f"{slot * 2:02d}", f_axis, MUTED)

    now_slot = (now_local.hour * 60 + now_local.minute) // SLOT_MINUTES
    now_wd = now_local.weekday()

    for slot in range(SLOTS_PER_DAY):
        for wd in range(DAYS):
            idx = wd * SLOTS_PER_DAY + slot
            weight = weights[idx]
            ratio = weight / peak if peak > 1e-9 else 0.0
            x0, y0, x1, y1 = _cell_box(wd, slot)
            draw.rounded_rectangle((x0, y0, x1, y1), radius=_px(6), fill=_ramp(ratio))
            raw = state.fine[idx]
            if raw >= 0.5:
                fill = (18, 24, 32) if ratio > 0.62 else TEXT
                _center(draw, (x0 + x1) / 2, (y0 + y1) / 2, f"{raw:.0f}", f_cell, fill)
            if wd == now_wd and slot == now_slot:
                # 红点要整个落在格子里：贴到圆角上会看成跑到格子外面去了
                r = _px(3.5)
                cx = x1 - _px(9)
                cy = y1 - _px(9)
                draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=NOW_MARK)

    if plan is not None:
        # 半透明覆盖层单独画：直接填色会盖掉格内数字
        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        for wd, first, last in _window_slots(plan, tz):
            # 逐格填色：整块铺一层会把槽之间的缝也染上，看着像没对齐格子
            for slot in range(first, last + 1):
                od.rounded_rectangle(
                    _cell_box(wd, slot),
                    radius=_px(6),
                    fill=(242, 209, 139, 44),
                )
            x0, y0, _, _ = _cell_box(wd, first)
            _, _, x1, y1 = _cell_box(wd, last)
            od.rounded_rectangle(
                (x0, y0, x1, y1),
                radius=_px(6),
                outline=(242, 209, 139, 235),
                width=int(_px(2)),
            )
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

    panel_top = GRID_BOTTOM + _px(22)
    draw.rounded_rectangle(
        (PAD, panel_top, WIDTH - PAD, panel_top + _px(84)),
        radius=_px(10),
        fill=PANEL,
        outline=BORDER,
    )
    draw.text(
        (PAD + _px(18), panel_top + _px(12)),
        _t("下次 DMN 运行", "Next DMN run"),
        font=f_label,
        fill=MUTED,
    )

    if next_run is not None:
        local = next_run.astimezone(tz)
        stamp = (
            f"{local.strftime('%Y-%m-%d')} "
            f"{_t(_WD_ZH[local.weekday()], _WD_EN[local.weekday()])} "
            f"{local.strftime('%H:%M')}"
        )
        draw.text((PAD + _px(18), panel_top + _px(32)), stamp, font=f_value, fill=TEXT)
        w, _ = _text_size(draw, stamp, f_value)
        draw.text(
            (PAD + _px(30) + w, panel_top + _px(40)),
            _humanize((local - now_local).total_seconds()),
            font=f_sub,
            fill=ACCENT,
        )
    else:
        draw.text(
            (PAD + _px(18), panel_top + _px(32)),
            _t("尚未排期", "not scheduled"),
            font=f_value,
            fill=HOT_MARK,
        )
        draw.text(
            (PAD + _px(148), panel_top + _px(40)),
            _t("等用户下一条消息触发规划", "waiting for next user message"),
            font=f_sub,
            fill=MUTED,
        )

    if plan is not None:
        local_end = plan.end.astimezone(tz)
        detail = _t(
            f"窗口 {plan.start.strftime('%H:%M')}–{local_end.strftime('%H:%M')} · "
            f"{plan.length_minutes} 分钟 · tier={plan.tier} · "
            f"relax={plan.relaxed_steps} · forced={plan.forced}",
            f"window {plan.start.strftime('%H:%M')}-{local_end.strftime('%H:%M')} · "
            f"{plan.length_minutes}min · tier={plan.tier} · "
            f"relax={plan.relaxed_steps} · forced={plan.forced}",
        )
        if plan.note:
            detail += f" · {plan.note}"
        draw.text(
            (PAD + _px(18), panel_top + _px(62)), detail, font=f_small, fill=MUTED
        )

    bar_y = panel_top + _px(104)
    bar_w = _px(220)
    bar_h = _px(10)
    for i in range(int(bar_w)):
        draw.line(
            ((PAD + i, bar_y), (PAD + i, bar_y + bar_h)),
            fill=_ramp(i / max(1, bar_w - 1)),
        )
    draw.text((PAD, bar_y - _px(4)), _t("冷", "cold"), font=f_small, fill=MUTED)
    draw.text(
        (PAD + bar_w + _px(10), bar_y - _px(4)),
        _t("热", "hot"),
        font=f_small,
        fill=MUTED,
    )
    _right(
        draw,
        WIDTH - PAD,
        bar_y + bar_h / 2,
        _t(
            "纵轴 2 小时一槽 · 红点=当前时段 · 颜色按半衰期折算后的权重",
            "2h per row · dot = now · color = decayed weight",
        ),
        f_small,
        MUTED,
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
