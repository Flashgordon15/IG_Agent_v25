#!/usr/bin/env python3
"""
Out-of-process Telegram HTTPS handshake — independent of the unified headless engine.

Validates async delivery to api.telegram.org with a production-style closed-trade payload.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_INJECTED_TOKEN = "d84asthr01qutij8i4a0d84asthr01qutij8i4ag"
_INJECTED_CHAT = "c33d709357dd4ef8823d4e3eefdac056"

_TELEGRAM_API = "https://api.telegram.org"
_LONDON = ZoneInfo("Europe/London")


def build_closed_trade_markdown() -> str:
    """Production layout: Spot Gold winner with SHM sync confirmation."""
    now = datetime.now(_LONDON).strftime("%H:%M BST")
    return (
        "✅ TRADE CLOSED — Spot Gold\n"
        "Epic: CS.D.CFPGOLD.CFP.IP\n"
        "Action: BUY | Size: 1.0\n"
        "Entry: 2,345.50 → Exit: 2,350.00\n"
        "Outcome: WIN | Realised P&L: +£450.00\n"
        "SHM Sync: TRUE SYNC\n"
        f"⏱ {now}\n"
        "\n"
        "verify_telegram_final — out-of-process handshake"
    )


async def send_telegram_handshake(*, token: str, chat: str) -> tuple[int, dict]:
    """Direct async HTTPS POST to Telegram sendMessage endpoint."""
    if not token or not chat:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")

    url = f"{_TELEGRAM_API}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat,
        "text": build_closed_trade_markdown(),
        "disable_web_page_preview": True,
    }

    print("[verify_telegram] boot: async HTTPS session → api.telegram.org")
    print(f"[verify_telegram] endpoint: {_TELEGRAM_API}/bot<redacted>/sendMessage")
    print(f"[verify_telegram] chat_id: {chat[:6]}…{chat[-4:]}")

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload)

    body: dict = {}
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = {"raw": response.text}

    return response.status_code, body


async def main() -> int:
    print("[verify_telegram] === Telegram Final Handshake ===")

    injected_token, injected_chat = _INJECTED_TOKEN, _INJECTED_CHAT
    print("[verify_telegram] phase 1: injected credential handshake")
    status, body = await send_telegram_handshake(token=injected_token, chat=injected_chat)

    if status != 200 or not body.get("ok"):
        print(
            f"[verify_telegram] injected handshake failed "
            f"(HTTP {status}: {body.get('description', 'unknown')})"
        )
        try:
            from analytics.post_open_audit import resolve_telegram_credentials

            prod_token, prod_chat = resolve_telegram_credentials()
        except Exception as exc:
            print(f"[verify_telegram] credential fallback unavailable: {exc}")
            prod_token, prod_chat = "", ""

        if prod_token and prod_chat and (
            prod_token != injected_token or prod_chat != injected_chat
        ):
            print("[verify_telegram] phase 2: project credentials handshake")
            status, body = await send_telegram_handshake(token=prod_token, chat=prod_chat)

    ok = status == 200 and bool(body.get("ok"))

    print(f"[verify_telegram] HTTP status: {status} {'OK' if status == 200 else 'FAIL'}")
    print(f"[verify_telegram] telegram ok: {body.get('ok')}")
    if body.get("result"):
        msg_id = body["result"].get("message_id")
        print(f"[verify_telegram] message_id: {msg_id}")
    if not ok:
        print(f"[verify_telegram] error: {body.get('description') or body}")
        return 1

    print("[verify_telegram] ✅ 200 OK — handshake complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
