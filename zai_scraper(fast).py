import time
import json
import re
import sys
from playwright.sync_api import sync_playwright
from datetime import datetime

# ── Optional: html2text for HTML→Markdown conversion ──
try:
    import html2text as _html2text_mod
    _H2T = _html2text_mod.HTML2Text()
    _H2T.ignore_links = False
    _H2T.ignore_images = True
    _H2T.body_width = 0
    _H2T.protect_links = True
    _H2T.wrap_links = False
    HAS_HTML2TEXT = True
except ImportError:
    HAS_HTML2TEXT = False


def _apply_inline(text, BOLD, ITALIC, STRIKE, CYAN, DIM, RESET):
    """Apply inline MD formatting (bold, italic, code, strikethrough, links) with ANSI."""
    parts = re.split(r'(`[^`]+`)', text)
    result = []
    for part in parts:
        if part.startswith('`') and part.endswith('`') and len(part) > 1:
            result.append(f'{CYAN}{part[1:-1]}{RESET}')
        else:
            p = part
            p = re.sub(r'\*\*\*(.*?)\*\*\*', lambda m: f'{BOLD}{ITALIC}{m.group(1)}{RESET}', p)
            p = re.sub(r'\*\*(.*?)\*\*',     lambda m: f'{BOLD}{m.group(1)}{RESET}', p)
            p = re.sub(r'\*(.*?)\*',          lambda m: f'{ITALIC}{m.group(1)}{RESET}', p)
            p = re.sub(r'__(.*?)__',          lambda m: f'{BOLD}{m.group(1)}{RESET}', p)
            p = re.sub(r'_(.*?)_',            lambda m: f'{ITALIC}{m.group(1)}{RESET}', p)
            p = re.sub(r'~~(.*?)~~',          lambda m: f'{STRIKE}{m.group(1)}{RESET}', p)
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

        self.INPUT_SELECTOR    = "textarea#chat-input"
        self.SEND_BTN_SELECTOR = "button#send-message-button"
        self.RESPONSE_SELECTOR = "div.markdown-prose"

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
        self.page.goto(
            "https://chat.z.ai/",
            wait_until="domcontentloaded",
            timeout=30000
        )

        self._wait_for_splash_gone()
        self._handle_auth()
        self._handle_captcha()
        self._wait_for_input()

        print("[+] Ready!")
        print("-" * 60)

    def _wait_for_splash_gone(self):
        print("[*] Waiting for SvelteKit to hydrate...")
        try:
            self.page.wait_for_selector("#splash-screen", state="hidden", timeout=20000)
            print("[+] App hydrated.")
        except:
            print("[!] Splash timeout — checking for input...")

    def _handle_auth(self):
        time.sleep(1.5)
        current_url = self.page.url
        if "/auth" in current_url or "/login" in current_url:
            print(f"\n🔐 Login required! URL: {current_url}")
            print("    Please log in inside the browser window.")
            input("    Press Enter after logging in... ")
            try:
                self.page.wait_for_url("**/", timeout=60000)
            except:
                pass
            self._wait_for_splash_gone()

    def _handle_captcha(self):
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
        print("[*] Waiting for chat input box...")
        try:
            self.page.wait_for_selector(self.INPUT_SELECTOR, state="visible", timeout=20000)
            print("[+] Input box ready.")
        except:
            print("[!] Input not found — page may need login")
            print(f"    Current URL: {self.page.url}")

    # ─────────────────────────────────────────────
    # Core: Send Message  ←  THE KEY CHANGE
    # ─────────────────────────────────────────────

    def send_message(self, message: str) -> tuple:
        """
        Send a message and wait for #send-message-button to go DISABLED,
        which signals the response is fully committed to the DOM.
        Works for all modes: plain, thinking, web search, web search + thinking.
        Returns (markdown, html, was_web_search).
        """
        if not self.page:
            return ("[Error] Browser not started.", "", False)

        try:
            # ── 1. Fill & Send ──
            input_box = self.page.locator(self.INPUT_SELECTOR)
            input_box.click()
            input_box.fill(message)
            time.sleep(0.15)
            input_box.press("Enter")

            # ── 2. Wait for button DISABLED → done → scrape immediately ──
            was_web_search = self._wait_for_button_disabled(timeout=180)

            # ── 3. Scrape ──
            md, html = self._scrape_response()
            return (md, html, was_web_search)

        except Exception as e:
            return (f"[Error] {str(e)}", "", False)

    def _button_is_disabled(self) -> bool:
        """Return True if #send-message-button currently exists and is disabled."""
        try:
            btn = self.page.locator(self.SEND_BTN_SELECTOR)
            return btn.count() > 0 and btn.is_disabled()
        except:
            return False

    def _wait_for_button_disabled(self, timeout: int = 180) -> bool:
        """
        Poll until #send-message-button is DISABLED.
        DISABLED = response fully done, DOM committed. Scrape immediately after.
        Passively tracks web search shimmer for display label only.
        Returns was_web_search bool.
        """
        was_web_search = False
        deadline = time.time() + timeout
        print("    ⌛ Waiting for response...", end="", flush=True)

        while time.time() < deadline:
            if not was_web_search and self._is_searching_web():
                was_web_search = True
                print(" 🔍 searching...", end="", flush=True)

            if self._button_is_disabled():
                print(" ✅ done.", flush=True)
                return was_web_search

            time.sleep(0.2)

        print(" ⚠ timeout.", flush=True)
        return was_web_search

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

    # ─────────────────────────────────────────────
    # Scraping — full HTML → Markdown pipeline
    # ─────────────────────────────────────────────

    def _scrape_html(self) -> str:
        """
        Grab raw cleaned innerHTML from the LAST TURN's .markdown-prose blocks only.
        Strips: thinking/reasoning blocks, SVG, citations/timestamps, footnote spans.
        """
        try:
            return self.page.evaluate("""() => {
                const allBlocks = document.querySelectorAll('div.markdown-prose');
                if (!allBlocks || allBlocks.length === 0) return '';

                const last = allBlocks[allBlocks.length - 1];

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
                    return el.parentElement || el;
                }

                const turnContainer = getTurnContainer(last);
                const turnBlocks = Array.from(
                    turnContainer.querySelectorAll('div.markdown-prose')
                );
                const blocks = turnBlocks.length > 0 ? turnBlocks : [last];

                const htmlParts = [];
                const seenHTML = new Set();

                blocks.forEach(block => {
                    const clone = block.cloneNode(true);

                    // Remove thinking / reasoning wrappers
                    [
                        'blockquote', 'details',
                        '[class*="think"]', '[class*="reason"]',
                        '[class*="internal"]', '[data-type="thinking"]',
                    ].forEach(sel => clone.querySelectorAll(sel).forEach(e => e.remove()));

                    clone.querySelectorAll('svg, script, style, noscript').forEach(e => e.remove());

                    clone.querySelectorAll(
                        'sup, cite, [class*="citation"], [class*="footnote"], [class*="timestamp"], [class*="time-ago"]'
                    ).forEach(e => e.remove());

                    clone.querySelectorAll('span').forEach(span => {
                        const t = (span.textContent || '').trim();
                        if (/^\\d+[smhdw]$/.test(t)) { span.remove(); return; }
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
        except:
            return ""

    def _html_to_md(self, html: str) -> str:
        """Convert HTML → clean Markdown via html2text or manual fallback."""
        if not html or not html.strip():
            return ""

        if HAS_HTML2TEXT:
            try:
                return _H2T.handle(html).strip()
            except Exception:
                pass

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
                    self.out = []
                    self.stack = []
                    self.ignore_depth = 0
                    self.pre_depth = 0
                    self.list_stack = []
                    self.in_table = False
                    self.td_buf = []
                    self.header_row = False
                    self.link_text_buf = []
                    self.in_link = False
                    self.cell_buf = []
                    self.in_cell = False

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
                        self.out.append('\n\n' + '#' * int(tag[1]) + ' ')
                    elif tag == 'p':
                        self.out.append('\n\n')
                    elif tag in ('ul', 'ol'):
                        self.list_stack.append([tag, 0])
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
                        self.stack[-1] = ('a', href)
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
                        else:
                            self.in_cell = True
                            self.cell_buf = []
                    elif tag == 'td':
                        self.in_cell = True
                        self.cell_buf = []

                def handle_endtag(self, tag):
                    tag = tag.lower()
                    if not self.stack:
                        return

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
                        link_text = ''.join(self.link_text_buf).strip()
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
                        self.td_buf.append(''.join(self.cell_buf).strip())
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
                        self.out.append(re.sub(r'\s+', ' ', data))

                def handle_entityref(self, name):
                    entities = {'amp':'&','lt':'<','gt':'>','quot':'"',
                                'nbsp':' ','mdash':'—','ndash':'–','hellip':'…',
                                'ldquo':'\u201c','rdquo':'\u201d','lsquo':'\u2018','rsquo':'\u2019'}
                    self.out.append(entities.get(name, f'&{name};'))

                def handle_charref(self, name):
                    try:
                        self.out.append(chr(int(name[1:], 16) if name.startswith('x') else int(name)))
                    except:
                        pass

                def get_md(self):
                    text = ''.join(str(x) for x in self.out)
                    text = re.sub(r'\n{3,}', '\n\n', text)
                    return text.strip()

            parser = _MDParser()
            parser.feed(html)
            return parser.get_md()

        except Exception:
            return re.sub(r'<[^>]+>', '', html).strip()

    def _render_md_terminal(self, md: str) -> str:
        """Render Markdown with ANSI escape codes for terminal display."""
        if not (hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()):
            return md

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
        BG_DARK= '\033[48;5;236m'

        lines = md.split('\n')
        out = []
        in_code = False

        for line in lines:
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

            if re.match(r'^---+$', line.strip()):
                out.append(f'{DIM}{"─" * 60}{RESET}')
                continue

            m = re.match(r'^(#{1,6})\s+(.*)', line)
            if m:
                level = len(m.group(1))
                text  = m.group(2)
                colors = [YELLOW, YELLOW, CYAN, CYAN, MAGENTA, MAGENTA]
                prefix = ['━━ ', '── ', '▸ ', '· ', '  · ', '   · ']
                out.append(f'\n{BOLD}{colors[level-1]}{prefix[level-1]}{text}{RESET}')
                continue

            if line.startswith('> '):
                text = _apply_inline(line[2:], BOLD, ITALIC, STRIKE, CYAN, DIM, RESET)
                out.append(f'{DIM}│{RESET} {ITALIC}{text}{RESET}')
                continue

            m_ul = re.match(r'^(\s*)[•\-\*]\s+(.*)', line)
            m_ol = re.match(r'^(\s*)(\d+)\.\s+(.*)', line)
            if m_ul:
                indent, text = m_ul.group(1), m_ul.group(2)
                text = _apply_inline(text, BOLD, ITALIC, STRIKE, CYAN, DIM, RESET)
                bullet = f'{CYAN}•{RESET}' if not indent else f'{DIM}◦{RESET}'
                out.append(f'{indent}{bullet} {text}')
                continue
            if m_ol:
                indent, num, text = m_ol.group(1), m_ol.group(2), m_ol.group(3)
                text = _apply_inline(text, BOLD, ITALIC, STRIKE, CYAN, DIM, RESET)
                out.append(f'{indent}{CYAN}{num}.{RESET} {text}')
                continue

            if line.startswith('|') and line.endswith('|'):
                cells = [c.strip() for c in line.strip('|').split('|')]
                if all(re.match(r'^-+$', c) for c in cells):
                    out.append(f'{DIM}' + '┼'.join('─' * (len(c)+2) for c in cells) + RESET)
                else:
                    rendered = f'{DIM}│{RESET} ' + f' {DIM}│{RESET} '.join(cells) + f' {DIM}│{RESET}'
                    out.append(rendered)
                continue

            out.append(_apply_inline(line, BOLD, ITALIC, STRIKE, CYAN, DIM, RESET))

        return '\n'.join(out)

    def _scrape_response(self) -> tuple:
        """Returns (markdown, html) tuple."""
        html = self._scrape_html()
        if not html:
            return ("[Error] Empty response", "")
        md = self._html_to_md(html)
        return (md, html)

    def _scrape_thinking(self) -> str:
        """Extract thinking content from blockquote inside last .markdown-prose."""
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
        except Exception as e:
            return f"[Think Error] {str(e)}"

    # ─────────────────────────────────────────────
    # New Chat
    # ─────────────────────────────────────────────

    def new_chat(self):
        try:
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

            print("[*] Navigating to new chat...")
            self.page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=15000)
            self._wait_for_splash_gone()
            self._wait_for_input()
            print("[+] New chat started.")

        except Exception as e:
            print(f"[-] New chat error: {e}")

    # ─────────────────────────────────────────────
    # Conversation History
    # ─────────────────────────────────────────────

    def get_full_conversation(self) -> list:
        try:
            history = self.page.evaluate("""() => {
                const history = [];
                const blocks = document.querySelectorAll('div.markdown-prose');
                blocks.forEach(block => {
                    const clone = block.cloneNode(true);
                    clone.querySelectorAll(
                        'blockquote, details, [class*="think"], [class*="reason"], svg, script, style'
                    ).forEach(el => el.remove());
                    const html = clone.innerHTML.trim();
                    if (html) history.push({ role: 'assistant', content: html });
                });
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
                            history.push({ role: 'user', content: el.innerText?.trim() || '' });
                        });
                        break;
                    }
                }
                return history;
            }""")
            return history or []
        except Exception as e:
            print(f"[-] History error: {e}")
            return []

    # ─────────────────────────────────────────────
    # DOM Debug
    # ─────────────────────────────────────────────

    def debug_dom(self):
        try:
            info = self.page.evaluate("""() => {
                const btn = document.querySelector('button#send-message-button');
                return {
                    url: window.location.href,
                    hasToken: !!localStorage.getItem('token'),
                    markdownProseCount: document.querySelectorAll('div.markdown-prose').length,
                    chatInputExists: !!document.querySelector('textarea#chat-input'),
                    sendBtnExists: !!btn,
                    sendBtnDisabled: btn ? btn.disabled : null,
                    splashVisible: (() => {
                        const s = document.querySelector('#splash-screen');
                        return s ? getComputedStyle(s).display !== 'none' : false;
                    })(),
                    lastMarkdownText: (() => {
                        const blocks = document.querySelectorAll('div.markdown-prose');
                        if (!blocks.length) return 'none';
                        return blocks[blocks.length-1].innerText?.substring(0, 200) || '';
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
            print(f"  #send-message-btn: {'✓' if info.get('sendBtnExists') else '✗'}")
            print(f"  Send btn disabled: {info.get('sendBtnDisabled')}")
            print(f"  .markdown-prose  : {info.get('markdownProseCount')} blocks")
            print(f"  Has thinking     : {'✓' if info.get('hasThinking') else '✗'}")
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
                self.page.evaluate("(k, v) => localStorage.setItem(k, v)", k, v)
            print(f"[+] Session loaded ← {filename}")
            self.page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=15000)
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
    print("   chat.z.ai — Playwright Scraper  (fast edition)")
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
                print()

                if was_web and html:
                    print(f"\n{'─'*60}")
                    print("  🌐 Web Search Response")
                    print(f"{'─'*60}")
                    print(scraper._render_md_terminal(md))
                    print(f"{'─'*60}\n")
                else:
                    print(scraper._render_md_terminal(md) + "\n")

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
