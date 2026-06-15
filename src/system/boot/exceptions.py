"""Boot pipeline exceptions."""

from __future__ import annotations


class Gate1FatalError(RuntimeError):
    """G1 preflight failed — process must not bind API or continue boot."""

    def __init__(self, message: str, *, exit_code: int = 3) -> None:
        super().__init__(message)
        self.exit_code = exit_code
