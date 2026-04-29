"""
IMA-Agent Backend Server
Wraps ZAIScraper and exposes it as a local REST API
that the VS Code extension talks to.

Endpoints:
  POST /chat          — send a message, get a response
  POST /new_chat      — start a fresh conversation
  GET  /status        — health check
  GET  /history       — full conversation history
  POST /refresh       — reload the browser page

Architecture note:
  Playwright's sync API must be used exclusively from the thread that
  created it.  All browser work is dispatched via a queue to a single
  long-lived "browser thread"; FastAPI worker threads simply block on
  a threading.Event waiting for the result.
"""

import time
import queue
import threading
from datetime import datetime
from playwright.sync_api import sync_playwright

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn


# ─────────────────────────────────────────────────
# Task dispatcher — run everything on one thread
# ─────────────────────────────────────────────────

class _Task:
    def __init__(self, fn, *args, **kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.result = None
        self.error = None
        self.done = threading.Event()

    def run(self):
        try:
            self.result = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            self.error = e
        finally:
            self.done.set()


class BrowserThread:
    """
    A single daemon thread that owns the Playwright instance.
    All callers submit a callable; the thread executes it and
    returns the result (or re-raises the exception) to the caller.
    """

    def __init__(self):
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="browser-thread")
        self._thread.start()

    def _loop(self):
        while True:
            task = self._queue.get()
            if task is None:          # poison pill — shut down
                break
            task.run()

    def run(self, fn, *args, timeout=180, **kwargs):
        """Submit fn(*args, **kwargs) to the browser thread and block until done."""
        task = _Task(fn, *args, **kwargs)
        self._queue.put(task)
        if not task.done.wait(timeout=timeout):
            raise TimeoutError(f"Browser task timed out after {timeout}s")
        if task.error:
            raise task.error
        return task.result

    def stop(self):
        self._queue.put(None)


_browser_thread = BrowserThread()


# ─────────────────────────────────────────────────
# ZAIScraper — all methods run on the browser thread
# ─────────────────────────────────────────────────

class ZAIScraper:
    def __init__(self, headless=False):
        self.headless = headless
        self.browser = None
        self.context = None
        self.page = None
        self.playwright = None

        self.INPUT_SELECTOR    = "textarea#chat-input"
        self.STOP_BTN_SELECTOR = "button[aria-label*='stop' i], button[aria-label*='Stop' i]"
        self.RESPONSE_SELECTOR = "div.markdown-prose"

    # ── internal (must be called on browser thread) ──

    def _start(self):
        print("[*] Launching browser...")
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        self.page = self.context.new_page()
        print("[*] Opening https://chat.z.ai/ ...")
        self.page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=30000)
        self._wait_for_splash_gone()
        self._handle_auth()
        self._handle_captcha()
        self._wait_for_input()
        print("[+] ZAI browser ready!")

    def _wait_for_splash_gone(self):
        print("[*] Waiting for SvelteKit to hydrate...")
        try:
            self.page.wait_for_selector("#splash-screen", state="hidden", timeout=20000)
            print("[+] App hydrated.")
        except Exception:
            print("[!] Splash timeout — continuing...")

    def _handle_auth(self):
        time.sleep(1.5)
        current_url = self.page.url
        if "/auth" in current_url or "/login" in current_url:
            print(f"\n🔐 Login required! URL: {current_url}")
            print("    Please log in inside the browser window.")
            input("    Press Enter after logging in... ")
            try:
                self.page.wait_for_url("**/", timeout=60000)
            except Exception:
                pass
            self._wait_for_splash_gone()

    def _handle_captcha(self):
        signals = [
            "iframe[src*='recaptcha']", "iframe[src*='captcha']",
            ".g-recaptcha", "text=Verify you are human", "text=Security Check",
        ]
        for sig in signals:
            try:
                if self.page.locator(sig).count() > 0:
                    print("\n⚠️  CAPTCHA detected!")
                    input("    Solve in browser, then press Enter... ")
                    return
            except Exception:
                pass

    def _wait_for_input(self):
        print("[*] Waiting for chat input box...")
        try:
            self.page.wait_for_selector(self.INPUT_SELECTOR, state="visible", timeout=20000)
            print("[+] Input box ready.")
        except Exception:
            print(f"[!] Input not found — URL: {self.page.url}")

    def _send_message(self, message: str) -> dict:
        try:
            self._wait_for_stream_complete(timeout=10)
            before_count = self.page.locator(self.RESPONSE_SELECTOR).count()

            input_box = self.page.locator(self.INPUT_SELECTOR)
            input_box.click()
            input_box.fill("")
            input_box.type(message, delay=25)
            time.sleep(0.2)
            input_box.press("Enter")

            self._wait_for_new_response(before_count)
            self._wait_for_stream_complete()
            time.sleep(0.5)

            return {
                "response": self._scrape_response(),
                "thinking": self._scrape_thinking(),
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            return {"error": str(e)}

    def _wait_for_new_response(self, before_count: int, timeout: int = 30):
        start = time.time()
        while time.time() - start < timeout:
            if self.page.locator(self.RESPONSE_SELECTOR).count() > before_count:
                return True
            time.sleep(0.3)
        return False

    def _wait_for_stream_complete(self, timeout: int = 120):
        start = time.time()
        stop = self.page.locator(self.STOP_BTN_SELECTOR)

        if timeout > 15:
            appeared = False
            while time.time() - start < 5:
                try:
                    if stop.count() > 0 and stop.is_visible():
                        appeared = True
                        break
                except Exception:
                    pass
                time.sleep(0.2)

            if appeared:
                while time.time() - start < timeout:
                    try:
                        if not stop.is_visible():
                            return
                    except Exception:
                        return
                    time.sleep(0.5)
                return

        last_text = ""
        stable_ticks = 0
        while time.time() - start < timeout:
            try:
                blocks = self.page.locator(self.RESPONSE_SELECTOR).all()
                if blocks:
                    current = blocks[-1].inner_text()
                    if current and current == last_text:
                        stable_ticks += 1
                        if stable_ticks >= 6:
                            return
                    else:
                        stable_ticks = 0
                        last_text = current
            except Exception:
                pass
            time.sleep(0.5)

    def _scrape_response(self) -> str:
        try:
            result = self.page.evaluate("""() => {
                const blocks = document.querySelectorAll('div.markdown-prose');
                if (!blocks || blocks.length === 0) return '[Error] No response found';
                const last = blocks[blocks.length - 1];
                const clone = last.cloneNode(true);
                ['blockquote','details','[class*="think"]','[class*="reason"]',
                 '[class*="internal"]','[data-type="thinking"]'].forEach(sel => {
                    clone.querySelectorAll(sel).forEach(el => el.remove());
                });
                const els = clone.querySelectorAll('p, li, h1, h2, h3, h4, h5, h6, pre, td');
                if (els.length > 0) {
                    const seen = new Set();
                    const texts = [];
                    els.forEach(el => {
                        const t = el.innerText?.trim() || '';
                        if (t && !seen.has(t)) { seen.add(t); texts.push(t); }
                    });
                    if (texts.length > 0) return texts.join('\\n\\n');
                }
                return clone.innerText?.trim() || '[Error] Empty response';
            }""")
            return result.strip() if result else "[Error] Empty response"
        except Exception as e:
            return f"[Scrape Error] {str(e)}"

    def _scrape_thinking(self) -> str:
        try:
            result = self.page.evaluate("""() => {
                const blocks = document.querySelectorAll('div.markdown-prose');
                if (!blocks || blocks.length === 0) return '';
                const last = blocks[blocks.length - 1];
                const bq = last.querySelector('blockquote');
                if (bq) return bq.innerText?.trim() || '';
                const details = last.querySelector('details');
                if (details) return details.innerText?.trim() || '';
                return '';
            }""")
            return result.strip() if result else ""
        except Exception:
            return ""

    def _new_chat(self) -> bool:
        try:
            selectors = [
                "button[aria-label*='new' i]", "button[aria-label*='New' i]",
                "button:has-text('New Chat')", "button:has-text('New chat')",
                "[class*='new-chat']", "[data-testid*='new']", "a[href='/']",
            ]
            clicked = False
            for sel in selectors:
                try:
                    btn = self.page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                self.page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=15000)
                self._wait_for_splash_gone()

            self._wait_for_input()
            return True
        except Exception as e:
            print(f"[-] New chat error: {e}")
            return False

    def _get_history(self) -> list:
        try:
            return self.page.evaluate("""() => {
                const history = [];
                document.querySelectorAll('div.markdown-prose').forEach(block => {
                    const clone = block.cloneNode(true);
                    clone.querySelectorAll('blockquote, details, [class*="think"], [class*="reason"]')
                         .forEach(el => el.remove());
                    const paras = clone.querySelectorAll('p, li, h1, h2, h3');
                    const text = paras.length > 0
                        ? Array.from(paras).map(p => p.innerText?.trim() || '').filter(t => t).join('\\n\\n')
                        : clone.innerText?.trim() || '';
                    if (text) history.push({ role: 'assistant', content: text });
                });
                return history;
            }""") or []
        except Exception:
            return []

    def _refresh(self) -> bool:
        try:
            self.page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=15000)
            self._wait_for_splash_gone()
            self._wait_for_input()
            return True
        except Exception:
            return False

    def _get_status(self) -> dict:
        try:
            info = self.page.evaluate("""() => ({
                url: window.location.href,
                inputReady: !!document.querySelector('textarea#chat-input'),
                responseCount: document.querySelectorAll('div.markdown-prose').length,
            })""")
            return {"online": True, **info}
        except Exception:
            return {"online": False}

    def _close(self):
        try:
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass

    # ── Public API — dispatches every call to the browser thread ──

    def start(self):
        _browser_thread.run(self._start, timeout=180)

    def send_message(self, message: str) -> dict:
        return _browser_thread.run(self._send_message, message, timeout=180)

    def new_chat(self) -> bool:
        return _browser_thread.run(self._new_chat, timeout=30)

    def get_history(self) -> list:
        return _browser_thread.run(self._get_history, timeout=15)

    def refresh(self) -> bool:
        return _browser_thread.run(self._refresh, timeout=30)

    def get_status(self) -> dict:
        return _browser_thread.run(self._get_status, timeout=10)

    def close(self):
        _browser_thread.run(self._close, timeout=10)


# ─────────────────────────────────────────────────
# FastAPI App
# ─────────────────────────────────────────────────

app = FastAPI(
    title="IMA-Agent API",
    description="Local API bridge between IMA-Agent VS Code extension and chat.z.ai",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

scraper: ZAIScraper = None
scraper_ready = threading.Event()


# ── Models ──

class ChatRequest(BaseModel):
    message: str

class ChatResponse(BaseModel):
    response: str
    thinking: str = ""
    timestamp: str = ""
    error: str = ""


# ── Dependency ──

def require_scraper() -> ZAIScraper:
    if scraper is None or not scraper_ready.is_set():
        raise HTTPException(status_code=503, detail="Browser not ready yet — please wait")
    return scraper


# ── Routes ──

@app.get("/status")
def status():
    if scraper is None or not scraper_ready.is_set():
        return {"online": False, "message": "Scraper not initialized"}
    return scraper.get_status()


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    s = require_scraper()
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    result = s.send_message(req.message)
    if "error" in result and result["error"]:
        raise HTTPException(status_code=500, detail=result["error"])
    return ChatResponse(
        response=result.get("response", ""),
        thinking=result.get("thinking", ""),
        timestamp=result.get("timestamp", ""),
    )


@app.post("/new_chat")
def new_chat():
    s = require_scraper()
    return {"success": s.new_chat()}


@app.get("/history")
def history():
    s = require_scraper()
    return {"history": s.get_history()}


@app.post("/refresh")
def refresh():
    s = require_scraper()
    return {"success": s.refresh()}


# ─────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────

def start_scraper():
    global scraper
    scraper = ZAIScraper(headless=False)
    scraper.start()
    scraper_ready.set()
    print("[+] Scraper ready — API is fully operational.")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("   IMA-Agent Backend Server")
    print("   API will be available at: http://localhost:8765")
    print("=" * 60 + "\n")

    init_thread = threading.Thread(target=start_scraper, daemon=True, name="init-thread")
    init_thread.start()

    print("[*] Waiting for browser to initialize (up to 120 s)...")
    if not scraper_ready.wait(timeout=120):
        print("[!] Browser did not initialize in time — starting API anyway.")

    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")
