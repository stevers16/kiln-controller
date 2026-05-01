"""History screen - five plot tabs sourced from GET /history.

Phase 7. Chart library: matplotlib embedded via
`kivy_garden.matplotlib.FigureCanvasKivyAgg`.

Data source
-----------
The Pico /history endpoint returns columnar JSON:
    {"fields": [name1, name2, ...],
     "rows":   [[v1, v2, ...], [v1, v2, ...], ...],
     "run":    "20260408_1730",
     "row_count": 1234}

Row values come back type-converted by the Pico handler: integers, floats,
or None. Two exceptions relevant here:
  - `ts` stays as a string "YYYY-MM-DD HH:MM:SS" (or fallback "+NNNs" if
    the RTC hadn't been set when logging began).
  - `stage` stays as a string ("drying", "equalizing", etc.).

We unpack the columnar payload into per-field lists once on load, then feed
those arrays directly to matplotlib. No per-row dict construction, per the
spec's "Implementation notes" section.

Tab design reflects the actual CSV columns from lib/logger.py DATA_COLUMNS
(which is slightly narrower than the spec suggests - we do NOT log target
temps, fan RPM, rail currents, or LoRa RSSI, so those overlays are omitted).

    Thermal       : temp_lumber (solid) + temp_intake (dashed)
                    heater_on overlay (red band when heater engaged)
    Humidity      : rh_lumber (solid) + rh_intake (dashed)
                    vent_open overlay (blue band when either vent >0)
    Moisture      : mc_ch1 (solid) + mc_ch2 (dashed)
    Stage         : stage-index step chart (encoded from string)
    Diagnostics   : exhaust_pct + circ_pct (left Y axis)
                    vent_intake + vent_exhaust (right Y axis)

Time range selector: 1h / 6h / 24h / 72h / Full run. Applied client-side
against the full fetched dataset (so range changes do not refetch). "Full
run" passes through to matplotlib unmodified.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("module://kivy_garden.matplotlib.backend_kivyagg")

from kivy_garden.matplotlib.backend_kivyagg import FigureCanvasKivyAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from kivy.clock import Clock  # noqa: E402
from kivy.graphics import Color, Rectangle  # noqa: E402
from kivy.metrics import dp  # noqa: E402
from kivy.uix.boxlayout import BoxLayout  # noqa: E402
from kivy.uix.button import Button  # noqa: E402
from kivy.uix.dropdown import DropDown  # noqa: E402
from kivy.uix.label import Label  # noqa: E402
from kivy.uix.screenmanager import Screen  # noqa: E402
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem  # noqa: E402

from kilnapp import theme  # noqa: E402
from kilnapp.api.autodetect import DetectResult, MODE_OFFLINE  # noqa: E402
from kilnapp.api.client import call_async  # noqa: E402
from kilnapp.connection import ConnectionManager  # noqa: E402
from kilnapp.format import format_run_label  # noqa: E402
from kilnapp.platform_helpers import IS_ANDROID  # noqa: E402


# Matplotlib renders text in literal points (1 pt = 1/72"). On a high-DPI
# phone the default 8pt tick/legend text is physically tiny; scale up so
# axis labels and legends are legible. Desktop stays at the original sizes.
_MPL_FS_SCALE = 2.4 if IS_ANDROID else 1.0
_MPL_TICK_FS = 8 * _MPL_FS_SCALE
_MPL_LABEL_FS = 11 * _MPL_FS_SCALE
_MPL_LEGEND_FS = 8 * _MPL_FS_SCALE
_MPL_TEXT_FS = 11 * _MPL_FS_SCALE
if IS_ANDROID:
    matplotlib.rcParams["axes.labelsize"] = _MPL_LABEL_FS
    matplotlib.rcParams["axes.titlesize"] = _MPL_LABEL_FS


# ---- Time range options ---------------------------------------------------

RANGE_OPTIONS = [
    ("1h", 1),
    ("6h", 6),
    ("24h", 24),
    ("72h", 72),
    ("Full", None),
]
DEFAULT_RANGE_HOURS: Optional[int] = 24


# Auto-refresh interval for active runs (the graph data keeps updating).
ACTIVE_RUN_REFRESH_S = 30

# matplotlib colours chosen for contrast on a dark panel
CLR_LUMBER = "#3fa8ff"      # blue (primary sensor)
CLR_INTAKE = "#ff9c3a"      # orange (secondary)
CLR_MC1 = "#6ee86e"
CLR_MC2 = "#e86ed4"
CLR_HEATER_BAND = "#c64545"
CLR_VENT_BAND = "#3a7ec8"
CLR_EXHAUST = "#ffc24a"
CLR_CIRC = "#9b7ee2"
CLR_VENT_I = "#45c6a8"
CLR_VENT_E = "#e07474"

# Dark panel palette for matplotlib
MPL_BG = "#26282f"
MPL_FG = "#e4e4e9"
MPL_GRID = "#3a3d45"


# ---- Timestamp parsing ----------------------------------------------------


def _parse_ts(ts: Any) -> Optional[datetime.datetime]:
    """Parse the CSV timestamp string into a datetime.

    Accepts the two formats lib/logger.py emits:
      - "YYYY-MM-DD HH:MM:SS"  (RTC is set)
      - "+NNNNs"               (RTC not set; elapsed seconds since boot)

    For the +NNNNs form we fabricate a datetime rooted at 1970-01-01 plus
    the elapsed seconds. Real wall-clock time doesn't matter - we only need
    a monotonic axis - but matplotlib can't render string x values.
    """
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=float(ts))
    s = str(ts).strip()
    if not s:
        return None
    if s.startswith("+") and s.endswith("s"):
        try:
            secs = int(s[1:-1])
            return datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=secs)
        except ValueError:
            return None
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


# ---- Columnar unpacking ---------------------------------------------------


def _unpack_columnar(payload: Dict[str, Any]) -> Dict[str, List[Any]]:
    """Convert {fields, rows} into {field_name: [v0, v1, ...]}.

    Missing fields yield an empty list so plot code can unconditionally
    call .get() without KeyError handling.
    """
    fields = payload.get("fields") or []
    rows = payload.get("rows") or []
    cols: Dict[str, List[Any]] = {f: [None] * len(rows) for f in fields}
    for ri, row in enumerate(rows):
        for ci, fname in enumerate(fields):
            if ci < len(row):
                cols[fname][ri] = row[ci]
    # Field-name aliases. The Pico CSVs use the short forms (mc_ch1,
    # exhaust_pct, ...); the Pi4 daemon's telemetry table uses the long
    # forms straight off the LoRa wire (mc_channel_1, exhaust_fan_pct,
    # ...). Synthesize the missing alias from whichever shape arrived so
    # the plot code below can read the canonical (short) names.
    _alias = {
        "mc_ch1": "mc_channel_1",
        "mc_ch2": "mc_channel_2",
        "exhaust_pct": "exhaust_fan_pct",
        "circ_pct": "circ_fan_pct",
    }
    for short, long in _alias.items():
        if short not in cols and long in cols:
            cols[short] = cols[long]
        elif long not in cols and short in cols:
            cols[long] = cols[short]
    # Pi4 telemetry stores a single `vent_open` bool because the kiln
    # moves both servos as a pair. The History plot expects per-servo
    # columns (vent_intake / vent_exhaust). When the per-servo columns
    # are missing, project vent_open into both at 0/100 so the humidity
    # tab's vent shading and the diagnostics tab's vent traces still
    # render.
    if "vent_open" in cols and "vent_intake" not in cols:
        cols["vent_intake"] = [
            (None if v is None else (100 if v else 0)) for v in cols["vent_open"]
        ]
    if "vent_open" in cols and "vent_exhaust" not in cols:
        cols["vent_exhaust"] = [
            (None if v is None else (100 if v else 0)) for v in cols["vent_open"]
        ]
    return cols


def _filter_range(
    times: List[datetime.datetime],
    cols: Dict[str, List[Any]],
    hours: Optional[int],
) -> Tuple[List[datetime.datetime], Dict[str, List[Any]]]:
    """Keep only samples within the last `hours` from the latest timestamp.

    `hours=None` means "full run" - return everything. If the latest
    timestamp is None (all rows had unparseable ts) we return the full set;
    no sensible cut-off exists.
    """
    if hours is None or not times:
        return times, cols
    latest = max((t for t in times if t is not None), default=None)
    if latest is None:
        return times, cols
    cutoff = latest - datetime.timedelta(hours=hours)
    keep_idx = [i for i, t in enumerate(times) if t is not None and t >= cutoff]
    if not keep_idx:
        return [], {k: [] for k in cols}
    filt_times = [times[i] for i in keep_idx]
    filt_cols = {k: [v[i] for i in keep_idx] for k, v in cols.items()}
    return filt_times, filt_cols


# ---- Stage encoding -------------------------------------------------------


def _encode_stages(stage_col: List[Any]) -> Tuple[List[Optional[int]], List[str]]:
    """Map stage strings to stable integer indices, preserving first-seen
    order so the chart increases monotonically as stages progress.

    Returns (index_series, label_list). `None` values in the input survive
    as `None` in the output.
    """
    labels: List[str] = []
    lookup: Dict[str, int] = {}
    out: List[Optional[int]] = []
    for v in stage_col:
        if v is None or v == "":
            out.append(None)
            continue
        s = str(v)
        if s not in lookup:
            lookup[s] = len(labels)
            labels.append(s)
        out.append(lookup[s])
    return out, labels


# ---- Matplotlib helpers ---------------------------------------------------


def _style_axes(ax) -> None:
    ax.set_facecolor(MPL_BG)
    ax.tick_params(colors=MPL_FG, labelsize=_MPL_TICK_FS)
    for spine in ax.spines.values():
        spine.set_color(MPL_GRID)
    ax.grid(True, color=MPL_GRID, linewidth=0.5, alpha=0.6)
    ax.yaxis.label.set_color(MPL_FG)
    ax.xaxis.label.set_color(MPL_FG)
    ax.title.set_color(MPL_FG)


def _new_figure() -> Figure:
    fig = Figure(facecolor=MPL_BG, tight_layout=True)
    return fig


def _xs_numeric(times: List[Optional[datetime.datetime]]) -> List[float]:
    """Convert datetimes to float seconds since first valid sample.

    matplotlib date support works, but the ambiguous "+NNNs" fallback
    timestamps land all plots in 1970; using relative seconds on the x
    axis keeps them intelligible whether or not the RTC was set.

    Fallback: if every element is None (unparseable / missing ts column)
    we return row indices so the plot still renders something meaningful
    rather than crashing.
    """
    if not times:
        return []
    valid = [t for t in times if t is not None]
    if not valid:
        return [float(i) for i in range(len(times))]
    t0 = min(valid)
    return [(t - t0).total_seconds() if t is not None else float("nan") for t in times]


def _fmt_xaxis(ax, xs: List[float]) -> None:
    """Label the x axis in hours if the run is long, minutes otherwise."""
    if not xs:
        ax.set_xlabel("time", color=MPL_FG)
        return
    # Ignore NaNs (inserted for rows with unparseable timestamps) when
    # deciding the axis scale; using xs[-1] - xs[0] raw gives NaN span.
    finite = [v for v in xs if v == v]  # NaN != NaN
    span = (finite[-1] - finite[0]) if len(finite) >= 2 else 0
    if span >= 3600:
        # hours
        ax.set_xlabel("elapsed (h)", color=MPL_FG)
        # Convert xs seconds to hours by scaling tick labels
        ticks = ax.get_xticks()
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{t / 3600:.1f}" for t in ticks])
    elif span >= 60:
        ax.set_xlabel("elapsed (min)", color=MPL_FG)
        ticks = ax.get_xticks()
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{t / 60:.0f}" for t in ticks])
    else:
        ax.set_xlabel("elapsed (s)", color=MPL_FG)


# ---- A single plot tab ----------------------------------------------------


class _PlotTab(TabbedPanelItem):
    """One matplotlib figure embedded in a Kivy tabbed panel item."""

    def __init__(self, title: str, **kwargs):
        super().__init__(text=title, **kwargs)
        self.figure = _new_figure()
        self.canvas_widget = FigureCanvasKivyAgg(self.figure)
        self.content = self.canvas_widget

    def clear(self) -> None:
        self.figure.clear()
        self.canvas_widget.draw_idle()

    def render_empty(self, msg: str) -> None:
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        _style_axes(ax)
        ax.text(
            0.5, 0.5, msg,
            color=MPL_FG, ha="center", va="center",
            transform=ax.transAxes, fontsize=_MPL_TEXT_FS,
        )
        ax.set_xticks([])
        ax.set_yticks([])
        self.canvas_widget.draw_idle()


# ---- Plot rendering functions --------------------------------------------


def _render_thermal(
    tab: _PlotTab,
    xs: List[float],
    cols: Dict[str, List[Any]],
) -> None:
    fig = tab.figure
    fig.clear()
    ax = fig.add_subplot(111)
    _style_axes(ax)

    temp_l = cols.get("temp_lumber") or []
    temp_i = cols.get("temp_intake") or []
    heater = cols.get("heater_on") or []

    any_line = False
    if any(v is not None for v in temp_l):
        ax.plot(xs, temp_l, color=CLR_LUMBER, linewidth=1.4, label="Lumber")
        any_line = True
    if any(v is not None for v in temp_i):
        ax.plot(xs, temp_i, color=CLR_INTAKE, linewidth=1.2,
                linestyle="--", label="Intake")
        any_line = True

    # Heater overlay: shaded band across the bottom 8% when heater_on==1
    if any(v for v in heater):
        y0, y1 = ax.get_ylim() if any_line else (0, 1)
        band_bot = y0
        band_top = y0 + (y1 - y0) * 0.05
        ax.fill_between(
            xs, band_bot, band_top,
            where=[bool(v) for v in heater],
            color=CLR_HEATER_BAND, alpha=0.8, step="post",
            label="Heater on",
        )

    if not any_line:
        tab.render_empty("No temperature data for this range")
        return
    ax.set_ylabel("temperature (C)", color=MPL_FG)
    ax.legend(facecolor=MPL_BG, edgecolor=MPL_GRID, labelcolor=MPL_FG, fontsize=_MPL_LEGEND_FS)
    _fmt_xaxis(ax, xs)
    tab.canvas_widget.draw_idle()


def _render_humidity(
    tab: _PlotTab,
    xs: List[float],
    cols: Dict[str, List[Any]],
) -> None:
    fig = tab.figure
    fig.clear()
    ax = fig.add_subplot(111)
    _style_axes(ax)

    rh_l = cols.get("rh_lumber") or []
    rh_i = cols.get("rh_intake") or []
    vent_i = cols.get("vent_intake") or []
    vent_e = cols.get("vent_exhaust") or []

    any_line = False
    if any(v is not None for v in rh_l):
        ax.plot(xs, rh_l, color=CLR_LUMBER, linewidth=1.4, label="Lumber")
        any_line = True
    if any(v is not None for v in rh_i):
        ax.plot(xs, rh_i, color=CLR_INTAKE, linewidth=1.2,
                linestyle="--", label="Intake")
        any_line = True

    # Vent overlay: band at bottom when either servo > 0
    vent_open = [
        1 if (vent_i and vent_i[k] not in (None, 0)) or
             (vent_e and vent_e[k] not in (None, 0)) else 0
        for k in range(len(xs))
    ]
    if any(vent_open):
        y0, y1 = ax.get_ylim() if any_line else (0, 100)
        band_bot = y0
        band_top = y0 + (y1 - y0) * 0.05
        ax.fill_between(
            xs, band_bot, band_top,
            where=[bool(v) for v in vent_open],
            color=CLR_VENT_BAND, alpha=0.7, step="post",
            label="Vents open",
        )

    if not any_line:
        tab.render_empty("No humidity data for this range")
        return
    ax.set_ylabel("relative humidity (%)", color=MPL_FG)
    ax.legend(facecolor=MPL_BG, edgecolor=MPL_GRID, labelcolor=MPL_FG, fontsize=_MPL_LEGEND_FS)
    _fmt_xaxis(ax, xs)
    tab.canvas_widget.draw_idle()


def _render_moisture(
    tab: _PlotTab,
    xs: List[float],
    cols: Dict[str, List[Any]],
) -> None:
    fig = tab.figure
    fig.clear()
    ax = fig.add_subplot(111)
    _style_axes(ax)

    mc1 = cols.get("mc_ch1") or []
    mc2 = cols.get("mc_ch2") or []

    any_line = False
    if any(v is not None for v in mc1):
        ax.plot(xs, mc1, color=CLR_MC1, linewidth=1.4, label="Channel 1")
        any_line = True
    if any(v is not None for v in mc2):
        ax.plot(xs, mc2, color=CLR_MC2, linewidth=1.2,
                linestyle="--", label="Channel 2")
        any_line = True

    if not any_line:
        tab.render_empty("No moisture data for this range")
        return
    ax.set_ylabel("moisture content (%)", color=MPL_FG)
    ax.legend(facecolor=MPL_BG, edgecolor=MPL_GRID, labelcolor=MPL_FG, fontsize=_MPL_LEGEND_FS)
    _fmt_xaxis(ax, xs)
    tab.canvas_widget.draw_idle()


def _render_stage(
    tab: _PlotTab,
    xs: List[float],
    cols: Dict[str, List[Any]],
) -> None:
    fig = tab.figure
    fig.clear()
    ax = fig.add_subplot(111)
    _style_axes(ax)

    stage_col = cols.get("stage") or []
    idx, labels = _encode_stages(stage_col)

    if not labels:
        tab.render_empty("No stage data for this range")
        return

    # Step chart. Matplotlib drops None; substitute NaN so the step
    # routine sees gaps instead of crashing.
    y = [float("nan") if v is None else v for v in idx]
    ax.step(xs, y, where="post", color=CLR_LUMBER, linewidth=1.5)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels)
    ax.set_ylabel("stage", color=MPL_FG)
    _fmt_xaxis(ax, xs)
    tab.canvas_widget.draw_idle()


def _render_diagnostics(
    tab: _PlotTab,
    xs: List[float],
    cols: Dict[str, List[Any]],
) -> None:
    fig = tab.figure
    fig.clear()
    ax = fig.add_subplot(111)
    _style_axes(ax)

    ex = cols.get("exhaust_pct") or []
    circ = cols.get("circ_pct") or []
    vi = cols.get("vent_intake") or []
    ve = cols.get("vent_exhaust") or []

    any_line = False
    if any(v is not None for v in ex):
        ax.plot(xs, ex, color=CLR_EXHAUST, linewidth=1.3, label="Exhaust %")
        any_line = True
    if any(v is not None for v in circ):
        ax.plot(xs, circ, color=CLR_CIRC, linewidth=1.3, label="Circulation %")
        any_line = True
    ax.set_ylabel("fan speed (%)", color=MPL_FG)

    # Vents on a secondary axis - they are servo positions (0-100) but it's
    # cleaner to keep them visually separated from the fan PWM lines.
    ax2 = None
    if any(v is not None for v in vi) or any(v is not None for v in ve):
        ax2 = ax.twinx()
        _style_axes(ax2)
        ax2.set_ylabel("vent position", color=MPL_FG)
        if any(v is not None for v in vi):
            ax2.plot(xs, vi, color=CLR_VENT_I, linewidth=1.0,
                     linestyle=":", label="Vent intake")
            any_line = True
        if any(v is not None for v in ve):
            ax2.plot(xs, ve, color=CLR_VENT_E, linewidth=1.0,
                     linestyle=":", label="Vent exhaust")
            any_line = True

    if not any_line:
        tab.render_empty("No diagnostics data for this range")
        return
    # Combined legend
    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = (ax2.get_legend_handles_labels() if ax2 else ([], []))
    if lines1 or lines2:
        ax.legend(
            lines1 + lines2, labels1 + labels2,
            facecolor=MPL_BG, edgecolor=MPL_GRID, labelcolor=MPL_FG, fontsize=_MPL_LEGEND_FS,
            loc="best",
        )
    _fmt_xaxis(ax, xs)
    tab.canvas_widget.draw_idle()


# ---- The screen -----------------------------------------------------------


class HistoryScreen(Screen):
    """Five-tab time-series plot view sourced from /history.

    Lifecycle:
      - `preselect_run(run_id)` may be called by the app before `on_enter`
        to jump straight to a specific run (from the Runs detail view).
      - On enter, loads the /runs list to populate the dropdown.
      - Selecting a run (or defaulting to most recent) loads /history
        once; changing the time range re-renders the cached data.
    """

    def __init__(self, connection: ConnectionManager, **kwargs):
        super().__init__(name="history", **kwargs)
        self.connection = connection
        self._runs: List[Dict[str, Any]] = []
        self._selected_run: Optional[str] = None
        self._pending_preselect: Optional[str] = None
        self._range_hours: Optional[int] = DEFAULT_RANGE_HOURS
        self._current_mode = MODE_OFFLINE
        self._in_flight_runs = False
        self._in_flight_history = False
        self._auto_refresh_event = None
        # Cached /status from last _reload_all - used to decide whether
        # the selected run is the currently-running one.
        self._active_status: Optional[Dict[str, Any]] = None
        # Cached unpacked history (full, unfiltered). Re-rendered on range change.
        self._times: List[datetime.datetime] = []
        self._cols: Dict[str, List[Any]] = {}

        # Background
        with self.canvas.before:
            self._bg_color = Color(*theme.BG_DARK)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(
            pos=lambda w, v: setattr(self._bg_rect, "pos", v),
            size=lambda w, v: setattr(self._bg_rect, "size", v),
        )

        root = BoxLayout(
            orientation="vertical",
            padding=(dp(8), dp(6), dp(8), dp(6)),
            spacing=dp(4),
        )

        # Header: run dropdown + refresh
        header = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=dp(36), spacing=dp(6)
        )
        self.run_button = Button(
            text="(no run selected)",
            font_size="12sp",
            background_color=(0.30, 0.32, 0.38, 1),
            color=(1, 1, 1, 1),
        )
        self.run_button.bind(on_release=self._open_run_dropdown)
        header.add_widget(self.run_button)

        refresh_btn = Button(
            text="Refresh",
            size_hint_x=None,
            width=dp(84),
            font_size="12sp",
            background_color=(0.30, 0.55, 0.85, 1),
            color=(1, 1, 1, 1),
        )
        refresh_btn.bind(on_release=lambda _b: self._reload_all())
        header.add_widget(refresh_btn)
        root.add_widget(header)

        # Range buttons
        self._range_buttons: List[Tuple[Button, Optional[int]]] = []
        range_row = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=dp(34), spacing=dp(4)
        )
        for label, hours in RANGE_OPTIONS:
            btn = Button(
                text=label,
                font_size="12sp",
                background_color=(0.25, 0.27, 0.32, 1),
                color=(1, 1, 1, 1),
            )
            btn.bind(
                on_release=lambda _b, h=hours: self._on_range_selected(h)
            )
            range_row.add_widget(btn)
            self._range_buttons.append((btn, hours))
        self._update_range_button_styles()
        root.add_widget(range_row)

        # Status line
        self.status_label = Label(
            text="",
            color=theme.TEXT_SECONDARY,
            font_size="11sp",
            size_hint_y=None,
            height=dp(20),
            halign="left",
            valign="middle",
        )
        self.status_label.bind(
            size=lambda w, s: setattr(w, "text_size", s)
        )
        root.add_widget(self.status_label)

        # Tabbed plot panel. Tab width is fixed so five tabs fit cleanly
        # inside the 390dp phone-shaped window without the last tab getting
        # clipped (5 * 72dp + spacing = ~360dp).
        self.tabs = TabbedPanel(
            do_default_tab=False,
            tab_width=dp(72),
            tab_height=dp(34),
            background_color=(0.15, 0.16, 0.19, 1),
        )
        self.tab_thermal = _PlotTab("Temp")
        self.tab_humidity = _PlotTab("RH")
        self.tab_moisture = _PlotTab("MC")
        self.tab_stage = _PlotTab("Stage")
        self.tab_diag = _PlotTab("Diag")
        for t in (
            self.tab_thermal, self.tab_humidity, self.tab_moisture,
            self.tab_stage, self.tab_diag,
        ):
            self.tabs.add_widget(t)
        self.tabs.default_tab = self.tab_thermal
        self.tabs.switch_to(self.tab_thermal)
        root.add_widget(self.tabs)

        self.add_widget(root)

        # Render empty placeholders immediately so the plot area is not
        # blank white before the first fetch lands.
        self._render_all_empty("Select a run to view history")

        self.connection.add_listener(self._on_connection_change)

    # ---- public API from app/router --------------------------------------

    def preselect_run(self, run_id: Optional[str]) -> None:
        """Called by the app when navigating here from the Runs detail
        view. Takes effect on the next `on_enter`."""
        self._pending_preselect = run_id

    # ---- lifecycle --------------------------------------------------------

    def on_enter(self, *_args):
        # If we came in via preselect_run, remember the target; otherwise
        # we just reload the runs list and let the user pick.
        if self._pending_preselect:
            self._selected_run = self._pending_preselect
            self._pending_preselect = None
        self._reload_all()

    def on_leave(self, *_args):
        # Drop the auto-refresh timer when the screen is off so we don't
        # hammer /history every 30s forever.
        self._cancel_auto_refresh()

    def _on_connection_change(self, result: DetectResult) -> None:
        self._current_mode = result.mode
        if result.mode == MODE_OFFLINE:
            self.status_label.text = "Offline - waiting for connection."
            self._cancel_auto_refresh()

    # ---- auto-refresh timer ----------------------------------------------

    def _selected_run_is_active(self) -> bool:
        """True only when /status reports a live run AND the selected
        run is the one the firmware is currently writing to. Matches by
        `active_run_id` (new firmware) or falls back to runs[0] (old)."""
        if not self._selected_run or not self._active_status:
            return False
        if not self._active_status.get("run_active"):
            return False
        active_id = self._active_status.get("active_run_id")
        if active_id:
            return self._selected_run == active_id
        # Older firmware: index 0 of the mtime-sorted list is the active run.
        return bool(self._runs) and self._runs[0].get("id") == self._selected_run

    def _schedule_auto_refresh(self) -> None:
        self._cancel_auto_refresh()
        if self._selected_run_is_active():
            self._auto_refresh_event = Clock.schedule_interval(
                lambda _dt: self._auto_refresh_tick(), ACTIVE_RUN_REFRESH_S
            )

    def _cancel_auto_refresh(self) -> None:
        if self._auto_refresh_event is not None:
            self._auto_refresh_event.cancel()
            self._auto_refresh_event = None

    def _auto_refresh_tick(self) -> None:
        if self._in_flight_history or self._in_flight_runs:
            return
        if self._current_mode == MODE_OFFLINE:
            return
        if not self._selected_run:
            return
        # Refetch just the history; the runs list doesn't change mid-run.
        self._fetch_history(self._selected_run)

    # ---- runs dropdown ----------------------------------------------------

    def _reload_all(self) -> None:
        if self._current_mode == MODE_OFFLINE:
            self.status_label.text = "Offline - waiting for connection."
            return
        # Fire /status in parallel so `_selected_run_is_active()` has a
        # fresh read when we arm the auto-refresh timer. Failure here is
        # non-fatal - we simply won't auto-refresh.
        client = self.connection.client

        def done_status(result, err):
            if err is None and isinstance(result, dict):
                self._active_status = result

        call_async(lambda: client.status(), done_status)
        self._fetch_runs(then_load_history=True)

    def _fetch_runs(self, *, then_load_history: bool) -> None:
        if self._in_flight_runs:
            return
        if self.connection.client.config.base_url is None:
            return
        self._in_flight_runs = True
        self.status_label.text = "Loading runs..."
        client = self.connection.client

        def work():
            return client.runs()

        def done(result, err):
            self._in_flight_runs = False
            if err is not None:
                self.status_label.text = f"Runs load failed: {err}"
                return
            self._runs = (result or {}).get("runs") or []
            # Default selection:
            #   - drop any prior selection that no longer exists
            #   - if the kiln is actively running, jump to the active
            #     run. Prefer the firmware-reported `active_run_id`;
            #     older firmware falls back to runs[0] (newest by mtime).
            #     This keeps users from getting stuck on an old run
            #     while the dashboard shows a new one.
            #   - otherwise keep the existing selection, or pick the
            #     most recent.
            if self._selected_run is not None:
                if not any(r.get("id") == self._selected_run for r in self._runs):
                    self._selected_run = None
            run_active = bool(
                self._active_status
                and self._active_status.get("run_active")
            )
            if run_active and self._runs:
                active_id = (
                    self._active_status.get("active_run_id")
                    if self._active_status else None
                )
                # Only honour `active_run_id` if it's actually in the
                # runs list (guard against a race where /status reports
                # a run id that /runs hasn't yet enumerated).
                if active_id and any(
                    r.get("id") == active_id for r in self._runs
                ):
                    self._selected_run = active_id
                else:
                    self._selected_run = self._runs[0].get("id")
            elif self._selected_run is None and self._runs:
                self._selected_run = self._runs[0].get("id")
            self._update_run_button_text()
            if then_load_history and self._selected_run:
                self._fetch_history(self._selected_run)
            elif not self._runs:
                self.status_label.text = "No runs found on device."
                self._render_all_empty("No runs recorded")

        call_async(work, done)

    def _open_run_dropdown(self, _btn) -> None:
        if not self._runs:
            return
        dd = DropDown()

        # Pull the active run to the top of the dropdown. When the Pico
        # RTC isn't set at run-start the new run's file mtime is near
        # epoch-2000, dropping it to the bottom of the mtime-desc sort
        # on the server. Users then can't find the run they just started.
        runs = list(self._runs)
        active_id = None
        if self._active_status and self._active_status.get("run_active"):
            active_id = self._active_status.get("active_run_id")
        if active_id:
            for i, r in enumerate(runs):
                if r.get("id") == active_id and i != 0:
                    runs.insert(0, runs.pop(i))
                    break

        for run in runs:
            run_id = run.get("id") or "?"
            rows = run.get("data_rows", 0)
            is_active = bool(active_id) and run.get("id") == active_id
            suffix = " (active)" if is_active else ""
            primary = format_run_label(run)
            label = f"{primary}{suffix}  --  {rows} rows"
            item = Button(
                text=label,
                size_hint_y=None,
                height=dp(32),
                font_size="12sp",
                background_color=(0.20, 0.22, 0.26, 1),
                color=(1, 1, 1, 1),
                halign="left",
                valign="middle",
            )
            item.bind(size=lambda w, s: setattr(w, "text_size", s))
            item.bind(
                on_release=lambda b, rid=run_id: self._select_run(rid, dd)
            )
            dd.add_widget(item)
        dd.open(self.run_button)

    def _select_run(self, run_id: str, dd: DropDown) -> None:
        dd.dismiss()
        if run_id == self._selected_run:
            return
        self._selected_run = run_id
        self._update_run_button_text()
        self._fetch_history(run_id)

    def _update_run_button_text(self) -> None:
        if not self._selected_run:
            self.run_button.text = "(select a run)"
            return
        # Prefer the formatted primary label over the raw rid.
        for r in self._runs:
            if r.get("id") == self._selected_run:
                self.run_button.text = f"Run: {format_run_label(r)}"
                return
        self.run_button.text = f"Run: {self._selected_run}"

    # ---- range selection --------------------------------------------------

    def _on_range_selected(self, hours: Optional[int]) -> None:
        if hours == self._range_hours:
            return
        self._range_hours = hours
        self._update_range_button_styles()
        # Re-render from cache - no refetch.
        self._render_all_plots()

    def _update_range_button_styles(self) -> None:
        for btn, hours in self._range_buttons:
            if hours == self._range_hours:
                btn.background_color = (0.30, 0.55, 0.85, 1)
            else:
                btn.background_color = (0.25, 0.27, 0.32, 1)

    # ---- /history fetch + unpack -----------------------------------------

    def _fetch_history(self, run_id: str) -> None:
        if self._in_flight_history:
            return
        if self.connection.client.config.base_url is None:
            return
        self._in_flight_history = True
        self.status_label.text = f"Loading history for {run_id}..."
        client = self.connection.client

        def work():
            return client.history(run=run_id)

        def done(result, err):
            self._in_flight_history = False
            if err is not None:
                self.status_label.text = f"History load failed: {err}"
                self._render_all_empty("Load failed")
                return
            payload = result or {}
            cols = _unpack_columnar(payload)
            # Parse timestamps once; everything downstream uses the list.
            self._times = [_parse_ts(v) for v in cols.get("ts", [])]
            self._cols = cols
            row_count = payload.get("row_count", len(self._times))
            self.status_label.text = (
                f"Loaded {row_count} rows from {run_id}"
            )
            self._render_all_plots()
            # Re-arm the auto-refresh timer now that we know whether
            # the selected run is still active.
            self._schedule_auto_refresh()

        call_async(work, done)

    # ---- rendering --------------------------------------------------------

    def _render_all_empty(self, msg: str) -> None:
        for tab in (
            self.tab_thermal, self.tab_humidity, self.tab_moisture,
            self.tab_stage, self.tab_diag,
        ):
            tab.render_empty(msg)

    def _render_all_plots(self) -> None:
        if not self._times:
            self._render_all_empty("No data")
            return
        times, cols = _filter_range(self._times, self._cols, self._range_hours)
        if not times:
            self._render_all_empty("No data for this time range")
            return
        xs = _xs_numeric(times)
        _render_thermal(self.tab_thermal, xs, cols)
        _render_humidity(self.tab_humidity, xs, cols)
        _render_moisture(self.tab_moisture, xs, cols)
        _render_stage(self.tab_stage, xs, cols)
        _render_diagnostics(self.tab_diag, xs, cols)
