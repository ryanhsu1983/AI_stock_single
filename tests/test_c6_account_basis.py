import unittest
from rotation_radar.c6_account_basis import allocate_sale_basis, INITIAL_CAPITAL


class BasisTests(unittest.TestCase):
    def test_withdrawal_does_not_change_remaining_position_return(self):
        for close in (80, 100, 125):
            sold, remaining = allocate_sale_basis(1_000_000, 10_000, 600)
            self.assertEqual(sold, 60_000)
            self.assertEqual(remaining, 940_000)
            self.assertAlmostEqual(9400 * close / remaining - 1, 10000 * close / 1_000_000 - 1)

    def test_full_sale_and_invalid_quantities(self):
        self.assertEqual(allocate_sale_basis(1234.56, 10, 10), (1234.56, 0))
        for quantity in (0, -1, 11):
            with self.assertRaises(ValueError):
                allocate_sale_basis(1234.56, 10, quantity)

    def test_confirmed_opening_cash_is_not_profit(self):
        self.assertAlmostEqual(INITIAL_CAPITAL - 494914, 6876689.04 + 305358)
