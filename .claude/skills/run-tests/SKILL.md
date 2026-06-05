---
name: run-tests
description: Run the test suite with the correct PYTHONPATH and flags. Accepts an optional filter argument (e.g. /run-tests test_trading_loop).
disable-model-invocation: false
---

Run the pytest suite for this project. All commands require `PYTHONPATH=src`.

If $ARGUMENTS is provided, treat it as a pytest `-k` filter or a specific test file path:
```
PYTHONPATH=src python3 -m pytest tests/ -x -q -v -k "$ARGUMENTS"
```

If no argument is given, run the full suite:
```
PYTHONPATH=src python3 -m pytest tests/ -x -q
```

After running, report:
- Pass/fail count
- Any failing tests with the error message
- Whether the run was clean (no warnings about missing fixtures or import errors)
