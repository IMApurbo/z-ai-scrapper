import time
import json
from playwright.sync_api import sync_playwright
from datetime import datetime


class ZAIScraper:
    def __init__(self, headless=False):
        self.headless = headless
        self.browser = None
        self.context = None
        self.page = None
        self.playwright = None

        # ── Exact Selectors ──
        self.INPUT_SELECTOR    = "textarea#chat-input"
        self.STOP_BTN_SELECTOR = "button[aria-label*='stop' i], button[aria-label*='Stop' i]"
        self.RESPONSE_SELECTOR = "div.markdown-prose"   # ← EXACT from your inspection
        self.THINKING_SELECTOR = "blockquote"           # ← thinking inside markdown-prose

    # ─────────────────────────────────────────────
    # Browser Setup
    # ─────────────────────────────────────────────

    def start(self):
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

        # ── FIX: Use domcontentloaded instead of networkidle ──
        # SvelteKit keeps long-polling connections open
        # networkidle never fires → hangs forever
        self.page.goto(
            "https://chat.z.ai/",
            wait_until="domcontentloaded",  # ← KEY FIX
            timeout=30000
        )

        # Wait for splash to disappear (SvelteKit hydration)
        self._wait_for_splash_gone()

        # Handle auth redirect
        self._handle_auth()

        # Handle CAPTCHA
        self._handle_captcha()

        # Wait for input box
        self._wait_for_input()

        print("[+] Ready!")
        print("-" * 60)

    def _wait_for_splash_gone(self):
        """Wait for #splash-screen to hide (SvelteKit finished loading)"""
        print("[*] Waiting for SvelteKit to hydrate...")
        try:
            self.page.wait_for_selector(
                "#splash-screen",
                state="hidden",
                timeout=20000
            )
            print("[+] App hydrated.")
        except:
            # Splash might already be gone or not exist
            print("[!] Splash timeout — checking for input...")

    def _handle_auth(self):
        """Handle login redirect"""
        time.sleep(1.5)
        current_url = self.page.url

        if "/auth" in current_url or "/login" in current_url:
            print(f"\n🔐 Login required! URL: {current_url}")
            print("    Please log in inside the browser window.")
            input("    Press Enter after logging in... ")

            # Wait for redirect back to main
            try:
                self.page.wait_for_url(
                    "**/",
                    timeout=60000
                )
            except:
                pass
            self._wait_for_splash_gone()

    def _handle_captcha(self):
        """Detect and wait for CAPTCHA solve"""
        signals = [
            "iframe[src*='recaptcha']",
            "iframe[src*='captcha']",
            ".g-recaptcha",
            "text=Verify you are human",
            "text=Security Check",
        ]
        for sig in signals:
            try:
                if self.page.locator(sig).count() > 0:
                    print("\n⚠️  CAPTCHA detected!")
                    input("    Solve in browser, then press Enter... ")
                    return
            except:
                pass

    def _wait_for_input(self):
        """Wait for textarea#chat-input"""
        print("[*] Waiting for chat input box...")
        try:
            self.page.wait_for_selector(
                self.INPUT_SELECTOR,
                state="visible",
                timeout=20000
            )
            print("[+] Input box ready.")
        except:
            print("[!] Input not found — page may need login")
            print(f"    Current URL: {self.page.url}")

    # ─────────────────────────────────────────────
    # Core: Send Message
    # ─────────────────────────────────────────────

    def send_message(self, message: str) -> str:
        """
        1. Count existing .markdown-prose blocks
        2. Type into textarea#chat-input + Enter
        3. Wait for stop button → disappear
        4. Scrape last .markdown-prose → remove blockquote → get <p> text
        """
        if not self.page:
            return "[Error] Browser not started."

        try:
            # ── 1. Snapshot count ──
            before_count = self.page.locator(self.RESPONSE_SELECTOR).count()

            # ── 2. Type & Send ──
            input_box = self.page.locator(self.INPUT_SELECTOR)
            input_box.click()
            input_box.fill(message)
            time.sleep(0.2)
            input_box.press("Enter")

            # ── 3. Wait for new .markdown-prose to appear ──
            self._wait_for_new_response(before_count)

            # ── 4. Wait for streaming to finish ──
            self._wait_for_stream_complete()

            # ── 5. Settle ──
            time.sleep(0.5)

            # ── 6. Scrape ──
            return self._scrape_response()

        except Exception as e:
            return f"[Error] {str(e)}"

    # ─────────────────────────────────────────────
    # Wait Helpers
    # ─────────────────────────────────────────────

    def _wait_for_new_response(self, before_count: int, timeout: int = 30):
        """Wait until a new .markdown-prose block appears"""
        print("    ⌛ Waiting for response...", end="", flush=True)
        start = time.time()

        while time.time() - start < timeout:
            current = self.page.locator(self.RESPONSE_SELECTOR).count()
            if current > before_count:
                print(" ✓")
                return True
            time.sleep(0.3)

        print(" ⚠ timeout")
        return False

    def _wait_for_stream_complete(self, timeout: int = 120):
        """
        Wait for AI to finish generating.
        Strategy 1: Stop button disappears
        Strategy 2: .markdown-prose text stabilizes
        """
        start = time.time()

        # ── Strategy 1: Stop button ──
        try:
            stop = self.page.locator(self.STOP_BTN_SELECTOR)
            if stop.count() > 0 and stop.is_visible():
                print("    ⏳ Generating", end="", flush=True)
                while time.time() - start < timeout:
                    if not stop.is_visible():
                        print(" ✓")
                        return
                    print(".", end="", flush=True)
                    time.sleep(0.5)
                print()
                return
        except:
            pass

        # ── Strategy 2: Text stabilization ──
        print("    ⏳ Streaming", end="", flush=True)
        last_text = ""
        stable_ticks = 0
        needed = 6  # 3 seconds (6 × 0.5s)

        while time.time() - start < timeout:
            try:
                # Get last markdown-prose text
                blocks = self.page.locator(self.RESPONSE_SELECTOR).all()
                if blocks:
                    current = blocks[-1].inner_text()
                    if current and current == last_text:
                        stable_ticks += 1
                        if stable_ticks >= needed:
                            print(" ✓")
                            return
                    else:
                        stable_ticks = 0
                        last_text = current
                        print(".", end="", flush=True)
            except:
                pass
            time.sleep(0.5)

        print(" ⚠ timeout")

    # ─────────────────────────────────────────────
    # Scraping — div.markdown-prose → skip blockquote → <p>
    # ─────────────────────────────────────────────

    def _scrape_response(self) -> str:
        """
        DOM structure:
          <div class="markdown-prose">
            <blockquote>          ← thinking/reasoning — REMOVE
              ...internal thoughts...
            </blockquote>
            <p>Real answer here</p>    ← KEEP
            <p>More answer...</p>      ← KEEP
            <ul><li>...</li></ul>      ← KEEP
          </div>

        JS clone → remove blockquote → collect p/li/h tags
        """
        try:
            result = self.page.evaluate("""() => {
                // Get ALL .markdown-prose blocks
                const blocks = document.querySelectorAll('div.markdown-prose');
                if (!blocks || blocks.length === 0) {
                    return '[Error] No div.markdown-prose found';
                }

                // Take the LAST one (most recent response)
                const last = blocks[blocks.length - 1];

                // Clone — never modify real DOM
                const clone = last.cloneNode(true);

                // Remove thinking/reasoning blocks
                const removeSelectors = [
                    'blockquote',
                    'details',
                    '[class*="think"]',
                    '[class*="reason"]',
                    '[class*="internal"]',
                    '[data-type="thinking"]',
                ];
                removeSelectors.forEach(sel => {
                    clone.querySelectorAll(sel).forEach(el => el.remove());
                });

                // Collect content elements
                const contentSelectors = 'p, li, h1, h2, h3, h4, h5, h6, pre, td';
                const contentEls = clone.querySelectorAll(contentSelectors);

                if (contentEls.length > 0) {
                    // Deduplicate (nested li inside ul etc.)
                    const seen = new Set();
                    const texts = [];

                    contentEls.forEach(el => {
                        const t = el.innerText?.trim() || '';
                        if (t && !seen.has(t)) {
                            // Skip if this text is contained in an already-added text
                            const isNested = texts.some(existing => existing.includes(t));
                            if (!isNested) {
                                seen.add(t);
                                texts.push(t);
                            }
                        }
                    });

                    if (texts.length > 0) return texts.join('\\n\\n');
                }

                // Fallback: all remaining inner text
                const fallback = clone.innerText?.trim() || '';
                return fallback || '[Error] Empty after cleanup';
            }""")

            return result.strip() if result else "[Error] Empty response"

        except Exception as e:
            return f"[Scrape Error] {str(e)}"

    def _scrape_thinking(self) -> str:
        """Extract thinking content from blockquote inside last .markdown-prose"""
        try:
            result = self.page.evaluate("""() => {
                const blocks = document.querySelectorAll('div.markdown-prose');
                if (!blocks || blocks.length === 0) return '';

                const last = blocks[blocks.length - 1];

                // blockquote = thinking block in z.ai
                const bq = last.querySelector('blockquote');
                if (bq) return bq.innerText?.trim() || '';

                // Fallback: details/summary
                const details = last.querySelector('details');
                if (details) return details.innerText?.trim() || '';

                return '';
            }""")

            return result.strip() if result else ""

        except Exception as e:
            return f"[Think Error] {str(e)}"

    # ─────────────────────────────────────────────
    # New Chat
    # ─────────────────────────────────────────────

    def new_chat(self):
        """Start a new conversation"""
        try:
            # Common new chat button patterns
            selectors = [
                "button[aria-label*='new' i]",
                "button[aria-label*='New' i]",
                "button:has-text('New Chat')",
                "button:has-text('New chat')",
                "[class*='new-chat']",
                "[data-testid*='new']",
                "a[href='/']",
            ]

            for sel in selectors:
                try:
                    btn = self.page.locator(sel).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click()
                        time.sleep(1)
                        self._wait_for_input()
                        print("[+] New chat started.")
                        return
                except:
                    continue

            # Fallback: navigate with domcontentloaded
            print("[*] Navigating to new chat...")
            self.page.goto(
                "https://chat.z.ai/",
                wait_until="domcontentloaded",
                timeout=15000
            )
            self._wait_for_splash_gone()
            self._wait_for_input()
            print("[+] New chat started.")

        except Exception as e:
            print(f"[-] New chat error: {e}")

    # ─────────────────────────────────────────────
    # Conversation History
    # ─────────────────────────────────────────────

    def get_full_conversation(self) -> list:
        """Extract all messages from DOM"""
        try:
            history = self.page.evaluate("""() => {
                const history = [];

                // Get all markdown-prose blocks
                const blocks = document.querySelectorAll('div.markdown-prose');

                blocks.forEach(block => {
                    // Clone and clean
                    const clone = block.cloneNode(true);
                    clone.querySelectorAll(
                        'blockquote, details, [class*="think"], [class*="reason"]'
                    ).forEach(el => el.remove());

                    const paras = clone.querySelectorAll('p, li, h1, h2, h3');
                    const text = paras.length > 0
                        ? Array.from(paras)
                            .map(p => p.innerText?.trim() || '')
                            .filter(t => t.length > 0)
                            .join('\\n\\n')
                        : clone.innerText?.trim() || '';

                    if (text) history.push({ role: 'assistant', content: text });
                });

                // Try to interleave user messages
                // Look for user message elements
                const userSelectors = [
                    '[data-role="user"]',
                    '[class*="user-message"]',
                    '[class*="human-message"]',
                    '[class*="user-bubble"]',
                ];

                for (const sel of userSelectors) {
                    const userMsgs = document.querySelectorAll(sel);
                    if (userMsgs.length > 0) {
                        userMsgs.forEach(el => {
                            history.push({
                                role: 'user',
                                content: el.innerText?.trim() || ''
                            });
                        });
                        break;
                    }
                }

                // Sort by DOM position
                return history;
            }""")

            return history or []
        except Exception as e:
            print(f"[-] History error: {e}")
            return []

    # ─────────────────────────────────────────────
    # Quick DOM Debug
    # ─────────────────────────────────────────────

    def debug_dom(self):
        """Quick check of current DOM state"""
        try:
            info = self.page.evaluate("""() => {
                return {
                    url: window.location.href,
                    hasToken: !!localStorage.getItem('token'),
                    markdownProseCount: document.querySelectorAll('div.markdown-prose').length,
                    chatInputExists: !!document.querySelector('textarea#chat-input'),
                    splashVisible: (() => {
                        const s = document.querySelector('#splash-screen');
                        return s ? getComputedStyle(s).display !== 'none' : false;
                    })(),
                    lastMarkdownText: (() => {
                        const blocks = document.querySelectorAll('div.markdown-prose');
                        if (!blocks.length) return 'none';
                        return blocks[blocks.length-1].innerText?.substring(0, 200) || '';
                    })(),
                    blockquoteCount: document.querySelectorAll('div.markdown-prose blockquote').length,
                    pTagCount: document.querySelectorAll('div.markdown-prose p').length,
                };
            }""")

            print("\n" + "=" * 60)
            print("  DOM DEBUG")
            print("=" * 60)
            print(f"  URL              : {info.get('url')}")
            print(f"  Token            : {'✓' if info.get('hasToken') else '✗'}")
            print(f"  Splash visible   : {info.get('splashVisible')}")
            print(f"  #chat-input      : {'✓' if info.get('chatInputExists') else '✗'}")
            print(f"  .markdown-prose  : {info.get('markdownProseCount')} blocks")
            print(f"  blockquotes      : {info.get('blockquoteCount')}")
            print(f"  <p> tags         : {info.get('pTagCount')}")
            print(f"  Last response    : {info.get('lastMarkdownText', '')[:100]}")
            print("=" * 60 + "\n")

        except Exception as e:
            print(f"[-] Debug error: {e}")

    # ─────────────────────────────────────────────
    # Session Management
    # ─────────────────────────────────────────────

    def save_session(self, filename="zai_session.json"):
        try:
            data = {
                "cookies": self.context.cookies(),
                "localStorage": json.loads(
                    self.page.evaluate("() => JSON.stringify(localStorage)")
                ),
                "saved_at": datetime.now().isoformat()
            }
            with open(filename, "w") as f:
                json.dump(data, f, indent=2)
            print(f"[+] Session saved → {filename}")
        except Exception as e:
            print(f"[-] Save error: {e}")

    def load_session(self, filename="zai_session.json"):
        try:
            with open(filename) as f:
                data = json.load(f)
            self.context.add_cookies(data["cookies"])
            for k, v in data.get("localStorage", {}).items():
                self.page.evaluate(
                    "(k, v) => localStorage.setItem(k, v)", k, v
                )
            print(f"[+] Session loaded ← {filename}")
            self.page.goto(
                "https://chat.z.ai/",
                wait_until="domcontentloaded",
                timeout=15000
            )
            self._wait_for_splash_gone()
            self._wait_for_input()
        except FileNotFoundError:
            print("[-] No session file found.")
        except Exception as e:
            print(f"[-] Load error: {e}")

    def close(self):
        try:
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
            print("[*] Browser closed.")
        except:
            pass


# ─────────────────────────────────────────────────
# Main CLI
# ─────────────────────────────────────────────────

def main():
    scraper = None

    print("\n" + "=" * 60)
    print("   chat.z.ai — Playwright Scraper")
    print("=" * 60)

    try:
        scraper = ZAIScraper(headless=False)
        scraper.start()

        print("\nCommands:")
        print("  /new       - New conversation")
        print("  /thinking  - Show reasoning (blockquote)")
        print("  /history   - Full conversation")
        print("  /debug     - DOM state check")
        print("  /save      - Save session")
        print("  /load      - Load session")
        print("  /refresh   - Reload page")
        print("  /quit      - Exit")
        print("-" * 60 + "\n")

        while True:
            try:
                user_input = input("You: ").strip()
                if not user_input:
                    continue

                if user_input.startswith("/"):
                    cmd = user_input.lower().strip()

                    if cmd == "/quit":
                        break
                    elif cmd == "/new":
                        scraper.new_chat()
                    elif cmd == "/thinking":
                        t = scraper._scrape_thinking()
                        print("\n💭 Thinking:\n" + "─" * 40)
                        print(t if t else "(No thinking block found)")
                        print("─" * 40 + "\n")
                    elif cmd == "/history":
                        history = scraper.get_full_conversation()
                        print("\n" + "=" * 60)
                        if not history:
                            print("  No messages found.")
                        for msg in history:
                            icon = "🧑" if msg["role"] == "user" else "🤖"
                            print(f"\n{icon} {msg['role'].upper()}:")
                            print(msg["content"])
                        print("=" * 60 + "\n")
                    elif cmd == "/debug":
                        scraper.debug_dom()
                    elif cmd == "/save":
                        scraper.save_session()
                    elif cmd == "/load":
                        scraper.load_session()
                    elif cmd == "/refresh":
                        scraper.page.goto(
                            "https://chat.z.ai/",
                            wait_until="domcontentloaded",
                            timeout=15000
                        )
                        scraper._wait_for_splash_gone()
                        scraper._wait_for_input()
                        print("[+] Refreshed.")
                    else:
                        print("[-] Unknown command.")
                    continue

                # ── Send ──
                print("🤖 AI: ", end="", flush=True)
                response = scraper.send_message(user_input)
                print("\n" + response + "\n")

            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"\n[-] Error: {e}")

    finally:
        if scraper:
            scraper.close()
        print("[*] Goodbye!")


if __name__ == "__main__":
    main()
