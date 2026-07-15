import unittest

from app import SPACE_MARGIN_FRACTION, _space_shortfall


class TestSpaceShortfall(unittest.TestCase):
    TOTAL = 1_000_000_000_000  # 1 TB disk
    MARGIN = int(TOTAL * SPACE_MARGIN_FRACTION)

    def test_fits_with_room_to_spare(self):
        self.assertEqual(
            _space_shortfall(10_000_000_000, self.TOTAL, 500_000_000_000), 0
        )

    def test_fits_exactly_at_margin_boundary(self):
        need = 10_000_000_000
        self.assertEqual(
            _space_shortfall(need, self.TOTAL, need + self.MARGIN), 0
        )

    def test_rejected_one_byte_below_margin(self):
        need = 10_000_000_000
        self.assertEqual(
            _space_shortfall(need, self.TOTAL, need + self.MARGIN - 1), 1
        )

    def test_rejected_when_copy_would_fill_disk(self):
        # Fits byte-wise but leaves the disk 100% full — must be rejected.
        need = 100_000_000_000
        self.assertEqual(
            _space_shortfall(need, self.TOTAL, need), self.MARGIN
        )

    def test_zero_need_still_requires_margin(self):
        # A disk already inside the margin can't accept even a tiny copy.
        self.assertEqual(
            _space_shortfall(0, self.TOTAL, self.MARGIN - 5), 5
        )


if __name__ == "__main__":
    unittest.main()
