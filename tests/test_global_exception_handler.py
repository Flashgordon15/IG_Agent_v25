"""Global uncaught exception handler — logging-only, trading-safe."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.global_exception_handler import (
    install_global_exception_handlers,
    reset_global_exception_handlers_for_tests,
)


class GlobalExceptionHandlerTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_global_exception_handlers_for_tests()

    def test_install_is_idempotent(self) -> None:
        self.assertTrue(install_global_exception_handlers(force=True))
        self.assertFalse(install_global_exception_handlers())
        self.assertIsNotNone(threading.excepthook)

    def test_thread_excepthook_logs_without_exit(self) -> None:
        install_global_exception_handlers(force=True)

        def _boom() -> None:
            raise RuntimeError("boom")

        with patch("system.guard.runtime_guard.log_guarded_exception") as mock_log:
            th = threading.Thread(target=_boom, name="test-uncaught")
            th.start()
            th.join(timeout=2.0)
            self.assertFalse(th.is_alive())
            self.assertGreaterEqual(mock_log.call_count, 1)


if __name__ == "__main__":
    unittest.main()
