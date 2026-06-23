"""
Runtime Metaclass & Global Interception Kernel.

Installs ``sys.excepthook`` and wraps every function in ``trading/`` and
``execution/`` so unhandled exceptions always emit full tracebacks via
``runtime_guard.log_guarded_exception``. Hot-path failures fail-closed with
``sys.exit(99)`` unless ``IG_KERNEL_SOFT=1`` or ``IG_AGENT_PYTEST=1``.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import pkgutil
import sys
import threading
import types
from typing import Any, Callable, TypeVar

from system.engine_log import log_engine
from system.guard.runtime_guard import log_guarded_exception
from system.shutdown_cleanup import is_broker_connectivity_failure

F = TypeVar("F", bound=Callable[..., Any])

_HARD_EXIT = 99
_INSTALLED = False
_INSTALL_LOCK = threading.Lock()
_WRAPPED_IDS: set[int] = set()
_HOT_METHODS = frozenset(
    {
        "_execute_order_blocking",
        "_run_tick",
        "run_once",
    }
)
_HOT_PACKAGES = ("trading", "execution")


def _soft_mode() -> bool:
    if os.environ.get("IG_AGENT_PYTEST", "").strip() == "1":
        return True
    if os.environ.get("IG_TEST_HARNESS", "").strip() == "1":
        return True
    if os.environ.get("IG_KERNEL_SOFT", "").strip().lower() in ("1", "true", "yes"):
        return True
    return False


def _fail_closed_enabled() -> bool:
    return not _soft_mode()


def dispatch_broker_connectivity_teardown(exc: BaseException, *, source: str) -> None:
    """Route broker connectivity loss into supervised shutdown — never returns when armed."""
    if not is_broker_connectivity_failure(exc):
        return
    if os.environ.get("IG_SUPERVISED_NETWORK_TEARDOWN", "").strip() != "1":
        return
    from system.shutdown_cleanup import perform_network_failure_teardown

    perform_network_failure_teardown(exc, source=source)


def _kernel_excepthook(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: Any,
) -> None:
    if exc_type is KeyboardInterrupt:
        sys.__excepthook__(exc_type, exc, tb)
        return
    log_guarded_exception(
        "kernel_excepthook",
        exc,
        detail=f"{exc_type.__name__} uncaught at top level",
    )
    dispatch_broker_connectivity_teardown(exc, source="kernel_excepthook")
    if _fail_closed_enabled():
        log_engine(
            f"KernelInterceptor HARD FAIL-CLOSED: uncaught {exc_type.__name__} — exit {_HARD_EXIT}"
        )
        sys.exit(_HARD_EXIT)
    sys.__excepthook__(exc_type, exc, tb)


def kernel_guard(subsystem: str) -> Callable[[F], F]:
    """Explicit decorator for hot-path callables outside auto-wrap sweep."""

    def _decorator(fn: F) -> F:
        return _wrap_callable(fn, subsystem)

    return _decorator


def _wrap_callable(fn: Callable[..., Any], qualname: str) -> Callable[..., Any]:
    fn_id = id(fn)
    if fn_id in _WRAPPED_IDS:
        return fn
    if isinstance(fn, (classmethod, staticmethod)):
        return fn

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            dispatch_broker_connectivity_teardown(exc, source=f"kernel:{qualname}")
            log_guarded_exception(f"kernel:{qualname}", exc)
            if _fail_closed_enabled():
                log_engine(
                    f"KernelInterceptor HARD FAIL-CLOSED: {qualname} — exit {_HARD_EXIT}"
                )
                sys.exit(_HARD_EXIT)
            raise

    _WRAPPED_IDS.add(fn_id)
    _WRAPPED_IDS.add(id(wrapper))
    return wrapper


def _instrument_module(mod: types.ModuleType) -> int:
    if not mod.__name__.startswith(_HOT_PACKAGES):
        return 0
    wrapped = 0
    for name, obj in list(vars(mod).items()):
        if name.startswith("_"):
            continue
        if inspect.isfunction(obj) and getattr(obj, "__module__", None) == mod.__name__:
            setattr(mod, name, _wrap_callable(obj, f"{mod.__name__}.{name}"))
            wrapped += 1
            continue
        if inspect.isclass(obj) and getattr(obj, "__module__", None) == mod.__name__:
            for meth_name, meth in inspect.getmembers(obj, predicate=inspect.isfunction):
                if meth_name.startswith("_") and meth_name not in _HOT_METHODS:
                    continue
                original = getattr(obj, meth_name, None)
                if original is None or id(original) in _WRAPPED_IDS:
                    continue
                wrapped_fn = _wrap_callable(original, f"{obj.__name__}.{meth_name}")
                try:
                    setattr(obj, meth_name, wrapped_fn)
                    wrapped += 1
                except (AttributeError, TypeError):
                    pass
    return wrapped


def _walk_and_instrument_package(root_name: str) -> int:
    total = 0
    try:
        root = importlib.import_module(root_name)
    except ImportError as exc:
        log_guarded_exception(f"kernel_import_{root_name}", exc)
        return 0
    total += _instrument_module(root)
    path = getattr(root, "__path__", None)
    if not path:
        return total
    for modinfo in pkgutil.walk_packages(path, prefix=f"{root_name}."):
        try:
            mod = importlib.import_module(modinfo.name)
        except Exception as exc:
            log_guarded_exception(f"kernel_import_{modinfo.name}", exc)
            continue
        total += _instrument_module(mod)
    return total


def _bare_metal_fast_arm() -> bool:
    """Skip O(n) module walk on unified bare-metal boot — excepthook only."""
    return os.environ.get("IG_BARE_METAL_EXEC", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def install_kernel_interceptor(*, force: bool = False) -> dict[str, Any]:
    """
    Idempotent global install — excepthook + trading/execution method matrix.

    Returns install telemetry for audit logs.
    """
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED and not force:
            return {"installed": True, "skipped": True}
        sys.excepthook = _kernel_excepthook
        if _bare_metal_fast_arm():
            _INSTALLED = True
            if _fail_closed_enabled():
                os.environ["IG_SUPERVISED_NETWORK_TEARDOWN"] = "1"
            summary = {
                "installed": True,
                "trading_wrapped": 0,
                "execution_wrapped": 0,
                "fail_closed": _fail_closed_enabled(),
                "bare_metal_fast_arm": True,
            }
            log_engine(
                "KernelInterceptor: bare-metal fast-arm "
                f"(excepthook only) fail_closed={summary['fail_closed']}"
            )
            return summary
        trading_wrapped = _walk_and_instrument_package("trading")
        execution_wrapped = _walk_and_instrument_package("execution")
        _INSTALLED = True
        if _fail_closed_enabled():
            os.environ["IG_SUPERVISED_NETWORK_TEARDOWN"] = "1"
        summary = {
            "installed": True,
            "trading_wrapped": trading_wrapped,
            "execution_wrapped": execution_wrapped,
            "fail_closed": _fail_closed_enabled(),
        }
        log_engine(
            "KernelInterceptor: armed "
            f"trading={trading_wrapped} execution={execution_wrapped} "
            f"fail_closed={summary['fail_closed']}"
        )
        return summary


def ensure_kernel_armed_for_execution() -> None:
    """Arm interceptor when execution hot-path modules load outside ``main``."""
    if os.environ.get("IG_KERNEL_ARMED") == "1":
        return
    try:
        install_kernel_interceptor()
        os.environ["IG_KERNEL_ARMED"] = "1"
    except Exception as exc:
        log_guarded_exception("kernel_auto_arm", exc)


def reset_kernel_interceptor_for_tests() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        _INSTALLED = False
        _WRAPPED_IDS.clear()
        sys.excepthook = sys.__excepthook__


# Late import — avoid circular dependency at module load.
import os  # noqa: E402
