import unittest

from app import (
    DEFAULT_SPACE_MARGIN_PERCENT,
    _space_margin_fraction,
    _space_shortfall,
)

DEFAULT_FRACTION = DEFAULT_SPACE_MARGIN_PERCENT / 100.0


class TestSpaceShortfall(unittest.TestCase):
    TOTAL = 1_000_000_000_000  # 1 TB disk
    MARGIN = int(TOTAL * DEFAULT_FRACTION)

    def test_fits_with_room_to_spare(self):
        self.assertEqual(
            _space_shortfall(
                10_000_000_000, self.TOTAL, 500_000_000_000, DEFAULT_FRACTION
            ),
            0,
        )

    def test_fits_exactly_at_margin_boundary(self):
        need = 10_000_000_000
        self.assertEqual(
            _space_shortfall(
                need, self.TOTAL, need + self.MARGIN, DEFAULT_FRACTION
            ),
            0,
        )

    def test_rejected_one_byte_below_margin(self):
        need = 10_000_000_000
        self.assertEqual(
            _space_shortfall(
                need, self.TOTAL, need + self.MARGIN - 1, DEFAULT_FRACTION
            ),
            1,
        )

    def test_rejected_when_copy_would_fill_disk(self):
        # Fits byte-wise but leaves the disk 100% full — must be rejected.
        need = 100_000_000_000
        self.assertEqual(
            _space_shortfall(need, self.TOTAL, need, DEFAULT_FRACTION),
            self.MARGIN,
        )

    def test_zero_need_still_requires_margin(self):
        # A disk already inside the margin can't accept even a tiny copy.
        self.assertEqual(
            _space_shortfall(0, self.TOTAL, self.MARGIN - 5, DEFAULT_FRACTION),
            5,
        )

    def test_zero_margin_allows_filling_disk(self):
        # margin 0 (config'd off): a copy may fill the disk to the last byte.
        need = 100_000_000_000
        self.assertEqual(_space_shortfall(need, self.TOTAL, need, 0.0), 0)


class TestSpaceMarginFraction(unittest.TestCase):
    def test_default_when_unset(self):
        self.assertEqual(_space_margin_fraction({}), DEFAULT_FRACTION)

    def test_default_when_cfg_is_none(self):
        self.assertEqual(_space_margin_fraction(None), DEFAULT_FRACTION)

    def test_configured_value(self):
        self.assertEqual(
            _space_margin_fraction({"space_margin_percent": 15}), 0.15
        )

    def test_zero_is_respected(self):
        self.assertEqual(
            _space_margin_fraction({"space_margin_percent": 0}), 0.0
        )

    def test_max_value(self):
        self.assertEqual(
            _space_margin_fraction({"space_margin_percent": 50}), 0.50
        )

    def test_out_of_range_falls_back_to_default(self):
        # A hand-edited config can't silently disable or explode the margin.
        self.assertEqual(
            _space_margin_fraction({"space_margin_percent": 90}),
            DEFAULT_FRACTION,
        )
        self.assertEqual(
            _space_margin_fraction({"space_margin_percent": -3}),
            DEFAULT_FRACTION,
        )

    def test_malformed_falls_back_to_default(self):
        self.assertEqual(
            _space_margin_fraction({"space_margin_percent": "lots"}),
            DEFAULT_FRACTION,
        )
        self.assertEqual(
            _space_margin_fraction({"space_margin_percent": None}),
            DEFAULT_FRACTION,
        )

    def test_drive_override_wins_over_config(self):
        cfg = {"space_margin_percent": 10, "drive_margins": {"/mnt/x": 3}}
        self.assertEqual(_space_margin_fraction(cfg, "/mnt/x"), 0.03)

    def test_drive_zero_override_wins(self):
        cfg = {"space_margin_percent": 10, "drive_margins": {"/mnt/x": 0}}
        self.assertEqual(_space_margin_fraction(cfg, "/mnt/x"), 0.0)

    def test_drive_without_override_uses_config(self):
        cfg = {"space_margin_percent": 10, "drive_margins": {"/mnt/x": 3}}
        self.assertEqual(_space_margin_fraction(cfg, "/mnt/y"), 0.10)

    def test_no_mountpoint_uses_config(self):
        cfg = {"space_margin_percent": 10, "drive_margins": {"/mnt/x": 3}}
        self.assertEqual(_space_margin_fraction(cfg), 0.10)

    def test_malformed_drive_override_falls_back_to_config(self):
        cfg = {"space_margin_percent": 10, "drive_margins": {"/mnt/x": 99}}
        self.assertEqual(_space_margin_fraction(cfg, "/mnt/x"), 0.10)

    def test_non_dict_drive_margins_falls_back_to_config(self):
        # A hand-edited config can't crash margin resolution.
        cfg = {"space_margin_percent": 10, "drive_margins": "oops"}
        self.assertEqual(_space_margin_fraction(cfg, "/mnt/x"), 0.10)


if __name__ == "__main__":
    unittest.main()
