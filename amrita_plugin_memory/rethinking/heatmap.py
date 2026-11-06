"""热度图渲染：把 IdlePlanner 的 7×12 作息热度画成 PNG。

纯渲染模块，只依赖 Pillow 与 schedule 的数据结构，不碰 nonebot，
方便脱离运行时用临时脚本出图核对。格内数字是落盘时点的累计计数，
颜色再按半衰期折算到当前时刻——数字看"来过多少条"，颜色看"现在有多热"。

字体按候选路径查找，找不到中文字体时自动退回英文标签（PIL 自带位图字体
没有 CJK 字形，硬画会出一片豆腐块）。
"""

from __future__ import annotations

import io
import os
from datetime import datetime, timedelta
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .schedule import DAYS, SLOT_MINUTES, SLOTS_PER_DAY, IdlePlanner, Plan, slot_of

WIDTH = 1000
HEIGHT = 648
PAD = 36
GRID_LEFT = 96
GRID_TOP = 152
CELL_W = (WIDTH - GRID_LEFT - PAD) / SLOTS_PER_DAY
CELL_H = 46
GRID_BOTTOM = GRID_TOP + CELL_H * DAYS

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


def _plan_cells(plan: Plan, tz) -> set[tuple[int, int]]:
    """把计划窗口铺到 (星期几, 槽位) 上，用于高亮。"""
    cells: set[tuple[int, int]] = set()
    cur = plan.start.astimezone(tz)
    end = plan.end.astimezone(tz)
    for _ in range(SLOTS_PER_DAY * DAYS):
        if cur >= end:
            break
        cells.add(slot_of(cur.timestamp(), tz))
        cur += timedelta(minutes=SLOT_MINUTES)
    if not cells:
        cells.add(slot_of(plan.start.timestamp(), tz))
    return cells


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
    plan 是上一次规划的窗口，用于在网格上高亮预计占用的槽位。
    """
    state = planner.state
    tz = planner.tz
    now_local = (now or datetime.now(tz)).astimezone(tz)
    if next_run is not None and next_run.tzinfo is None:
        # 调度器给的是带时区的时刻；裸时间按本地时区解释，别猜成 UTC
        next_run = next_run.replace(tzinfo=tz)

    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    f_title = _font(26)
    f_sub = _font(14)
    f_cell = _font(13)
    f_axis = _font(13)
    f_label = _font(13)
    f_value = _font(22)
    f_small = _font(12)

    draw.text(
        (PAD, 26), _t("潜意识热度图", "Subconscious heatmap"), font=f_title, fill=TEXT
    )
    _right(draw, WIDTH - PAD, 40, planner.tz_name, f_sub, MUTED)

    observed = round(sum(state.coarse))
    tier_text = plan.tier if plan is not None else "-"
    subtitle = _t(
        f"观测 {observed} 条 · 活跃 {len(state.seen_days)} 天 · "
        f"惩罚 #{penalty} · 分层 {tier_text}",
        f"observations {observed} · active {len(state.seen_days)}d · "
        f"penalty #{penalty} · tier {tier_text}",
    )
    draw.text((PAD, 72), subtitle, font=f_sub, fill=MUTED)

    dt_days = 0.0
    if state.last_update:
        dt_days = max(0.0, (now_local.timestamp() - state.last_update) / 86400)
    decay = 2 ** (-dt_days / planner.tau_fine) if dt_days else 1.0
    weights = [max(0.0, v) * decay for v in state.fine]
    peak = max(weights) if weights else 0.0

    for slot in range(SLOTS_PER_DAY):
        cx = GRID_LEFT + (slot + 0.5) * CELL_W
        _center(draw, cx, GRID_TOP - 16, f"{slot * 2:02d}", f_axis, MUTED)

    highlight = _plan_cells(plan, tz) if plan is not None else set()
    if next_run is not None:
        highlight.add(slot_of(next_run.astimezone(tz).timestamp(), tz))
    now_cell = slot_of(now_local.timestamp(), tz)

    for wd in range(DAYS):
        row_cy = GRID_TOP + wd * CELL_H + CELL_H / 2
        _right(draw, GRID_LEFT - 12, row_cy, _t(_WD_ZH[wd], _WD_EN[wd]), f_axis, MUTED)
        for slot in range(SLOTS_PER_DAY):
            idx = wd * SLOTS_PER_DAY + slot
            weight = weights[idx]
            ratio = (weight / peak) ** 0.5 if peak > 1e-9 else 0.0
            x0 = GRID_LEFT + slot * CELL_W + 3
            y0 = GRID_TOP + wd * CELL_H + 3
            x1 = GRID_LEFT + (slot + 1) * CELL_W - 3
            y1 = GRID_TOP + (wd + 1) * CELL_H - 3
            draw.rounded_rectangle((x0, y0, x1, y1), radius=6, fill=_ramp(ratio))
            raw = state.fine[idx]
            if raw >= 0.5:
                fill = (18, 24, 32) if ratio > 0.62 else TEXT
                _center(draw, (x0 + x1) / 2, (y0 + y1) / 2, f"{raw:.0f}", f_cell, fill)
            if (wd, slot) in highlight:
                draw.rounded_rectangle(
                    (x0, y0, x1, y1), radius=6, outline=HOT_MARK, width=2
                )
            if (wd, slot) == now_cell:
                cx = (x0 + x1) / 2
                cy = y1 - 7
                draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=NOW_MARK)

    panel_top = GRID_BOTTOM + 20
    draw.rounded_rectangle(
        (PAD, panel_top, WIDTH - PAD, panel_top + 84),
        radius=10,
        fill=PANEL,
        outline=BORDER,
    )
    draw.text(
        (PAD + 18, panel_top + 12),
        _t("下次 DMN 运行", "Next DMN run"),
        font=f_label,
        fill=MUTED,
    )

    if next_run is not None:
        local = next_run.astimezone(tz)
        stamp = f"{local.strftime('%Y-%m-%d')} {_t(_WD_ZH[local.weekday()], _WD_EN[local.weekday()])} {local.strftime('%H:%M')}"
        draw.text((PAD + 18, panel_top + 32), stamp, font=f_value, fill=TEXT)
        w, _ = _text_size(draw, stamp, f_value)
        draw.text(
            (PAD + 30 + w, panel_top + 40),
            _humanize((local - now_local).total_seconds()),
            font=f_sub,
            fill=ACCENT,
        )
    else:
        draw.text(
            (PAD + 18, panel_top + 32),
            _t("尚未排期", "not scheduled"),
            font=f_value,
            fill=HOT_MARK,
        )
        draw.text(
            (PAD + 18 + 130, panel_top + 40),
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
        draw.text((PAD + 18, panel_top + 62), detail, font=f_small, fill=MUTED)

    bar_y = panel_top + 104
    bar_w = 240
    for i in range(bar_w):
        draw.line(
            ((PAD + i, bar_y), (PAD + i, bar_y + 8)),
            fill=_ramp(i / max(1, bar_w - 1)),
        )
    draw.text((PAD, bar_y - 4), _t("冷", "cold"), font=f_small, fill=MUTED)
    draw.text((PAD + bar_w + 10, bar_y - 4), _t("热", "hot"), font=f_small, fill=MUTED)
    _right(
        draw,
        WIDTH - PAD,
        bar_y + 4,
        _t(
            "每格 2 小时 · 颜色按半衰期衰减后的权重",
            "2h per cell · color = decayed weight",
        ),
        f_small,
        MUTED,
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
