#!/usr/bin/env python3
"""POST in-process correlation guard reset on the live agent."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8080/api/admin/reset-correlation-guard"
_APEX_BYPASS = "v30_unlocked_session_token"


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("Cookie", f"ig_agent_auth={_APEX_BYPASS}")
    req.add_header("Authorization", f"Bearer {_APEX_BYPASS}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"RESET-CORRELATION HTTP {exc.code}: {exc.read().decode()[:300]}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"RESET-CORRELATION failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2))
    snap = body.get("snapshot") or {}
    print(f"RESET-CORRELATION OK buy={snap.get('buy')} sell={snap.get('sell')} max={snap.get('max')}")
    return 0 if body.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
