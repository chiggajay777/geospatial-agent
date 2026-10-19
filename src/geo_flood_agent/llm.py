"""OpenAI-compatible tool calling (OpenAI, Ollama, or any /v1 server).

The model may choose tools and draft a narrative. It does not compute scores.
If the draft contains a number that is not a rounding of a tool output, the
draft is rejected and the deterministic narrative is kept.
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from typing import Any, Callable

_NUMBER = re.compile(r"(?<![A-Za-z0-9_])(-?\d+(?:\.\d+)?)")


def collect_numbers(obj: Any, found: list[float] | None = None) -> list[float]:
    if found is None:
        found = []
    if isinstance(obj, bool) or obj is None:
        return found
    if isinstance(obj, (int, float)):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return found
        found.append(float(obj))
        return found
    if isinstance(obj, str):
        for match in _NUMBER.findall(obj):
            found.append(float(match))
        return found
    if isinstance(obj, dict):
        for v in obj.values():
            collect_numbers(v, found)
        return found
    if isinstance(obj, (list, tuple)):
        for v in obj:
            collect_numbers(v, found)
    return found


def audit_numbers(text: str, allowed_payload: Any) -> list[str]:
    """Return number strings in `text` that are not a rounding of an allowed value."""
    allowed = collect_numbers(allowed_payload)
    bad: list[str] = []
    if not text:
        return bad
    for match in _NUMBER.findall(text):
        value = float(match)
        decimals = len(match.split(".", 1)[1]) if "." in match else 0
        tol = 0.5 * (10 ** (-decimals)) + 1e-6
        if any(abs(value - src) <= tol for src in allowed):
            continue
        bad.append(match)
    return bad


def chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    timeout: float = 90.0,
) -> dict:
    """POST /chat/completions. Returns the assistant message dict."""
    url = base_url.rstrip("/") + "/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "geo-flood-agent/0.3",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM connection failed: {exc.reason}") from exc
    try:
        return payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LLM response missing choices[0].message: {payload!r:.400}") from exc


ToolFn = Callable[[dict], dict]


def run_tool_loop(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools_schema: list[dict],
    execute: ToolFn,
    max_rounds: int = 8,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Run tool calls until the model returns a final message or rounds run out.

    `execute` receives the parsed arguments plus the tool name under key `__name__`
    and returns a JSON-serializable dict.
    """
    steps: list[dict] = []
    final = ""
    for round_i in range(max_rounds):
        message = chat_completion(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            tools=tools_schema,
            timeout=timeout,
        )
        tool_calls = message.get("tool_calls") or []
        # Append the assistant turn (content may be null when tool calls are present).
        messages.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            }
        )
        if not tool_calls:
            final = message.get("content") or ""
            break
        for call in tool_calls:
            fn = (call.get("function") or {})
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                args = {}
            try:
                result = execute({"__name__": name, **args})
                ok = True
                error = None
            except Exception as exc:  # noqa: BLE001 — recorded in the trace
                result = {"error": f"{type(exc).__name__}: {exc}"}
                ok = False
                error = result["error"]
            steps.append(
                {
                    "round": round_i + 1,
                    "tool": name,
                    "arguments": args,
                    "ok": ok,
                    "error": error,
                    "result": result,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or name,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )
    else:
        final = final or ""
    return {"final": final, "steps": steps, "messages": messages}
