"""
chat.z.ai → Anthropic API Proxy
================================
Exposes a local HTTP server that speaks the Anthropic Messages API,
but routes every request through chat.z.ai via Playwright.

Usage:
    pip install playwright flask
    playwright install chromium
    python server.py

Then in another shell:
    export ANTHROPIC_BASE_URL="http://localhost:8765"
    export ANTHROPIC_API_KEY="local-proxy-key"
    claude   # or any tool that uses the Anthropic SDK
"""

import json
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from flask import Flask, request, Response, jsonify

# ── reuse the scraper from zai_scraper.py (must be in same folder) ──
from zai_scraper_fast import ZAIScraper

# ─────────────────────────────────────────────────────────────────────────────
# Global scraper instance (one browser, one session)
# ─────────────────────────────────────────────────────────────────────────────

_scraper: ZAIScraper | None = None
_scraper_lock = threading.Lock()


def get_scraper() -> ZAIScraper:
    global _scraper
    with _scraper_lock:
        if _scraper is None:
            _scraper = ZAIScraper(headless=False)
            _scraper.start()
        return _scraper


# ─────────────────────────────────────────────────────────────────────────────
# Message Formatting Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _messages_to_prompt(messages: list) -> str:
    """
    Flatten the Anthropic `messages` array into a single plain-text prompt.
    Handles:
      - Simple string content
      - Content block arrays (text / image / tool_result / tool_use)
      - System prompt injected at the top (passed separately)
    """
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "tool_result":
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            for rb in result_content:
                                if isinstance(rb, dict) and rb.get("type") == "text":
                                    text_parts.append(f"[Tool Result]\n{rb.get('text','')}")
                        else:
                            text_parts.append(f"[Tool Result]\n{result_content}")
                    elif btype == "tool_use":
                        name = block.get("name", "tool")
                        inp  = json.dumps(block.get("input", {}), indent=2)
                        text_parts.append(f"[Tool Call: {name}]\n{inp}")
            text = "\n".join(text_parts)
        else:
            text = str(content)

        if role == "system":
            parts.append(f"[System]\n{text}")
        elif role == "user":
            parts.append(f"Human: {text}")
        elif role == "assistant":
            parts.append(f"Assistant: {text}")
        else:
            parts.append(text)

    parts.append("Assistant:")
    return "\n\n".join(parts)


def _build_response_body(content_text: str, model: str, usage_in: int = 0) -> dict:
    """Build a valid Anthropic /v1/messages response object."""
    usage_out = len(content_text.split())
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": content_text,
            }
        ],
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens":  max(usage_in, 1),
            "output_tokens": max(usage_out, 1),
        },
    }


def _sse_event(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


def _stream_response(content_text: str, model: str):
    """
    Yield SSE events that match the Anthropic streaming format:
      message_start → content_block_start → content_block_delta(s)
      → content_block_stop → message_delta → message_stop
    """
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    yield _sse_event({
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    })

    yield _sse_event({"type": "content_block_start", "index": 0,
                       "content_block": {"type": "text", "text": ""}})

    chunk_size = 40
    for i in range(0, len(content_text), chunk_size):
        chunk = content_text[i: i + chunk_size]
        yield _sse_event({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": chunk},
        })

    yield _sse_event({"type": "content_block_stop", "index": 0})

    yield _sse_event({
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": max(len(content_text.split()), 1)},
    })

    yield _sse_event({"type": "message_stop"})
    yield "data: [DONE]\n\n"


# ─────────────────────────────────────────────────────────────────────────────
# Flask App
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.after_request
def _add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, x-api-key, anthropic-version"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


def _cors_response():
    resp = Response("", status=204)
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, x-api-key, anthropic-version"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# ── /v1/messages ─────────────────────────────────────────────────────────────

@app.route("/v1/messages", methods=["POST", "OPTIONS"])
def messages():
    if request.method == "OPTIONS":
        return _cors_response()

    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": {"type": "invalid_request_error",
                                   "message": "Invalid JSON body"}}), 400

    model    = body.get("model", "claude-3-5-sonnet-20241022")
    messages = body.get("messages", [])
    system   = body.get("system", "")
    stream   = body.get("stream", False)

    if not messages:
        return jsonify({"error": {"type": "invalid_request_error",
                                   "message": "messages required"}}), 400

    # ── Build the prompt ──────────────────────────────────────────────────────
    all_messages = []
    if system:
        all_messages.append({"role": "system", "content": system})
    all_messages.extend(messages)

    # Simple single-turn: send last user message directly.
    # Multi-turn: inject full history as structured prompt.
    if len([m for m in messages if m.get("role") == "user"]) == 1 and not system:
        last_user = next(
            (m for m in reversed(messages) if m.get("role") == "user"), None
        )
        content = last_user.get("content", "") if last_user else ""
        if isinstance(content, list):
            prompt = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            prompt = str(content)
    else:
        prompt = _messages_to_prompt(all_messages)

    if not prompt.strip():
        return jsonify({"error": {"type": "invalid_request_error",
                                   "message": "Empty prompt"}}), 400

    # ── Send to z.ai ──────────────────────────────────────────────────────────
    try:
        scraper = get_scraper()
        # send_message returns (md, html, was_web, elapsed)
        md, html, was_web, elapsed = scraper.send_message(prompt)
    except Exception as e:
        return jsonify({"error": {"type": "api_error",
                                   "message": f"z.ai scraper error: {e}"}}), 500

    if md.startswith("[Error]"):
        return jsonify({"error": {"type": "api_error",
                                   "message": md}}), 500

    # ── Return response ───────────────────────────────────────────────────────
    if stream:
        return Response(
            _stream_response(md, model),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        input_tokens = max(len(prompt.split()), 1)
        resp_body = _build_response_body(md, model, usage_in=input_tokens)
        return jsonify(resp_body)


# ── /v1/models — Claude Code queries this on startup ─────────────────────────

@app.route("/v1/models", methods=["GET", "OPTIONS"])
def list_models():
    if request.method == "OPTIONS":
        return _cors_response()
    return jsonify({
        "data": [
            {
                "id": "claude-opus-4-5",
                "object": "model",
                "created": 1720000000,
                "owned_by": "anthropic",
            },
            {
                "id": "claude-sonnet-4-5",
                "object": "model",
                "created": 1720000000,
                "owned_by": "anthropic",
            },
            {
                "id": "claude-haiku-3-5",
                "object": "model",
                "created": 1720000000,
                "owned_by": "anthropic",
            },
        ]
    })


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "proxy": "chat.z.ai → Anthropic API",
        "time":  datetime.now().isoformat(),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="chat.z.ai Anthropic API Proxy")
    parser.add_argument("--host",      default="0.0.0.0",  help="Bind host")
    parser.add_argument("--port",      default=8765, type=int, help="Port")
    parser.add_argument("--headless",  action="store_true",  help="Run browser headless")
    parser.add_argument("--no-warmup", action="store_true",
                        help="Don't pre-launch browser on startup")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("   chat.z.ai  →  Anthropic API Proxy")
    print("=" * 60)
    print(f"  Listening on : http://{args.host}:{args.port}")
    print(f"  Headless     : {args.headless}")
    print()
    print("  Set these in your shell, then run Claude Code:")
    print(f"    export ANTHROPIC_BASE_URL=\"http://localhost:{args.port}\"")
    print(f"    export ANTHROPIC_API_KEY=\"local-proxy-key\"")
    print()
    print("  Supported endpoints:")
    print("    POST /v1/messages   (streaming + non-streaming)")
    print("    GET  /v1/models")
    print("    GET  /health")
    print("=" * 60 + "\n")

    if not args.no_warmup:
        print("[*] Pre-launching browser (--no-warmup to skip)...")

        import zai_scraper_fast as _zs
        _orig_init = _zs.ZAIScraper.__init__
        def _patched_init(self, headless=False):
            _orig_init(self, headless=args.headless)
        _zs.ZAIScraper.__init__ = _patched_init

        get_scraper()
        print("[+] Browser ready. Proxy is live!\n")

    app.run(host=args.host, port=args.port, threaded=False, debug=False)
