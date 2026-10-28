#!/usr/bin/env python
"""Smoke MCP-сервера: initialize + tools/list + tools/call get_latest_status(SBER).

Ключ берётся из .env (MCP_SMOKE_KEY, создаётся scripts/mcp_key.py generate).
Ключ не печатается. Использование:
    PYTHONPATH=src python scripts/smoke_mcp.py [base_url]
base_url по умолчанию http://127.0.0.1:8900
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = REPO_ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _post(base_url: str, key: str, payload: dict, session_id: str | None = None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-API-Key": key,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(
        f"{base_url}/mcp",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return e.code, dict(e.headers), body
    body = resp.read().decode(errors="replace")
    return resp.status, dict(resp.headers), body


def _parse_body(body: str) -> dict | None:
    """Streamable-http отвечает JSON или SSE (data: {...})."""
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return None


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8900"
    key = _load_env().get("MCP_SMOKE_KEY", "")
    if not key:
        print("MCP_SMOKE_KEY не задан в .env — сгенерируй ключ scripts/mcp_key.py generate")
        return 1

    print(f"== Smoke MCP {base_url}/mcp ==")

    # 1) initialize
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "mdg-smoke", "version": "0.1.0"},
        },
    }
    status, headers, body = _post(base_url, key, init_payload)
    print(f"initialize: HTTP {status}")
    result = _parse_body(body)
    print(json.dumps(result, ensure_ascii=False)[:500])
    session_id = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
    if status != 200:
        return 1

    # 2) notifications/initialized
    _post(
        base_url,
        key,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session_id,
    )

    # 3) tools/list
    status, _, body = _post(
        base_url,
        key,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        session_id,
    )
    result = _parse_body(body)
    tools = [t.get("name") for t in (result or {}).get("result", {}).get("tools", [])]
    print(f"tools/list: HTTP {status}, tools={tools}")
    if status != 200:
        return 1

    # 4) tools/call get_latest_status(SBER)
    status, _, body = _post(
        base_url,
        key,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_latest_status",
                "arguments": {"ticker": "SBER"},
            },
        },
        session_id,
    )
    result = _parse_body(body)
    print(f"tools/call get_latest_status(SBER): HTTP {status}")
    print(json.dumps(result, ensure_ascii=False)[:1000])
    ok = status == 200 and (result or {}).get("result", {}).get("isError") is not True
    print("SMOKE:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
