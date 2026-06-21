"""Regression — OrderValidator scalping halt callable from instance."""

from __future__ import annotations

import unittest

from execution.order_validator import OrderValidator


class OrderValidatorScalpingHaltTests(unittest.TestCase):
    def test_check_scalping_entry_halt_via_instance(self) -> None:
        validator = OrderValidator.__new__(OrderValidator)
        ok, msg = validator.check_scalping_entry_halt()
        self.assertTrue(ok)
        self.assertIsInstance(msg, str)

    def test_check_scalping_entry_halt_static_call(self) -> None:
        ok, msg = OrderValidator.check_scalping_entry_halt()
        self.assertTrue(ok)
        self.assertIsInstance(msg, str)


if __name__ == "__main__":
    unittest.main()
