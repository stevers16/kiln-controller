"""LoRa link quality panel.

Shown on the Dashboard when the app is connected to the Pi4 daemon. The
Pi4 augments /status with the most recently observed RSSI / SNR plus a
`last_packet_age_s` so the user can see the radio link's health at a
glance.

The panel is a no-op when the values are absent (e.g. the daemon hasn't
heard a packet yet, or we are in AP mode and the dashboard is hiding
this widget anyway).
"""

from __future__ import annotations

from typing import Optional

from kivy.graphics import Color, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label

from kilnapp import theme
from kilnapp.widgets.cards import Panel, small_label, value_label


_BAR_COUNT = 5


class _SignalBars(BoxLayout):
    """Five vertical bars; the first N are coloured, the rest greyed.

    Heights step up so the bars look like a phone signal indicator
    rather than a flat row.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("orientation", "horizontal")
        kwargs.setdefault("spacing", 2)
        kwargs.setdefault("size_hint", (None, None))
        kwargs.setdefault("size", (60, 22))
        super().__init__(**kwargs)
        self._bars = []
        for i in range(_BAR_COUNT):
            bar = BoxLayout(
                orientation="vertical",
                size_hint=(None, None),
                size=(8, 6 + i * 4),
            )
            with bar.canvas.before:
                color = Color(*theme.TEXT_MUTED)
                rect = Rectangle(pos=bar.pos, size=bar.size)
            bar.bind(
                pos=lambda w, v, r=rect: setattr(r, "pos", v),
                size=lambda w, v, r=rect: setattr(r, "size", v),
            )
            bar._color = color
            bar._rect = rect
            # Vertically pin to bottom of the parent row so taller bars
            # extend upward, like a real signal indicator.
            self.add_widget(bar)
            self._bars.append(bar)

    def set_level(self, level: int) -> None:
        level = max(0, min(_BAR_COUNT, int(level or 0)))
        for i, bar in enumerate(self._bars):
            if i < level:
                # Step from green (full) -> amber -> red as the level drops.
                if level >= 4:
                    rgba = theme.SEVERITY_OK
                elif level >= 2:
                    rgba = theme.SEVERITY_WARN
                else:
                    rgba = theme.SEVERITY_ERROR
            else:
                rgba = theme.TEXT_MUTED
            bar._color.rgba = rgba


class LoraLinkPanel(Panel):
    """Compact panel: 5-bar indicator + RSSI/SNR + last-packet age."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_widget(small_label("LoRa link", bold=True))

        row = BoxLayout(
            orientation="horizontal",
            size_hint_y=None,
            height=24,
            spacing=8,
        )
        self._bars = _SignalBars()
        row.add_widget(self._bars)
        self._summary = value_label("--", size="13sp")
        self._summary.size_hint_x = 1
        row.add_widget(self._summary)
        self.add_widget(row)

        self._age_label = small_label("Last packet: --", size="11sp")
        self.add_widget(self._age_label)

    def update(
        self,
        *,
        rssi_dbm: Optional[float],
        snr_db: Optional[float],
        bars: int,
        age_s: Optional[float],
    ) -> None:
        self._bars.set_level(bars)

        parts = []
        if rssi_dbm is not None:
            parts.append(f"{int(rssi_dbm)} dBm")
        if snr_db is not None:
            parts.append(f"SNR {snr_db:.1f} dB")
        self._summary.text = " | ".join(parts) if parts else "no packet yet"

        if age_s is None:
            self._age_label.text = "Last packet: --"
        elif age_s < 60:
            self._age_label.text = f"Last packet: {int(age_s)}s ago"
        elif age_s < 3600:
            self._age_label.text = f"Last packet: {int(age_s / 60)}m ago"
        else:
            self._age_label.text = f"Last packet: {age_s / 3600:.1f}h ago"
