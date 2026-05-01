"""
╔══════════════════════════════════════════════════════════════╗
║              chat.z.ai  —  Playwright Scraper                ║
║              Modern Edition  ·  Powered by Rich              ║
╚══════════════════════════════════════════════════════════════╝
"""

import time
import json
import re
import sys
from datetime import datetime
from playwright.sync_api import sync_playwright

# ── Rich imports ──────────────────────────────────────────────
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.rule import Rule
from rich import box
from rich.theme import Theme
from rich.live import Live
from rich.spinner import Spinner
from rich.align import Align
from rich.padding import Padding

# ── Optional html2text ────────────────────────────────────────
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

# ── Theme ─────────────────────────────────────────────────────
THEME = Theme({
    "primary":     "bold #7C3AED",
    "secondary":   "#A78BFA",
    "accent":      "bold #06B6D4",
    "success":     "bold #10B981",
    "warning":     "bold #F59E0B",
    "error":       "bold #EF4444",
    "muted":       "#6B7280",
    "user_label":  "bold #F472B6",
    "ai_label":    "bold #818CF8",
    "web_label":   "bold #34D399",
    "think_label": "bold #FCD34D",
    "cmd":         "#94A3B8",
    "info":        "#60A5FA",
})

console = Console(theme=THEME, highlight=False)


# ─────────────────────────────────────────────────────────────
# UI Helpers
# ─────────────────────────────────────────────────────────────

def print_banner():
    lines = [
        ("  ╔═══════════════════════════════════════╗\n", "primary"),
        ("  ║  ", "primary"),
        ("⚡ chat.z.ai  ", "bold #A78BFA"),
        ("Playwright Scraper  ", "#A78BFA"),
        ("║\n", "primary"),
        ("  ║  ", "primary"),
        ("     Modern Edition · Rich UI           ", "muted"),
        ("║\n", "primary"),
        ("  ╚═══════════════════════════════════════╝", "primary"),
    ]
    t = Text()
    for s, style in lines:
        t.append(s, style=style)
    console.print()
    console.print(t)
    console.print()


def print_help():
    table = Table(
        box=box.ROUNDED,
        border_style="#7C3AED",
        show_header=True,
        header_style="bold #06B6D4",  # literal style, not theme alias
        padding=(0, 2),
    )
    table.add_column("Command", style="bold #06B6D4", no_wrap=True)
    table.add_column("Description", style="#94A3B8")
    for cmd, desc in [
        ("/new",      "Start a new conversation"),
        ("/thinking", "Show last reasoning / thinking block"),
        ("/history",  "Display full conversation history"),
        ("/debug",    "Inspect DOM state"),
        ("/save",     "Save browser session to disk"),
        ("/load",     "Load browser session from disk"),
        ("/refresh",  "Reload the page"),
        ("/quit",     "Exit"),
    ]:
        table.add_row(cmd, desc)
    console.print(Panel(table, title="[primary]Commands[/primary]",
                        border_style="#7C3AED", padding=(1, 2)))
    console.print()


def spinner_ctx(message: str, style: str = "#A78BFA"):
    """Return a Rich Live context with an animated spinner."""
    renderable = Align.left(Spinner("dots2", text=Text(f"  {message}", style=style)))
    return Live(renderable, console=console, refresh_per_second=12, transient=True)


def render_response(md_text: str, was_web: bool, has_thinking: bool, elapsed: float):
    tags = []
    if was_web:       tags.append("[web_label]🌐 Web Search[/web_label]")
    if has_thinking:  tags.append("[think_label]💭 Thinking[/think_label]")
    if not tags:      tags.append("[ai_label]✦ Response[/ai_label]")
    title = "  ".join(tags) + f"  [muted]({elapsed:.1f}s)[/muted]"

    console.print(Panel(
        Padding(Markdown(md_text, code_theme="monokai", hyperlinks=True), (1, 2)),
        title=title, title_align="left",
        border_style="web_label" if was_web else "secondary",
        box=box.ROUNDED, padding=(0, 1),
    ))
    console.print()


def render_thinking(thinking_text: str):
    console.print(Panel(
        Padding(Text(thinking_text, style="muted italic"), (1, 2)),
        title="[think_label]💭 Reasoning[/think_label]", title_align="left",
        border_style="think_label", box=box.ROUNDED,
    ))
    console.print()


# ─────────────────────────────────────────────────────────────
# Scraper
# ─────────────────────────────────────────────────────────────

class ZAIScraper:
    def __init__(self, headless: bool = False):
        self.headless   = headless
        self.browser    = None
        self.context    = None
        self.page       = None
        self.playwright = None

        self.INPUT_SELECTOR    = "textarea#chat-input"
        self.SEND_BTN_SELECTOR = "button#send-message-button"
        self.RESPONSE_SELECTOR = "div.markdown-prose"

    # ── Browser Setup ─────────────────────────────────────────

    def start(self):
        with spinner_ctx("Launching Chromium…"):
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-dev-shm-usage"],
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
        console.print("[success]✔[/success]  Browser launched")

        with spinner_ctx("Loading chat.z.ai…"):
            self.page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=30000)
        console.print("[success]✔[/success]  Page loaded")

        with spinner_ctx("Hydrating app…"):
            self._wait_for_splash_gone()
        console.print("[success]✔[/success]  App ready")

        self._handle_auth()
        self._handle_captcha()

        with spinner_ctx("Waiting for input box…"):
            self._wait_for_input()
        console.print("[success]✔[/success]  Chat input ready\n")

    def _wait_for_splash_gone(self):
        try:
            self.page.wait_for_selector("#splash-screen", state="hidden", timeout=20000)
        except:
            pass

    def _handle_auth(self):
        time.sleep(1.5)
        url = self.page.url
        if "/auth" in url or "/login" in url:
            console.print(Panel(
                f"[warning]Login required[/warning]\n[muted]{url}[/muted]\n\n"
                "Log in inside the browser window, then press [accent]Enter[/accent].",
                title="[warning]🔐 Authentication[/warning]",
                border_style="warning", box=box.ROUNDED,
            ))
            input()
            try:
                self.page.wait_for_url("**/", timeout=60000)
            except:
                pass
            self._wait_for_splash_gone()

    def _handle_captcha(self):
        for sig in ["iframe[src*='recaptcha']", "iframe[src*='captcha']",
                    ".g-recaptcha", "text=Verify you are human", "text=Security Check"]:
            try:
                if self.page.locator(sig).count() > 0:
                    console.print(Panel(
                        "[warning]CAPTCHA detected.[/warning]\n"
                        "Solve it in the browser, then press [accent]Enter[/accent].",
                        title="[warning]⚠  CAPTCHA[/warning]",
                        border_style="warning", box=box.ROUNDED,
                    ))
                    input()
                    return
            except:
                pass

    def _wait_for_input(self):
        try:
            self.page.wait_for_selector(self.INPUT_SELECTOR, state="visible", timeout=20000)
        except:
            console.print("[error]✘  Input not found — page may need login[/error]")
            console.print(f"[muted]   {self.page.url}[/muted]")

    # ── Core: Send Message ────────────────────────────────────

    def send_message(self, message: str) -> tuple:
        if not self.page:
            return ("[Error] Browser not started.", "", False, 0.0)
        try:
            box_ = self.page.locator(self.INPUT_SELECTOR)
            box_.click()
            box_.fill(message)
            time.sleep(0.15)
            box_.press("Enter")

            t0 = time.time()
            was_web = self._wait_for_button_disabled(timeout=180)
            elapsed = time.time() - t0

            md, html = self._scrape_response()
            return (md, html, was_web, elapsed)
        except Exception as e:
            return (f"[Error] {e}", "", False, 0.0)

    # ── Wait Logic ────────────────────────────────────────────

    def _button_is_disabled(self) -> bool:
        try:
            btn = self.page.locator(self.SEND_BTN_SELECTOR)
            return btn.count() > 0 and btn.is_disabled()
        except:
            return False

    def _is_searching_web(self) -> bool:
        try:
            s = self.page.locator("span.text-sm.font-semibold.shimmer")
            for i in range(s.count()):
                el = s.nth(i)
                if el.is_visible() and "searching" in (el.inner_text() or "").lower():
                    return True
        except:
            pass
        return False

    def _wait_for_button_disabled(self, timeout: int = 180) -> bool:
        was_web  = False
        deadline = time.time() + timeout

        def _spin(label, style="#A78BFA"):
            return Align.left(Spinner("dots2", text=Text(f"  {label}", style=style)))

        with Live(_spin("Generating response…"), console=console,
                  refresh_per_second=12, transient=True) as live:
            while time.time() < deadline:
                if not was_web and self._is_searching_web():
                    was_web = True
                    live.update(_spin("Searching the web…", "#34D399"))

                if self._button_is_disabled():
                    live.update(Text(""))
                    return was_web

                time.sleep(0.2)

        console.print("[warning]⚠  Response timed out.[/warning]")
        return was_web

    # ── Scraping ──────────────────────────────────────────────

    def _scrape_html(self) -> str:
        try:
            return self.page.evaluate("""() => {
                const allBlocks = document.querySelectorAll('div.markdown-prose');
                if (!allBlocks || allBlocks.length === 0) return '';
                const last = allBlocks[allBlocks.length - 1];

                function getTurnContainer(el) {
                    const patterns = [
                        e => e.dataset && (e.dataset.role || e.dataset.turn || e.dataset.message),
                        e => /\\b(message|turn|response|assistant|ai-message|chat-message)\\b/i.test(e.className || ''),
                        e => e.getAttribute && e.getAttribute('role') === 'listitem',
                    ];
                    let node = el.parentElement, steps = 0;
                    while (node && node !== document.body && steps < 10) {
                        for (const t of patterns) { if (t(node)) return node; }
                        node = node.parentElement; steps++;
                    }
                    return el.parentElement || el;
                }

                const container  = getTurnContainer(last);
                const turnBlocks = Array.from(container.querySelectorAll('div.markdown-prose'));
                const blocks     = turnBlocks.length > 0 ? turnBlocks : [last];
                const htmlParts  = [], seen = new Set();

                blocks.forEach(block => {
                    const clone = block.cloneNode(true);
                    ['blockquote','details','[class*="think"]','[class*="reason"]',
                     '[class*="internal"]','[data-type="thinking"]']
                        .forEach(s => clone.querySelectorAll(s).forEach(e => e.remove()));
                    clone.querySelectorAll('svg,script,style,noscript').forEach(e => e.remove());
                    clone.querySelectorAll(
                        'sup,cite,[class*="citation"],[class*="footnote"],[class*="timestamp"],[class*="time-ago"]'
                    ).forEach(e => e.remove());
                    clone.querySelectorAll('span').forEach(span => {
                        const t = (span.textContent || '').trim();
                        if (/^\\d+[smhdw]$/.test(t)) { span.remove(); return; }
                        if (!span.getAttribute('role') && !span.getAttribute('aria-label')) {
                            const p = span.parentNode;
                            if (p) { while (span.firstChild) p.insertBefore(span.firstChild, span); span.remove(); }
                        }
                    });
                    const html = clone.innerHTML.trim();
                    if (html && !seen.has(html)) { seen.add(html); htmlParts.push(html); }
                });

                return htmlParts.join('\\n');
            }""") or ""
        except:
            return ""

    def _html_to_md(self, html: str) -> str:
        if not html or not html.strip():
            return ""
        if HAS_HTML2TEXT:
            try:
                return _H2T.handle(html).strip()
            except:
                pass

        try:
            from html.parser import HTMLParser

            class _MDParser(HTMLParser):
                IGNORE = {'svg','script','style','noscript','button','input',
                          'select','textarea','form','head','meta','link',
                          'sup','sub','cite','time','footer','nav','aside'}
                BOLD={'strong','b'}; ITALIC={'em','i'}; CODE={'code'}
                DEL={'s','del','strike'}; UNDER={'u'}

                def __init__(self):
                    super().__init__()
                    self.out=[];self.stack=[];self.ignore_depth=0;self.pre_depth=0
                    self.list_stack=[];self.in_table=False;self.td_buf=[]
                    self.header_row=False;self.link_buf=[];self.in_link=False
                    self.cell_buf=[];self.in_cell=False

                def handle_starttag(self, tag, attrs):
                    tag=tag.lower(); adict=dict(attrs)
                    if self.ignore_depth or tag in self.IGNORE:
                        self.ignore_depth+=1; self.stack.append(tag); return
                    self.stack.append(tag)
                    if tag in ('h1','h2','h3','h4','h5','h6'):
                        self.out.append('\n\n'+'#'*int(tag[1])+' ')
                    elif tag=='p': self.out.append('\n\n')
                    elif tag in ('ul','ol'): self.list_stack.append([tag,0]); self.out.append('\n')
                    elif tag=='li':
                        if self.list_stack:
                            k,c=self.list_stack[-1]
                            if k=='ol': self.list_stack[-1][1]+=1; p=f"{self.list_stack[-1][1]}. "
                            else: p='• '
                            self.out.append(f'\n{"  "*(len(self.list_stack)-1)}{p}')
                    elif tag=='blockquote': self.out.append('\n\n> ')
                    elif tag=='pre': self.pre_depth+=1; self.out.append('\n\n```')
                    elif tag=='code':
                        if self.pre_depth==0: self.out.append('`')
                    elif tag in self.BOLD: self.out.append('**')
                    elif tag in self.ITALIC: self.out.append('*')
                    elif tag in self.DEL: self.out.append('~~')
                    elif tag in self.UNDER: self.out.append('__')
                    elif tag=='a':
                        self.stack[-1]=('a',adict.get('href','')); self.in_link=True; self.link_buf=[]
                    elif tag=='br': self.out.append('  \n')
                    elif tag=='hr': self.out.append('\n\n---\n\n')
                    elif tag=='table': self.in_table=True; self.out.append('\n\n')
                    elif tag=='tr': self.td_buf=[]
                    elif tag in ('th','thead'):
                        if tag=='thead': self.header_row=True
                        else: self.in_cell=True; self.cell_buf=[]
                    elif tag=='td': self.in_cell=True; self.cell_buf=[]

                def handle_endtag(self, tag):
                    tag=tag.lower()
                    if not self.stack: return
                    for i in range(len(self.stack)-1,-1,-1):
                        if self.stack[i]==tag or (isinstance(self.stack[i],tuple) and self.stack[i][0]==tag):
                            entry=self.stack.pop(i); break
                    else: return
                    if self.ignore_depth: self.ignore_depth-=1; return
                    if tag in ('h1','h2','h3','h4','h5','h6'): self.out.append('\n')
                    elif tag=='p': self.out.append('\n')
                    elif tag in ('ul','ol'):
                        if self.list_stack: self.list_stack.pop()
                        self.out.append('\n')
                    elif tag=='blockquote': self.out.append('\n\n')
                    elif tag=='pre': self.pre_depth-=1; self.out.append('\n```\n\n')
                    elif tag=='code':
                        if self.pre_depth==0: self.out.append('`')
                    elif tag in self.BOLD: self.out.append('**')
                    elif tag in self.ITALIC: self.out.append('*')
                    elif tag in self.DEL: self.out.append('~~')
                    elif tag in self.UNDER: self.out.append('__')
                    elif tag=='a':
                        href=entry[1] if isinstance(entry,tuple) else ''
                        self.in_link=False
                        lt=''.join(self.link_buf).strip()
                        lt=re.sub(r'^\d+[smhdw]\s*','',lt).strip()
                        lt=re.sub(r'\s*\d+[smhdw]$','',lt).strip()
                        if lt and href: self.out.append(f'[{lt}]({href})')
                        elif lt: self.out.append(lt)
                        elif href: self.out.append(href)
                        self.link_buf=[]
                    elif tag in ('td','th'):
                        self.in_cell=False; self.td_buf.append(''.join(self.cell_buf).strip()); self.cell_buf=[]
                    elif tag=='tr':
                        self.out.append(f'| {" | ".join(self.td_buf)} |\n')
                        if self.header_row:
                            self.out.append(f'| {" | ".join(["---"]*len(self.td_buf))} |\n')
                            self.header_row=False
                        self.td_buf=[]
                    elif tag=='table': self.in_table=False; self.out.append('\n')

                def handle_data(self, data):
                    if self.ignore_depth: return
                    if self.pre_depth: self.out.append(data)
                    elif self.in_link:
                        t=re.sub(r'\s+',' ',data)
                        if not re.match(r'^\s*\d+[smhdw]\s*$',t): self.link_buf.append(t)
                    elif self.in_cell: self.cell_buf.append(re.sub(r'\s+',' ',data))
                    else: self.out.append(re.sub(r'\s+',' ',data))

                def handle_entityref(self, name):
                    e={'amp':'&','lt':'<','gt':'>','quot':'"','nbsp':' ',
                       'mdash':'—','ndash':'–','hellip':'…',
                       'ldquo':'\u201c','rdquo':'\u201d','lsquo':'\u2018','rsquo':'\u2019'}
                    self.out.append(e.get(name, f'&{name};'))

                def handle_charref(self, name):
                    try: self.out.append(chr(int(name[1:],16) if name.startswith('x') else int(name)))
                    except: pass

                def get_md(self):
                    return re.sub(r'\n{3,}','\n\n',''.join(str(x) for x in self.out)).strip()

            p = _MDParser(); p.feed(html); return p.get_md()
        except:
            return re.sub(r'<[^>]+>', '', html).strip()

    def _scrape_response(self) -> tuple:
        html = self._scrape_html()
        if not html:
            return ("[Error] Empty response", "")
        return (self._html_to_md(html), html)

    def _scrape_thinking(self) -> str:
        try:
            r = self.page.evaluate("""() => {
                const blocks = document.querySelectorAll('div.markdown-prose');
                if (!blocks.length) return '';
                const last = blocks[blocks.length-1];
                const bq = last.querySelector('blockquote');
                if (bq) return bq.innerText?.trim() || '';
                const d = last.querySelector('details');
                if (d) return d.innerText?.trim() || '';
                return '';
            }""")
            return r.strip() if r else ""
        except Exception as e:
            return f"[Think Error] {e}"

    # ── New Chat ──────────────────────────────────────────────

    def new_chat(self):
        for sel in ["button[aria-label*='new' i]", "button[aria-label*='New' i]",
                    "button:has-text('New Chat')", "button:has-text('New chat')",
                    "[class*='new-chat']", "[data-testid*='new']", "a[href='/']"]:
            try:
                btn = self.page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(); time.sleep(1); self._wait_for_input()
                    console.print("[success]✔[/success]  New conversation started.")
                    return
            except:
                continue
        with spinner_ctx("Starting new chat…"):
            self.page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=15000)
            self._wait_for_splash_gone(); self._wait_for_input()
        console.print("[success]✔[/success]  New conversation started.")

    # ── Conversation History ──────────────────────────────────

    def get_full_conversation(self) -> list:
        try:
            history = self.page.evaluate("""() => {
                const history = [];
                document.querySelectorAll('div.markdown-prose').forEach(block => {
                    const clone = block.cloneNode(true);
                    clone.querySelectorAll(
                        'blockquote,details,[class*="think"],[class*="reason"],svg,script,style'
                    ).forEach(el => el.remove());
                    const html = clone.innerHTML.trim();
                    if (html) history.push({ role: 'assistant', content: html });
                });
                for (const sel of ['[data-role="user"]','[class*="user-message"]',
                                    '[class*="human-message"]','[class*="user-bubble"]']) {
                    const msgs = document.querySelectorAll(sel);
                    if (msgs.length > 0) {
                        msgs.forEach(el => history.push({
                            role: 'user', content: el.innerText?.trim() || ''
                        }));
                        break;
                    }
                }
                return history;
            }""")
            return history or []
        except Exception as e:
            console.print(f"[error]History error: {e}[/error]")
            return []

    # ── DOM Debug ─────────────────────────────────────────────

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

            t = Table(
                title="[primary]DOM State[/primary]",
                box=box.ROUNDED,
                border_style="#7C3AED",
                show_header=False,
                padding=(0, 2),
            )
            t.add_column("Key",   style="bold #06B6D4", no_wrap=True)
            t.add_column("Value", style="#A78BFA")

            def tick(v): return "[success]✔  yes[/success]" if v else "[error]✘  no[/error]"
            t.add_row("URL",                f"[muted]{info.get('url')}[/muted]")
            t.add_row("Auth token",          tick(info.get('hasToken')))
            t.add_row("Splash visible",      tick(info.get('splashVisible')))
            t.add_row("#chat-input",         tick(info.get('chatInputExists')))
            t.add_row("#send-message-btn",   tick(info.get('sendBtnExists')))
            t.add_row("Send btn disabled",   str(info.get('sendBtnDisabled')))
            t.add_row(".markdown-prose",     str(info.get('markdownProseCount')) + " blocks")
            t.add_row("Has thinking",        tick(info.get('hasThinking')))
            t.add_row("Last 80 chars",       f"[muted]{info.get('lastMarkdownText','')[:80]}…[/muted]")

            console.print(); console.print(t); console.print()
        except Exception as e:
            console.print(f"[error]Debug error: {e}[/error]")

    # ── Session Management ────────────────────────────────────

    def save_session(self, filename: str = "zai_session.json"):
        try:
            data = {
                "cookies":      self.context.cookies(),
                "localStorage": json.loads(self.page.evaluate("() => JSON.stringify(localStorage)")),
                "saved_at":     datetime.now().isoformat(),
            }
            with open(filename, "w") as f:
                json.dump(data, f, indent=2)
            console.print(f"[success]✔[/success]  Saved → [accent]{filename}[/accent]")
        except Exception as e:
            console.print(f"[error]Save error: {e}[/error]")

    def load_session(self, filename: str = "zai_session.json"):
        try:
            with open(filename) as f:
                data = json.load(f)
            self.context.add_cookies(data["cookies"])
            for k, v in data.get("localStorage", {}).items():
                self.page.evaluate("(k, v) => localStorage.setItem(k, v)", k, v)
            console.print(f"[success]✔[/success]  Loaded ← [accent]{filename}[/accent]")
            with spinner_ctx("Reloading…"):
                self.page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=15000)
                self._wait_for_splash_gone(); self._wait_for_input()
        except FileNotFoundError:
            console.print("[error]No session file found.[/error]")
        except Exception as e:
            console.print(f"[error]Load error: {e}[/error]")

    def close(self):
        try:
            if self.browser:    self.browser.close()
            if self.playwright: self.playwright.stop()
        except:
            pass


# ─────────────────────────────────────────────────────────────
# Main CLI
# ─────────────────────────────────────────────────────────────

def main():
    scraper = None
    print_banner()

    try:
        scraper = ZAIScraper(headless=False)
        scraper.start()
        print_help()

        while True:
            try:
                console.print(Rule(style="muted"))
                user_input = console.input("[user_label]  You  [/user_label][muted] › [/muted]").strip()

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
                        render_thinking(t) if t else console.print("[muted]  (No thinking block found)[/muted]\n")
                    elif cmd == "/history":
                        history = scraper.get_full_conversation()
                        if not history:
                            console.print("[muted]  No messages found.[/muted]\n")
                        for msg in history:
                            if msg["role"] == "assistant":
                                md = scraper._html_to_md(msg["content"])
                                console.print(Panel(
                                    Padding(Markdown(md, code_theme="monokai"), (1, 2)),
                                    title="[ai_label]✦ Assistant[/ai_label]",
                                    border_style="secondary", box=box.ROUNDED,
                                ))
                            else:
                                console.print(Panel(
                                    Padding(Text(msg["content"]), (0, 2)),
                                    title="[user_label]  You  [/user_label]",
                                    border_style="user_label", box=box.ROUNDED,
                                ))
                            console.print()
                    elif cmd == "/debug":
                        scraper.debug_dom()
                    elif cmd == "/save":
                        scraper.save_session()
                    elif cmd == "/load":
                        scraper.load_session()
                    elif cmd == "/refresh":
                        with spinner_ctx("Refreshing…"):
                            scraper.page.goto("https://chat.z.ai/",
                                              wait_until="domcontentloaded", timeout=15000)
                            scraper._wait_for_splash_gone()
                            scraper._wait_for_input()
                        console.print("[success]✔[/success]  Refreshed.")
                    else:
                        console.print(f"[error]Unknown command:[/error] [cmd]{cmd}[/cmd]  "
                                      "[muted]— type /help to see commands[/muted]")
                    continue

                # ── Send & render ──
                console.print()
                md, html, was_web, elapsed = scraper.send_message(user_input)
                has_thinking = bool(scraper._scrape_thinking())
                render_response(md, was_web, has_thinking, elapsed)

            except KeyboardInterrupt:
                console.print("\n[muted]  Interrupted.[/muted]")
                break
            except Exception as e:
                console.print(f"[error]Error: {e}[/error]")

    finally:
        if scraper:
            scraper.close()
        console.print()
        console.print(Panel(
            Align.center(Text("Session ended  ·  Goodbye!", style="muted")),
            border_style="#7C3AED", box=box.ROUNDED,
        ))


if __name__ == "__main__":
    main()
