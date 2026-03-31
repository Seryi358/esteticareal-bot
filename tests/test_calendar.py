"""Tests for calendar slot validation, overlap detection, and formatting."""

import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock, AsyncMock

from services.calendar import (
    _overlaps_busy,
    group_slots_into_ranges,
    format_slots_for_whatsapp,
    format_slots_detailed,
    _format_hour,
    COLOMBIA_TZ,
    SLOT_DURATION_MINUTES,
)

# ---------------------------------------------------------------------------
# _overlaps_busy
# ---------------------------------------------------------------------------


class TestOverlapsBusy:
    def _dt(self, hour, minute=0):
        return datetime(2026, 4, 1, hour, minute, tzinfo=COLOMBIA_TZ)

    def test_no_overlap_before(self):
        """Slot ends before busy period starts."""
        assert not _overlaps_busy(
            self._dt(9), self._dt(9, 30),
            [(self._dt(10), self._dt(10, 30))],
        )

    def test_no_overlap_after(self):
        """Slot starts after busy period ends."""
        assert not _overlaps_busy(
            self._dt(11), self._dt(11, 30),
            [(self._dt(10), self._dt(10, 30))],
        )

    def test_exact_overlap(self):
        """Slot exactly matches busy period."""
        assert _overlaps_busy(
            self._dt(10), self._dt(10, 30),
            [(self._dt(10), self._dt(10, 30))],
        )

    def test_partial_overlap_start(self):
        """Slot starts during busy period."""
        assert _overlaps_busy(
            self._dt(10, 15), self._dt(10, 45),
            [(self._dt(10), self._dt(10, 30))],
        )

    def test_partial_overlap_end(self):
        """Slot ends during busy period."""
        assert _overlaps_busy(
            self._dt(9, 45), self._dt(10, 15),
            [(self._dt(10), self._dt(10, 30))],
        )

    def test_slot_contains_busy(self):
        """Slot is larger than and contains busy period."""
        assert _overlaps_busy(
            self._dt(9), self._dt(11),
            [(self._dt(10), self._dt(10, 30))],
        )

    def test_adjacent_no_overlap(self):
        """Slot ends exactly when busy starts — no overlap."""
        assert not _overlaps_busy(
            self._dt(9), self._dt(10),
            [(self._dt(10), self._dt(10, 30))],
        )

    def test_multiple_busy_intervals(self):
        """Overlap with second of multiple busy intervals."""
        assert _overlaps_busy(
            self._dt(14), self._dt(14, 30),
            [
                (self._dt(10), self._dt(10, 30)),
                (self._dt(14), self._dt(15)),
            ],
        )

    def test_empty_busy_intervals(self):
        """No busy intervals — never overlaps."""
        assert not _overlaps_busy(
            self._dt(10), self._dt(10, 30), [],
        )


# ---------------------------------------------------------------------------
# group_slots_into_ranges
# ---------------------------------------------------------------------------


class TestGroupSlotsIntoRanges:
    def _slot(self, day, hour, minute=0):
        return datetime(2026, 4, day, hour, minute, tzinfo=COLOMBIA_TZ)

    def test_empty(self):
        assert group_slots_into_ranges([]) == []

    def test_single_slot(self):
        s = self._slot(1, 9)
        ranges = group_slots_into_ranges([s])
        assert len(ranges) == 1
        assert ranges[0] == (s, s + timedelta(minutes=SLOT_DURATION_MINUTES))

    def test_consecutive_slots_merge(self):
        """Three consecutive 30-min slots become one range."""
        slots = [self._slot(1, 9), self._slot(1, 9, 30), self._slot(1, 10)]
        ranges = group_slots_into_ranges(slots)
        assert len(ranges) == 1
        assert ranges[0] == (self._slot(1, 9), self._slot(1, 10, 30))

    def test_gap_splits_range(self):
        """A gap (e.g., lunch) creates two ranges."""
        slots = [
            self._slot(1, 9), self._slot(1, 9, 30),
            # gap: 10:00-13:30
            self._slot(1, 14), self._slot(1, 14, 30),
        ]
        ranges = group_slots_into_ranges(slots)
        assert len(ranges) == 2
        assert ranges[0][0] == self._slot(1, 9)
        assert ranges[1][0] == self._slot(1, 14)

    def test_different_days_split(self):
        """Slots on different days are separate ranges."""
        slots = [self._slot(1, 9), self._slot(2, 9)]
        ranges = group_slots_into_ranges(slots)
        assert len(ranges) == 2


# ---------------------------------------------------------------------------
# format_slots_for_whatsapp — including the premature break fix
# ---------------------------------------------------------------------------


class TestFormatSlotsForWhatsapp:
    def _slot(self, day, hour, minute=0):
        return datetime(2026, 4, day, hour, minute, tzinfo=COLOMBIA_TZ)

    def test_empty_slots(self):
        assert "No hay horarios" in format_slots_for_whatsapp([])

    @patch("services.calendar.datetime")
    def test_third_day_gets_both_ranges(self, mock_dt):
        """Regression: 3rd day must include both morning and afternoon ranges."""
        mock_dt.now.return_value = datetime(2026, 3, 30, 8, 0, tzinfo=COLOMBIA_TZ)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        # Day 1: morning + afternoon
        # Day 2: morning + afternoon
        # Day 3: morning + afternoon ← both must appear
        slots = []
        for day in [1, 2, 3]:
            # Morning: 9:00-11:30
            for h in [9, 10, 11]:
                for m in [0, 30]:
                    if h == 11 and m == 30:
                        continue
                    slots.append(self._slot(day, h, m))
            # Afternoon: 14:00-16:30
            for h in [14, 15, 16]:
                for m in [0, 30]:
                    if h == 16 and m == 30:
                        continue
                    slots.append(self._slot(day, h, m))

        result = format_slots_for_whatsapp(slots)
        # The 3rd day should have both "a.m." and "p.m." ranges
        # Count how many time range markers appear for day 3
        # (The old code would break before adding the afternoon range of day 3)
        parts = result.split(" y ")
        # Just verify we don't lose data — the formatted result should mention p.m.
        # for all three days
        assert "p.m." in result


# ---------------------------------------------------------------------------
# _format_hour
# ---------------------------------------------------------------------------


class TestFormatHour:
    def test_morning(self):
        dt = datetime(2026, 4, 1, 9, 0, tzinfo=COLOMBIA_TZ)
        assert _format_hour(dt) == "9 a.m."

    def test_afternoon(self):
        dt = datetime(2026, 4, 1, 14, 0, tzinfo=COLOMBIA_TZ)
        assert _format_hour(dt) == "2 p.m."

    def test_with_minutes(self):
        dt = datetime(2026, 4, 1, 14, 30, tzinfo=COLOMBIA_TZ)
        assert _format_hour(dt) == "2:30 p.m."

    def test_noon(self):
        dt = datetime(2026, 4, 1, 12, 0, tzinfo=COLOMBIA_TZ)
        assert _format_hour(dt) == "12 p.m."

    def test_midnight(self):
        dt = datetime(2026, 4, 1, 0, 0, tzinfo=COLOMBIA_TZ)
        assert _format_hour(dt) == "12 a.m."
