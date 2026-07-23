"""Project root and path resolution — macOS .app bundle aware."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_APEX_V30_SUPPORT = Path("Library") / "Application Support" / "IG Agent Apex" / "v30-production"
_BRIDGED_MARK = ".data_root_bridged"
_BRIDGE_REL_PATHS = (
    "trade_support_status.json",
    "edits_only_close_queue.json",
    "runtime_state.json",
    "state/broker_snapshot.json",
    "learning_db.sqlite3",
    "ml_training_store.jsonl",
    "ml_model",
    "ohlc_cache",
)


def is_v30_monolith() -> bool:
    """True when the active runtime is the v30 Apex monolith (isolated App Support store)."""
    try:
        from system.app_identity import APP_VERSION

        return str(APP_VERSION).startswith("30.")
    except Exception:
        return False


def is_v31_desk() -> bool:
    """True for Trading Desk v31.x — uses src/data/v31-production by default."""
    try:
        from system.app_identity import APP_VERSION

        return str(APP_VERSION).startswith("31.")
    except Exception:
        return False


def legacy_src_data_dir() -> Path:
    """Pre-unification writable tree (src/data) — bridge source only."""
    d = project_root() / "src" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def v31_production_data_dir() -> Path:
    d = project_root() / "src" / "data" / "v31-production"
    d.mkdir(parents=True, exist_ok=True)
    _ensure_data_subdirs(d)
    return d


def apex_isolated_root() -> Path:
    """
    v30 Apex isolated data namespace — never shares legacy src/data blocks.

    macOS: ~/Library/Application Support/IG Agent Apex/v30-production/
    """
    if sys.platform == "darwin":
        root = Path.home() / _APEX_V30_SUPPORT
    else:
        root = Path.home() / ".ig-agent-apex" / "v30-production"
    root.mkdir(parents=True, exist_ok=True)
    return root


def legacy_data_roots() -> tuple[Path, ...]:
    """Writable legacy paths that v30 must never read or write."""
    root = project_root()
    return (
        root / "src" / "data",
        root / "src" / "analytics",
        root / "data_lake",
    )


def _use_apex_isolated_store() -> bool:
    if os.environ.get("IG_AGENT_LEGACY_DATA", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    if os.environ.get("IG_AGENT_DATA_DIR", "").strip():
        return False
    if is_v30_monolith():
        return True
    if os.environ.get("IG_APEX_DESKTOP") == "1":
        return True
    profile = os.environ.get("IG_NODE_PROFILE", "").strip().lower()
    if profile in ("shadow", "v30", "sandbox"):
        return True
    node_env = os.environ.get("NODE_ENV", "").strip().lower()
    return node_env in ("shadow", "v30", "sandbox", "development")


def is_legacy_data_path(path: Path | str) -> bool:
    """Return True if ``path`` resolves under a forbidden legacy data root."""
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return False
    for legacy in legacy_data_roots():
        try:
            legacy.resolve()
            resolved.relative_to(legacy.resolve())
            return True
        except ValueError:
            continue
    return False


def coerce_writable_path(path: Path | str, *, subdir: str = "state") -> Path:
    """
    Redirect legacy v25/v29 paths into the v30 isolated namespace when active.

    No-op when legacy data mode is explicitly enabled.
    """
    p = Path(path)
    if not _use_apex_isolated_store() or not is_legacy_data_path(p):
        return p
    target = data_dir() / subdir / p.name
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def project_root() -> Path:
    """Resolve IG Agent repository root (code + read-only bundled config)."""
    env = os.environ.get("IG_AGENT_ROOT")
    if env:
        return Path(env).resolve()

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    exe = Path(sys.argv[0]).resolve()
    parts = exe.parts

    if "Contents" in parts and "MacOS" in parts:
        idx = parts.index("Contents")
        bundle = Path(*parts[:idx])
        if bundle.parent.name == "launcher":
            return bundle.parent.parent
        return bundle.parent

    return Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    """Read-only bundled config templates (never used as writable data store)."""
    return project_root() / "config"


def _ensure_data_subdirs(root: Path) -> None:
    for sub in ("logs", "state", "state_cfd", "state_sb", "historical", "ohlc_cache"):
        (root / sub).mkdir(parents=True, exist_ok=True)


def bridge_legacy_data_into(target: Path, *, legacy: Path | None = None) -> list[str]:
    """
    One-shot bridge: copy/symlink critical artifacts from legacy src/data into
    the unified data root when missing or empty stubs.

    Safe while the agent holds the legacy learning DB open — we only replace
    empty stubs in the target tree and never truncate a non-empty target DB.
    """
    legacy = legacy or legacy_src_data_dir()
    try:
        if target.resolve() == legacy.resolve():
            return []
    except OSError:
        return []

    actions: list[str] = []
    _ensure_data_subdirs(target)
    mark = target / _BRIDGED_MARK

    for rel in _BRIDGE_REL_PATHS:
        src = legacy / rel
        dst = target / rel
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dst.exists() or dst.is_symlink():
                # Replace empty learning_db stub with a link to the real DB.
                if (
                    rel.endswith("learning_db.sqlite3")
                    and dst.is_file()
                    and not dst.is_symlink()
                    and dst.stat().st_size == 0
                    and src.is_file()
                    and src.stat().st_size > 0
                ):
                    dst.unlink()
                    os.symlink(src.resolve(), dst)
                    actions.append(f"symlink_empty_stub:{rel}")
                # Empty ohlc_cache/ dir created by _ensure_data_subdirs blocks
                # the directory symlink — populate missing bar files from legacy.
                elif (
                    rel == "ohlc_cache"
                    and dst.is_dir()
                    and not dst.is_symlink()
                    and src.is_dir()
                ):
                    try:
                        empty = not any(dst.iterdir())
                    except OSError:
                        empty = False
                    if empty:
                        try:
                            dst.rmdir()
                            os.symlink(src.resolve(), dst)
                            actions.append(f"symlink_empty_dir:{rel}")
                        except OSError:
                            for child in src.iterdir():
                                target_child = dst / child.name
                                if target_child.exists() or target_child.is_symlink():
                                    continue
                                try:
                                    os.symlink(child.resolve(), target_child)
                                    actions.append(f"symlink:{rel}/{child.name}")
                                except OSError:
                                    continue
                    else:
                        for child in src.iterdir():
                            target_child = dst / child.name
                            if target_child.exists() or target_child.is_symlink():
                                continue
                            try:
                                os.symlink(child.resolve(), target_child)
                                actions.append(f"symlink:{rel}/{child.name}")
                            except OSError:
                                continue
                continue
            if src.is_dir():
                os.symlink(src.resolve(), dst)
                actions.append(f"symlink_dir:{rel}")
            elif rel.endswith(".sqlite3"):
                os.symlink(src.resolve(), dst)
                actions.append(f"symlink:{rel}")
            else:
                # Prefer symlink for large training stores; copy small JSON status.
                if rel.endswith(".jsonl") or rel.endswith("model.pkl"):
                    os.symlink(src.resolve(), dst)
                    actions.append(f"symlink:{rel}")
                else:
                    shutil.copy2(src, dst)
                    actions.append(f"copy:{rel}")
        except OSError:
            continue

    if actions and not mark.exists():
        try:
            mark.write_text(
                "bridged_from=" + str(legacy.resolve()) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    return actions


def data_dir() -> Path:
    """
    Writable runtime state — must match session ``IG_DATA_ROOT`` when set.

    Priority:
      1. ``IG_AGENT_DATA_DIR`` (explicit override)
      2. ``IG_DATA_ROOT`` (session identity / health data_root)
      3. Apex isolated store (shadow/v30 desktop profiles)
      4. Legacy ``src/data``
    """
    env = os.environ.get("IG_AGENT_DATA_DIR", "").strip()
    if env:
        d = Path(env).resolve()
        d.mkdir(parents=True, exist_ok=True)
        _ensure_data_subdirs(d)
        bridge_legacy_data_into(d)
        return d

    ig_root = os.environ.get("IG_DATA_ROOT", "").strip()
    if ig_root:
        d = Path(ig_root).resolve()
        d.mkdir(parents=True, exist_ok=True)
        _ensure_data_subdirs(d)
        bridge_legacy_data_into(d)
        return d

    # v31 Trading Desk default — never divert to Apex Application Support.
    if is_v31_desk():
        d = v31_production_data_dir()
        bridge_legacy_data_into(d)
        return d

    if _use_apex_isolated_store():
        d = apex_isolated_root() / "data"
        _ensure_data_subdirs(d)
        return d

    d = legacy_src_data_dir()
    _ensure_data_subdirs(d)
    return d


def logs_dir() -> Path:
    """Writable logs under active data namespace."""
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def analytics_dir() -> Path:
    """SQLite triage / analytics — isolated under v30-production when active."""
    if _use_apex_isolated_store():
        d = apex_isolated_root() / "analytics"
    else:
        d = project_root() / "src" / "analytics"
    d.mkdir(parents=True, exist_ok=True)
    return d


def triage_db_path() -> Path:
    """Worker D high-speed ledger — triage_v30.db in isolated analytics namespace."""
    env = os.environ.get("IG_TRIAGE_DB", "").strip()
    if env:
        return Path(env).resolve()
    try:
        from system.node_profile import get_node_profile

        return get_node_profile().triage_db
    except Exception:
        pass
    return analytics_dir() / "triage_v30.db"


def engine_state_subdir() -> str | None:
    """Per-engine state namespace when ``IG_ENGINE_ORIGIN`` / CLI is active."""
    explicit = os.environ.get("IG_STATE_DIR", "").strip()
    if explicit:
        return None
    sub = os.environ.get("IG_ENGINE_STATE_SUBDIR", "").strip()
    if sub:
        return sub
    origin = os.environ.get("IG_ENGINE_ORIGIN", "").strip().upper()
    if origin == "QUANT_SNIPER":
        return "state_cfd"
    if origin == "MACRO_SENTINEL":
        return "state_sb"
    return None


def shared_state_dir() -> Path:
    """Cross-engine operator state (deploy hold, REST budget, manual stop)."""
    d = data_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_dir() -> Path:
    """
    Process-local runtime state.

    v32 dual-port: ``QUANT_SNIPER`` → ``state_cfd/``, ``MACRO_SENTINEL`` → ``state_sb/``.
    Single-process default remains ``state/``.
    """
    explicit = os.environ.get("IG_STATE_DIR", "").strip()
    if explicit:
        d = Path(explicit).resolve()
        d.mkdir(parents=True, exist_ok=True)
        return d
    sub = engine_state_subdir()
    if sub:
        d = data_dir() / sub
    else:
        d = data_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_lake_dir() -> Path:
    """Event lake — isolated under v30 namespace when active."""
    if _use_apex_isolated_store():
        d = apex_isolated_root() / "data_lake"
    else:
        d = project_root() / "data_lake"
    d.mkdir(parents=True, exist_ok=True)
    return d


def feeder_events_dir() -> Path:
    """Append-only feeder events for learning plane."""
    d = data_lake_dir() / "events"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_path(relative: str) -> Path:
    p = Path(relative)
    return p if p.is_absolute() else project_root() / p


def chdir_to_root() -> None:
    os.chdir(project_root())


def find_python_executable() -> str:
    import shutil

    root = project_root()
    for rel in (
        ".venv/bin/python3",
        "venv/bin/python3",
        ".venv/bin/python",
        "venv/bin/python",
    ):
        candidate = root / rel
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    candidates = (
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "python3",
        "python",
    )
    for c in candidates:
        found = shutil.which(c)
        if found:
            return found
    return "python3"
