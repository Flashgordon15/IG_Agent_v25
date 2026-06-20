"""IG Agent v30.0 Project Apex — desktop shell integration."""

from apex.ipc_bridge import (
    ApexIpcBridge,
    get_ipc_bridge,
    start_ipc_bridge_daemon,
    stop_ipc_bridge,
)

__all__ = [
    "ApexIpcBridge",
    "get_ipc_bridge",
    "start_ipc_bridge_daemon",
    "stop_ipc_bridge",
]
