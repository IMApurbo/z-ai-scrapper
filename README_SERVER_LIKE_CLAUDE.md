# chat.z.ai → Anthropic API Proxy

Turns `chat.z.ai` into a local Anthropic-compatible API server.  
Claude Code, the Anthropic SDK, and any other tool that speaks the Anthropic Messages API will work transparently.

---

## File layout

```
your-folder/
├── zai_scraper.py   ← your original scraper (unchanged)
└── server.py        ← this proxy server
```

---

## 1. Install dependencies

```bash
pip install flask playwright
playwright install chromium
```

---

## 2. Start the proxy

```bash
python server.py
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--port` | `8765` | Port to listen on |
| `--host` | `0.0.0.0` | Bind address |
| `--headless` | off | Run Chromium headless |
| `--no-warmup` | off | Don't pre-launch browser at startup |

The first time you run it, a Chromium window opens.  
**Log in to chat.z.ai** in that window — the proxy will wait.

---

## 3. Configure Claude Code (or any Anthropic SDK tool)

```bash
export ANTHROPIC_BASE_URL="http://localhost:8765"
export ANTHROPIC_API_KEY="local-proxy-key"

# Now just run Claude Code normally:
claude
```

That's it. Every request Claude Code sends to `api.anthropic.com` is silently redirected to your local proxy, which forwards it through the browser to `chat.z.ai`.

---

## How it works

```
Claude Code
    │  POST /v1/messages  (Anthropic SDK format)
    ▼
server.py  (Flask, port 8765)
    │  scraper.send_message(prompt)
    ▼
zai_scraper.py  (Playwright)
    │  types into textarea, waits for response
    ▼
chat.z.ai  (in Chromium)
    │  returns markdown
    ▼
server.py  →  JSON / SSE response  →  Claude Code
```

## Supported endpoints

| Endpoint | Method | Notes |
|---|---|---|
| `/v1/messages` | POST | streaming (`"stream": true`) + non-streaming |
| `/v1/models` | GET | returns a fake model list so Claude Code doesn't error |
| `/health` | GET | sanity check |

## Session persistence

After logging in you can save the session so you don't need to log in again:

```
# Inside the chat.z.ai browser window (or via the CLI):
# Type /save    →  writes zai_session.json
# Type /load    →  restores it next time
```

Or call from Python:

```python
scraper.save_session("zai_session.json")
scraper.load_session("zai_session.json")
```

## Notes

- Only one request is processed at a time (the browser is single-threaded).  
  Concurrent requests will queue.
- Images, tool-use blocks, and file uploads in the messages payload are  
  silently stripped — only text reaches z.ai.
- Token counts in the response are estimated (word count), not exact.
