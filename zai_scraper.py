import time
import json
import re
import subprocess
import sys
from playwright.sync_api import sync_playwright
from datetime import datetime

# ── Optional: html2text for HTML→Markdown conversion ──
try:
    import html2text as _html2text_mod
    _H2T = _html2text_mod.HTML2Text()
    _H2T.ignore_links = False
    _H2T.ignore_images = True
    _H2T.body_width = 0          # no line-wrapping
    _H2T.protect_links = True
    _H2T.wrap_links = False
    HAS_HTML2TEXT = True
except ImportError:
    HAS_HTML2TEXT = False


def _apply_inline(text, BOLD, ITALIC, STRIKE, CYAN, DIM, RESET):
    """Apply inline MD formatting (bold, italic, code, strikethrough, links) with ANSI."""
    # Inline code first (prevent nested processing)
    parts = re.split(r'(`[^`]+`)', text)
    result = []
    for part in parts:
        if part.startswith('`') and part.endswith('`') and len(part) > 1:
            result.append(f'{CYAN}{part[1:-1]}{RESET}')
        else:
            p = part
            # Bold+italic ***text***
            p = re.sub(r'\*\*\*(.*?)\*\*\*', lambda m: f'{BOLD}{ITALIC}{m.group(1)}{RESET}', p)
            # Bold **text**
            p = re.sub(r'\*\*(.*?)\*\*',     lambda m: f'{BOLD}{m.group(1)}{RESET}', p)
            # Italic *text*
            p = re.sub(r'\*(.*?)\*',          lambda m: f'{ITALIC}{m.group(1)}{RESET}', p)
            # Bold __text__
            p = re.sub(r'__(.*?)__',          lambda m: f'{BOLD}{m.group(1)}{RESET}', p)
            # Italic _text_
            p = re.sub(r'_(.*?)_',            lambda m: f'{ITALIC}{m.group(1)}{RESET}', p)
            # Strikethrough ~~text~~
            p = re.sub(r'~~(.*?)~~',          lambda m: f'{STRIKE}{m.group(1)}{RESET}', p)
            # Links [text](url)
            p = re.sub(r'\[([^\]]+)\]\(([^)]+)\)',
                       lambda m: f'{BOLD}{m.group(1)}{RESET}{DIM}({m.group(2)}){RESET}', p)
            result.append(p)
    return ''.join(result)


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
        self._last_was_web_search = False

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

    def send_message(self, message: str) -> tuple:
        """
        Returns (markdown, html, was_web_search).
        markdown = MD-converted response text
        html     = raw cleaned HTML
        was_web_search = True if web search was used this turn
        """
        if not self.page:
            return ("[Error] Browser not started.", "", False)

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
            md, html = self._scrape_response()
            return (md, html, self._last_was_web_search)

        except Exception as e:
            return (f"[Error] {str(e)}", "", False)

    # ─────────────────────────────────────────────
    # Wait Helpers
    # ─────────────────────────────────────────────

    def _is_searching_web(self) -> bool:
        """Return True if 'Searching the web' shimmer span is currently visible."""
        try:
            shimmer = self.page.locator('span.text-sm.font-semibold.shimmer')
            for i in range(shimmer.count()):
                el = shimmer.nth(i)
                if el.is_visible():
                    if "searching" in (el.inner_text() or "").lower():
                        return True
        except:
            pass
        return False

    def _is_stop_visible(self) -> bool:
        """Return True if the stop button is currently visible."""
        try:
            stop = self.page.locator(self.STOP_BTN_SELECTOR)
            return stop.count() > 0 and stop.is_visible()
        except:
            return False

    def _wait_for_new_response(self, before_count: int, timeout: int = 30):
        """
        Wait until activity starts after sending a message.
        Accepts either:
          - a new .markdown-prose block appearing, OR
          - the 'Searching the web' shimmer appearing
          - the stop button appearing
        whichever comes first.
        """
        print("    ⌛ Waiting for response...", end="", flush=True)
        start = time.time()

        while time.time() - start < timeout:
            # New prose block appeared
            if self.page.locator(self.RESPONSE_SELECTOR).count() > before_count:
                print(" ✓")
                return True
            # Web search started
            if self._is_searching_web():
                print(" ✓")
                return True
            # Stop button appeared (thinking / generation started)
            if self._is_stop_visible():
                print(" ✓")
                return True
            time.sleep(0.3)

        print(" ⚠ timeout")
        return False

    def _wait_for_stream_complete(self, timeout: int = 180):
        """
        Full wait pipeline — handles plain / thinking / search / search+thinking:

        PHASE 1 — Web search shimmer (optional):
            Poll up to 8 s for 'Searching the web' shimmer to appear.
            If seen, wait for it to fully disappear before proceeding.

        PHASE 2 — Stop button (streaming / thinking):
            After search ends (or from the start if no search), poll up to
            15 s for the stop button to appear — generous because search+thinking
            causes the stop button to reappear AFTER the search phase with a delay.
            Once seen, wait for it to disappear (= generation complete).

        PHASE 3 — HTML stabilization fallback:
            If stop button never appeared, wait for the total innerHTML size of
            ALL markdown-prose blocks to stop changing for 3 consecutive seconds.
            Uses HTML length (not innerText) so structural changes (new tags,
            mindmaps, SVG) are detected too.

        PHASE 4 — Post-complete safety buffer:
            After stop button gone OR text stable, do one final check: wait until
            BOTH stop button is gone AND shimmer is gone AND HTML size unchanged
            for 2 ticks. Catches cases where streaming briefly pauses mid-response.
        """
        start = time.time()
        self._last_was_web_search = False

        def remaining():
            return timeout - (time.time() - start)

        def get_html_size() -> int:
            """Return total character count of all markdown-prose innerHTML."""
            try:
                return self.page.evaluate("""() => {
                    const blocks = document.querySelectorAll('div.markdown-prose');
                    let total = 0;
                    blocks.forEach(b => { total += b.innerHTML.length; });
                    return total;
                }""") or 0
            except:
                return 0

        # ── PHASE 1: Web search shimmer ───────────────────────────────────────
        shimmer_seen = False
        shimmer_deadline = time.time() + 8   # wider window
        while time.time() < shimmer_deadline:
            if self._is_searching_web():
                shimmer_seen = True
                break
            # Also stop early if stop button appears (no search, just thinking)
            if self._is_stop_visible():
                break
            time.sleep(0.25)

        if shimmer_seen:
            self._last_was_web_search = True
            print("    🔍 Searching the web", end="", flush=True)
            while remaining() > 0:
                time.sleep(0.5)
                if not self._is_searching_web():
                    print(" ✓")
                    break
                print(".", end="", flush=True)
            else:
                print(" ⚠ search timeout")
            time.sleep(0.4)   # UI transition gap

        # ── PHASE 2: Stop button ──────────────────────────────────────────────
        # After search, stop button may take several seconds to reappear.
        # For web-search mode we wait up to 15 s for it to appear.
        stop_seen = False
        stop_poll_limit = 15 if shimmer_seen else 5
        stop_deadline = time.time() + stop_poll_limit
        while time.time() < stop_deadline and remaining() > 0:
            if self._is_stop_visible():
                stop_seen = True
                break
            time.sleep(0.2)

        if stop_seen:
            print("    ⏳ Streaming", end="", flush=True)
            # Wait for stop button to disappear — but CONFIRM it stays gone.
            # During web-search streaming the button can vanish briefly mid-stream
            # (network pause / render gap) then reappear. We require it to be
            # continuously absent for `confirm_needed` consecutive ticks before
            # declaring done. If it reappears after going away, reset the counter.
            confirm_needed = 3   # 1.5 s @ 0.5 s/tick — enough to catch blips
            if shimmer_seen:
                confirm_needed = 4  # 2 s for web-search, slightly more conservative

            gone_ticks = 0
            while remaining() > 0:
                time.sleep(0.5)
                if self._is_stop_visible():
                    # Still (or again) streaming — reset confirmation counter
                    gone_ticks = 0
                    print(".", end="", flush=True)
                else:
                    gone_ticks += 1
                    if gone_ticks >= confirm_needed:
                        # Also verify no shimmer restarted (second search round)
                        if not self._is_searching_web():
                            print(" ✓")
                            return
                        else:
                            # New search round started — loop back to Phase 1 logic
                            print(" ↻", end="", flush=True)
                            gone_ticks = 0
                            # Wait for this shimmer to finish too
                            while remaining() > 0:
                                time.sleep(0.5)
                                if not self._is_searching_web():
                                    break
                                print(".", end="", flush=True)
                            # Then wait for stop button cycle again
                            while remaining() > 0:
                                time.sleep(0.5)
                                if self._is_stop_visible():
                                    break
                            gone_ticks = 0  # reset for the new streaming phase
            print(" ⚠ timeout")
            return

        else:
            # ── PHASE 3: HTML size stabilization fallback ─────────────────────
            # Only reached if stop button never appeared at all (very fast response).
            print("    ⏳ Stabilizing", end="", flush=True)
            last_size = 0
            stable_ticks = 0
            needed = 6   # 3 s × 0.5 s ticks

            while remaining() > 0:
                size = get_html_size()
                if size > 0 and size == last_size:
                    stable_ticks += 1
                    if stable_ticks >= needed:
                        if not self._is_stop_visible() and not self._is_searching_web():
                            print(" ✓")
                            return
                        else:
                            stable_ticks = 0
                else:
                    stable_ticks = 0
                    last_size = size
                    print(".", end="", flush=True)
                time.sleep(0.5)
            print(" ⚠ timeout")

    # ─────────────────────────────────────────────
    # Scraping — full HTML → Markdown pipeline
    # ─────────────────────────────────────────────

    def _scrape_html(self) -> str:
        """
        Grab raw cleaned innerHTML from the LAST TURN's .markdown-prose blocks only.
        Scopes to the current turn by walking up to the nearest turn/message container
        and only collecting prose blocks inside it — prevents repeating all prior turns.
        Also strips: thinking, SVG, citations/timestamps, footnote spans.
        """
        try:
            return self.page.evaluate("""() => {
                const allBlocks = document.querySelectorAll('div.markdown-prose');
                if (!allBlocks || allBlocks.length === 0) return '';

                const last = allBlocks[allBlocks.length - 1];

                // ── Find the turn container ───────────────────────────────────
                // Walk up the DOM to find the nearest ancestor that looks like a
                // message/turn wrapper. Common patterns: [data-role], [class*=message],
                // [class*=turn], [class*=assistant], [class*=response].
                // If none found, fall back to the immediate parent.
                function getTurnContainer(el) {
                    const turnPatterns = [
                        e => e.dataset && (e.dataset.role || e.dataset.turn || e.dataset.message),
                        e => /\\b(message|turn|response|assistant|ai-message|chat-message)\\b/i
                                .test(e.className || ''),
                        e => e.getAttribute && e.getAttribute('role') === 'listitem',
                    ];
                    let node = el.parentElement;
                    let steps = 0;
                    while (node && node !== document.body && steps < 10) {
                        for (const test of turnPatterns) {
                            if (test(node)) return node;
                        }
                        node = node.parentElement;
                        steps++;
                    }
                    // Fallback: use grandparent of the last prose block
                    return el.parentElement || el;
                }

                const turnContainer = getTurnContainer(last);

                // ── Collect ALL markdown-prose blocks inside this turn only ───
                const turnBlocks = Array.from(
                    turnContainer.querySelectorAll('div.markdown-prose')
                );

                // If querySelectorAll found nothing (scope too tight), fall back
                // to just the last block.
                const blocks = turnBlocks.length > 0 ? turnBlocks : [last];

                // ── Clean and extract HTML from each block ────────────────────
                const htmlParts = [];
                const seenHTML = new Set();   // exact-dedup within this turn

                blocks.forEach(block => {
                    const clone = block.cloneNode(true);

                    // Remove thinking / reasoning wrappers
                    [
                        'blockquote', 'details',
                        '[class*="think"]', '[class*="reason"]',
                        '[class*="internal"]', '[data-type="thinking"]',
                    ].forEach(sel => clone.querySelectorAll(sel).forEach(e => e.remove()));

                    // Remove purely decorative / non-content nodes
                    clone.querySelectorAll(
                        'svg, script, style, noscript'
                    ).forEach(e => e.remove());

                    // Remove citation timestamp elements:
                    // e.g. <span class="...">1m</span>, <sup>1</sup>, <cite>...
                    // These are tiny inline nodes with numbers/timestamps only.
                    clone.querySelectorAll('sup, cite, [class*="citation"], [class*="footnote"], [class*="timestamp"], [class*="time-ago"]')
                        .forEach(e => e.remove());

                    // Strip timestamp-like text-only spans: spans whose ENTIRE
                    // trimmed text matches a time pattern (1m, 2h, 3d, 1w etc.)
                    clone.querySelectorAll('span').forEach(span => {
                        const t = (span.textContent || '').trim();
                        // Remove if it's a bare timestamp (e.g. "1m", "2h", "3d")
                        if (/^\\d+[smhdw]$/.test(t)) {
                            span.remove();
                            return;
                        }
                        // Unwrap cosmetic spans (no role/aria) — keep their children
                        if (!span.getAttribute('role') && !span.getAttribute('aria-label')) {
                            const parent = span.parentNode;
                            if (parent) {
                                while (span.firstChild) parent.insertBefore(span.firstChild, span);
                                span.remove();
                            }
                        }
                    });

                    const html = clone.innerHTML.trim();
                    if (html && !seenHTML.has(html)) {
                        seenHTML.add(html);
                        htmlParts.push(html);
                    }
                });

                return htmlParts.join('\\n');
            }""") or ""
        except Exception as e:
            return ""

    def _html_to_md(self, html: str) -> str:
        """
        Convert HTML → clean Markdown.
        Uses html2text if available, otherwise falls back to a hand-rolled
        recursive parser that handles:
          Block:   h1-h6, p, div, blockquote, pre/code, ul/ol/li, table/tr/td/th, hr
          Inline:  strong/b, em/i, u, s/del, code, a, br, span
          Ignored: svg, script, style, noscript
        """
        if not html or not html.strip():
            return ""

        if HAS_HTML2TEXT:
            try:
                return _H2T.handle(html).strip()
            except Exception:
                pass  # fall through to manual

        # ── Manual recursive HTML→MD converter ───────────────────────────────
        try:
            from html.parser import HTMLParser

            class _MDParser(HTMLParser):
                BLOCK  = {'h1','h2','h3','h4','h5','h6','p','div','section',
                           'article','header','footer','main','nav','aside',
                           'blockquote','pre','ul','ol','li','table','thead',
                           'tbody','tfoot','tr','hr','br','figure','figcaption'}
                IGNORE = {'svg','script','style','noscript','button','input',
                          'select','textarea','form','head','meta','link',
                          'sup','sub','cite','time','footer',
                          'nav','aside'}
                INLINE_BOLD   = {'strong','b'}
                INLINE_ITALIC = {'em','i'}
                INLINE_CODE   = {'code'}
                INLINE_DEL    = {'s','del','strike'}
                INLINE_UNDER  = {'u'}

                def __init__(self):
                    super().__init__()
                    self.out = []           # output tokens
                    self.stack = []         # open tag stack
                    self.ignore_depth = 0   # depth inside ignored tags
                    self.pre_depth = 0      # depth inside <pre>
                    self.list_stack = []    # ('ul'|'ol', counter)
                    self.in_table = False
                    self.td_buf = []        # cells in current row
                    self.header_row = False
                    self.col_widths = []
                    self.link_text_buf = [] # buffer for <a> inner text
                    self.in_link = False    # inside <a> tag
                    self.cell_buf = []      # buffer for td/th inner text
                    self.in_cell = False    # inside td or th

                def _tag(self):
                    return self.stack[-1] if self.stack else ''

                def handle_starttag(self, tag, attrs):
                    tag = tag.lower()
                    adict = dict(attrs)

                    if self.ignore_depth or tag in self.IGNORE:
                        self.ignore_depth += 1
                        self.stack.append(tag)
                        return

                    self.stack.append(tag)

                    if tag in ('h1','h2','h3','h4','h5','h6'):
                        level = int(tag[1])
                        self.out.append('\n\n' + '#' * level + ' ')
                    elif tag == 'p':
                        self.out.append('\n\n')
                    elif tag in ('ul', 'ol'):
                        counter = 0 if tag == 'ol' else None
                        self.list_stack.append([tag, counter])
                        self.out.append('\n')
                    elif tag == 'li':
                        if self.list_stack:
                            kind, counter = self.list_stack[-1]
                            if kind == 'ol':
                                self.list_stack[-1][1] += 1
                                prefix = f"{self.list_stack[-1][1]}. "
                            else:
                                prefix = '• '
                            indent = '  ' * (len(self.list_stack) - 1)
                            self.out.append(f'\n{indent}{prefix}')
                    elif tag == 'blockquote':
                        self.out.append('\n\n> ')
                    elif tag == 'pre':
                        self.pre_depth += 1
                        self.out.append('\n\n```')
                    elif tag == 'code':
                        if self.pre_depth == 0:
                            # Try to get language class
                            cls = adict.get('class', '')
                            lang = ''
                            for part in cls.split():
                                if part.startswith('language-'):
                                    lang = part[9:]
                            if lang:
                                self.out.append(f'`')
                            else:
                                self.out.append('`')
                    elif tag in self.INLINE_BOLD:
                        self.out.append('**')
                    elif tag in self.INLINE_ITALIC:
                        self.out.append('*')
                    elif tag in self.INLINE_DEL:
                        self.out.append('~~')
                    elif tag in self.INLINE_UNDER:
                        self.out.append('__')
                    elif tag == 'a':
                        href = adict.get('href', '')
                        self.stack[-1] = ('a', href)   # store href for close
                        self.in_link = True
                        self.link_text_buf = []
                    elif tag == 'br':
                        self.out.append('  \n')
                    elif tag == 'hr':
                        self.out.append('\n\n---\n\n')
                    elif tag == 'table':
                        self.in_table = True
                        self.out.append('\n\n')
                    elif tag == 'tr':
                        self.td_buf = []
                    elif tag in ('th', 'thead'):
                        if tag == 'thead':
                            self.header_row = True
                        else:  # th
                            self.in_cell = True
                            self.cell_buf = []
                    elif tag == 'td':
                        self.in_cell = True
                        self.cell_buf = []
                        alt = adict.get('alt', '')
                        src = adict.get('src', '')
                        if alt:
                            self.out.append(f'[image: {alt}]')

                def handle_endtag(self, tag):
                    tag = tag.lower()
                    if not self.stack:
                        return

                    # Pop matching tag (handle mismatches gracefully)
                    for i in range(len(self.stack)-1, -1, -1):
                        if self.stack[i] == tag or (
                            isinstance(self.stack[i], tuple) and self.stack[i][0] == tag
                        ):
                            entry = self.stack.pop(i)
                            break
                    else:
                        return

                    if self.ignore_depth:
                        self.ignore_depth -= 1
                        return

                    if tag in ('h1','h2','h3','h4','h5','h6'):
                        self.out.append('\n')
                    elif tag == 'p':
                        self.out.append('\n')
                    elif tag in ('ul', 'ol'):
                        if self.list_stack:
                            self.list_stack.pop()
                        self.out.append('\n')
                    elif tag == 'li':
                        pass
                    elif tag == 'blockquote':
                        self.out.append('\n\n')
                    elif tag == 'pre':
                        self.pre_depth -= 1
                        self.out.append('\n```\n\n')
                    elif tag == 'code':
                        if self.pre_depth == 0:
                            self.out.append('`')
                    elif tag in self.INLINE_BOLD:
                        self.out.append('**')
                    elif tag in self.INLINE_ITALIC:
                        self.out.append('*')
                    elif tag in self.INLINE_DEL:
                        self.out.append('~~')
                    elif tag in self.INLINE_UNDER:
                        self.out.append('__')
                    elif tag == 'a':
                        href = entry[1] if isinstance(entry, tuple) else ''
                        self.in_link = False
                        # Join buffered link text and strip timestamp noise
                        link_text = ''.join(self.link_text_buf).strip()
                        # Remove leading/trailing timestamp patterns: "1m", "2h", "3d" etc.
                        link_text = re.sub(r'^\d+[smhdw]\s*', '', link_text)
                        link_text = re.sub(r'\s*\d+[smhdw]$', '', link_text)
                        link_text = link_text.strip()
                        if link_text and href:
                            self.out.append(f'[{link_text}]({href})')
                        elif link_text:
                            self.out.append(link_text)
                        elif href:
                            self.out.append(href)
                        self.link_text_buf = []
                    elif tag in ('td', 'th'):
                        self.in_cell = False
                        cell_text = ''.join(self.cell_buf).strip()
                        self.td_buf.append(cell_text)
                        self.cell_buf = []
                    elif tag == 'tr':
                        row = ' | '.join(self.td_buf)
                        self.out.append(f'| {row} |\n')
                        if self.header_row:
                            sep = ' | '.join(['---'] * len(self.td_buf))
                            self.out.append(f'| {sep} |\n')
                            self.header_row = False
                        self.td_buf = []
                    elif tag == 'table':
                        self.in_table = False
                        self.out.append('\n')

                def handle_data(self, data):
                    if self.ignore_depth:
                        return
                    if self.pre_depth:
                        self.out.append(data)
                    elif self.in_link:
                        t = re.sub(r'\s+', ' ', data)
                        if not re.match(r'^\s*\d+[smhdw]\s*$', t):
                            self.link_text_buf.append(t)
                    elif self.in_cell:
                        self.cell_buf.append(re.sub(r'\s+', ' ', data))
                    else:
                        collapsed = re.sub(r'\s+', ' ', data)
                        self.out.append(collapsed)

                def handle_entityref(self, name):
                    entities = {'amp':'&','lt':'<','gt':'>','quot':'"',
                                'nbsp':' ','mdash':'—','ndash':'–','hellip':'…',
                                'ldquo':'"','rdquo':'"','lsquo':''','rsquo':'''}
                    self.out.append(entities.get(name, f'&{name};'))

                def handle_charref(self, name):
                    try:
                        if name.startswith('x'):
                            self.out.append(chr(int(name[1:], 16)))
                        else:
                            self.out.append(chr(int(name)))
                    except:
                        pass

                def get_md(self):
                    text = ''.join(str(x) for x in self.out)
                    # Normalize excessive blank lines
                    text = re.sub(r'\n{3,}', '\n\n', text)
                    return text.strip()

            parser = _MDParser()
            parser.feed(html)
            return parser.get_md()

        except Exception as e:
            # Absolute last resort: strip all tags
            return re.sub(r'<[^>]+>', '', html).strip()

    def _render_md_terminal(self, md: str) -> str:
        """
        Render Markdown with ANSI escape codes for terminal display.
        Handles: h1-h6, bold, italic, strikethrough, inline code,
                 fenced code blocks, blockquotes, ul/ol lists, hr, links.
        Falls back to plain text if terminal doesn't support ANSI.
        """
        import os
        # Detect if terminal supports color
        if not (hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()):
            return md  # plain text for pipes / redirects

        RESET  = '\033[0m'
        BOLD   = '\033[1m'
        DIM    = '\033[2m'
        ITALIC = '\033[3m'
        STRIKE = '\033[9m'
        CYAN   = '\033[36m'
        YELLOW = '\033[33m'
        GREEN  = '\033[32m'
        BLUE   = '\033[34m'
        MAGENTA= '\033[35m'
        BG_DARK= '\033[48;5;236m'  # dark grey bg for code blocks

        lines = md.split('\n')
        out = []
        in_code = False
        code_lang = ''

        for line in lines:
            # ── Fenced code block ──
            if line.startswith('```'):
                if not in_code:
                    in_code = True
                    code_lang = line[3:].strip()
                    label = f' {code_lang} ' if code_lang else ' code '
                    out.append(f'{BG_DARK}{DIM}{label}{RESET}')
                else:
                    in_code = False
                    out.append(f'{BG_DARK}{DIM} ─── {RESET}')
                continue

            if in_code:
                out.append(f'{BG_DARK}{GREEN}{line}{RESET}')
                continue

            # ── Horizontal rule ──
            if re.match(r'^---+$', line.strip()):
                out.append(f'{DIM}{"─" * 60}{RESET}')
                continue

            # ── Headings ──
            m = re.match(r'^(#{1,6})\s+(.*)', line)
            if m:
                level = len(m.group(1))
                text  = m.group(2)
                colors = [YELLOW, YELLOW, CYAN, CYAN, MAGENTA, MAGENTA]
                prefix = ['━━ ', '── ', '▸ ', '· ', '  · ', '   · ']
                c = colors[level-1]
                p = prefix[level-1]
                out.append(f'\n{BOLD}{c}{p}{text}{RESET}')
                continue

            # ── Blockquote ──
            if line.startswith('> '):
                text = line[2:]
                text = _apply_inline(text, BOLD, ITALIC, STRIKE, CYAN, DIM, RESET)
                out.append(f'{DIM}│{RESET} {ITALIC}{text}{RESET}')
                continue

            # ── Lists ──
            m_ul = re.match(r'^(\s*)[•\-\*]\s+(.*)', line)
            m_ol = re.match(r'^(\s*)(\d+)\.\s+(.*)', line)
            if m_ul:
                indent = m_ul.group(1)
                text   = m_ul.group(2)
                text   = _apply_inline(text, BOLD, ITALIC, STRIKE, CYAN, DIM, RESET)
                bullet = f'{CYAN}•{RESET}' if len(indent) == 0 else f'{DIM}◦{RESET}'
                out.append(f'{indent}{bullet} {text}')
                continue
            if m_ol:
                indent = m_ol.group(1)
                num    = m_ol.group(2)
                text   = m_ol.group(3)
                text   = _apply_inline(text, BOLD, ITALIC, STRIKE, CYAN, DIM, RESET)
                out.append(f'{indent}{CYAN}{num}.{RESET} {text}')
                continue

            # ── Table row ──
            if line.startswith('|') and line.endswith('|'):
                cells = [c.strip() for c in line.strip('|').split('|')]
                if all(re.match(r'^-+$', c) for c in cells):
                    # separator row
                    out.append(f'{DIM}' + '┼'.join('─' * (len(c)+2) for c in cells) + RESET)
                else:
                    rendered = f'{DIM}│{RESET} ' + f' {DIM}│{RESET} '.join(
                        f'{BOLD}{c}{RESET}' if out and '─' not in out[-1] else c
                        for c in cells
                    ) + f' {DIM}│{RESET}'
                    out.append(rendered)
                continue

            # ── Regular paragraph line ──
            line = _apply_inline(line, BOLD, ITALIC, STRIKE, CYAN, DIM, RESET)
            out.append(line)

        return '\n'.join(out)

    def _scrape_response(self) -> tuple[str, str]:
        """
        Returns (markdown, html) tuple.
        markdown = human-readable MD for terminal
        html     = raw cleaned HTML (for web-search mode display)
        """
        html = self._scrape_html()
        if not html:
            return ("[Error] Empty response", "")
        md = self._html_to_md(html)
        return (md, html)

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
                        'blockquote, details, [class*="think"], [class*="reason"], svg, script, style'
                    ).forEach(el => el.remove());

                    const html = clone.innerHTML.trim();
                    if (html) history.push({ role: 'assistant', content: html });
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
                    lastTurnSiblingBlocks: (() => {
                        const all = document.querySelectorAll('div.markdown-prose');
                        if (!all.length) return 0;
                        let count = 1;
                        let sib = all[all.length - 1].previousElementSibling;
                        while (sib && sib.classList.contains('markdown-prose')) {
                            count++;
                            sib = sib.previousElementSibling;
                        }
                        return count;
                    })(),
                    hasThinking: !!document.querySelector('div.markdown-prose blockquote'),
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
            print(f"  Last turn blocks : {info.get('lastTurnSiblingBlocks')} (web-search may have multiple)")
            print(f"  Has thinking     : {'✓' if info.get('hasThinking') else '✗'}")
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
                            if msg["role"] == "assistant":
                                md = scraper._html_to_md(msg["content"])
                                print(scraper._render_md_terminal(md))
                            else:
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
                md, html, was_web = scraper.send_message(user_input)
                print()  # newline after wait indicators

                if was_web and html:
                    # Web search: show full rich MD (converted from complete HTML)
                    label = "🌐 Web Search Response"
                    print(f"\n{'─'*60}")
                    print(f"  {label}")
                    print(f"{'─'*60}")
                    rendered = scraper._render_md_terminal(md)
                    print(rendered)
                    print(f"{'─'*60}\n")
                else:
                    # Plain / thinking mode: ANSI-rendered markdown
                    rendered = scraper._render_md_terminal(md)
                    print(rendered + "\n")

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
