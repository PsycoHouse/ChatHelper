# chrome_chat_agent.py
# -----------------------------------------------------------------------------
# Windows Chrome-Agent mit Playwright/CDP
# - Beendet Chrome, startet neu mit --remote-debugging-port
# - GUI (Tk) mit zwei Always-on-Top-Fenstern: Persona + Agent-Chat (+History)
# - Chat-Modus (keine Navigation) + Domain-Lock
# - Generische Auto-Zielsuche für Chat-Composer (vermeidet Suchleisten)
# - Auto-Senden je nach Seite
# - Optional: OpenAI-LLM-Planner (sonst Stub)
# - Optional: Auto-Responder (Hintergrund-Task, im GUI einschaltbar)
# -----------------------------------------------------------------------------

import os, asyncio, json, threading, subprocess, time, queue, random
from urllib.parse import urlparse
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from playwright.async_api import async_playwright

# Optional: OpenAI-Client (SDK v1)
HAVE_OPENAI = False
try:
    from openai import OpenAI
    HAVE_OPENAI = True
except Exception:
    HAVE_OPENAI = False

console = Console()

# -------------------- Konfiguration ------------------------------------------
ASK_CONFIRM      = False  # keine Rückfragen vor Aktionen
AUTO_SEND        = True   # nach Tippen automatisch senden (domain-spezifisch)
FORCE_CHAT_MODE  = True   # nur Chat-Befehle zulassen (keine Navigation)
LOCK_TO_DOMAIN   = True   # Tab/Domain sperren – kein Wechsel erlaubt

# Auto-Responder
AUTO_MODE             = False   # Startzustand; im GUI umschaltbar
AUTO_POLL_SECONDS     = 3
AUTO_MIN_REPLY_DELAY  = 0.4
AUTO_MAX_REPLY_DELAY  = 1.4
AUTO_DOMAINS          = {
    "web.whatsapp.com": True,
    "bumble.com": True,
}

_DEBUG_PORT = 9222
_LOCK_HOST  = ""  # wird in connect_chrome gesetzt

_last_sent_text: str = ""
_last_sent_time: float = 0.0
_ECHO_COOLDOWN_SEC = 10.0  # innerhalb dieses Fensters niemals auf eigenen Text antworten

# -------------------- Transparenz-Hinweis ------------------------------------
console.print("[bold red]⚠ Achtung:[/bold red] Diese Sitzung wird von einer [bold]AI[/bold] unterstützt.")
console.print("Alle Aktionen im Chrome-Browser laufen über dieses Agent-Skript. Bitte bestätige riskante Schritte bewusst.\n")

# -------------------- API-Key laden ------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if HAVE_OPENAI and not OPENAI_API_KEY:
    console.print("[yellow]Hinweis:[/yellow] Kein OPENAI_API_KEY gefunden → nutze lokalen Stub.\n")
elif not HAVE_OPENAI:
    console.print("[yellow]Hinweis:[/yellow] Paket 'openai' nicht installiert → nutze lokalen Stub.\n")

# -------------------- Chrome-Start mit Debug-Port -----------------------------
def start_chrome_with_debug_port(port: int = _DEBUG_PORT):
    console.print("[cyan]Beende alle laufenden Chrome-Prozesse...[/cyan]")
    try:
        subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"], capture_output=True)
    except Exception as e:
        console.print(f"[yellow]Warnung: Konnte Chrome nicht beenden: {e}[/yellow]")

    profile_dir = os.path.join(os.getcwd(), "ChromeRemoteProfile")
    os.makedirs(profile_dir, exist_ok=True)

    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    console.print(f"[cyan]Starte Chrome mit Debug-Port {port}...[/cyan]")
    subprocess.Popen([
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
    ])
    time.sleep(2)

# -------------------- Playwright-Verbindung ----------------------------------
async def connect_chrome(port: int = _DEBUG_PORT):
    global _LOCK_HOST
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(f"http://localhost:{port}")
    except Exception as e:
        await pw.stop()
        raise RuntimeError("Konnte nicht zu Chrome verbinden.") from e

    if not browser.contexts:
        ctx = await browser.new_context()
    else:
        ctx = browser.contexts[0]

    page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    # Interne chrome:// Tabs meiden → auf http(s) wechseln
    try:
        cur_url = page.url
    except Exception:
        cur_url = ""
    if cur_url.startswith(("chrome://", "edge://", "brave://")):
        page = await ctx.new_page()
        await page.goto("https://www.google.com", wait_until="domcontentloaded")

    _LOCK_HOST = urlparse(page.url).hostname or ""
    return pw, browser, ctx, page

# --------------------- Seite/DOM-Helfer --------------------------------------
def shorten(txt: str, n: int = 1200) -> str:
    t = " ".join(txt.split())
    return (t[:n] + " …") if len(t) > n else t

async def read_page(page) -> dict:
    try:
        url = page.url
    except Exception:
        url = ""
    title = ""; body_text = ""
    if url.startswith("chrome://"):
        return {"url": url, "title": "(Chrome interner Tab)", "text": ""}
    try:
        title = await page.title()
    except Exception:
        pass
    try:
        body_text = await page.evaluate("() => document.body?.innerText || ''")
    except Exception:
        pass
    return {"url": url, "title": title, "text": body_text}

async def safe_confirm(prompt: str) -> bool:
    if not ASK_CONFIRM:
        return True
    console.print(f"[yellow]{prompt}[/yellow] [dim](j/N)[/dim]")
    try:
        ans = input().strip().lower()
    except EOFError:
        ans = "n"
    return ans.startswith("j") or ans == "y"

# --------------------- Befehle ------------------------------------------------
async def cmd_lese(page):
    data = await read_page(page)
    snippet = shorten(data["text"], 1500)
    console.print(Panel.fit(
        f"[bold]{data['title'] or '(ohne Titel)'}[/bold]\n[url]{data['url']}[/url]\n\n{snippet or '[kein sichtbarer Text]'}",
        title="Seite lesen", border_style="cyan"
    ))

async def cmd_gehe(page, url: str):
    # Domain-Lock & Chat-Only: Navigation unterbinden
    if FORCE_CHAT_MODE or LOCK_TO_DOMAIN:
        console.print("[yellow]Navigation ist im Chat-Modus gesperrt.[/yellow]")
        return
    if not url.startswith("http"):
        url = "https://" + url
    if await safe_confirm(f"Zu dieser URL navigieren: {url}?"):
        await page.goto(url, wait_until="domcontentloaded")
        await cmd_lese(page)

async def cmd_klicke(page, selector: str):
    if not await safe_confirm(f'Klicke Element: {selector}?'):
        console.print("[dim]Abgebrochen.[/dim]")
        return
    loc = page.locator(selector).first
    await loc.scroll_into_view_if_needed()
    await loc.click()
    console.print("[green]Klick ausgeführt.[/green]")

# --- Generische Auto-Zielsuche (vermeidet Suchleisten) -----------------------
async def _find_dom_input(page):
    """Generischer Finder für Chat-Eingabefelder.
    Markiert das beste Feld mit data-__agent="1" und liefert den Locator.
    Vermeidet Suchleisten, bevorzugt contenteditable/Textareas unten auf der Seite.
    """
    js = r"""
    (() => {
      const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 20 && r.height > 16 && st.visibility !== 'hidden' && st.display !== 'none';
      };
      const keywords = [
        'message','nachricht','reply','antwort','write a message','type a message',
        'gib eine nachricht ein','messaggio','mensaje','mensagem','メッセージ','сообщение'
      ];
      const searchWords = ['search','suche','suchen','buscar','recherche','поиск','pesquisa'];
      const cands = Array.from(document.querySelectorAll(
        "textarea, input[type='text']:not([type='search']), [contenteditable='true']"
      ));
      const scoreEl = (el) => {
        if (!isVisible(el)) return -1;
        let s = 0;
        const rect = el.getBoundingClientRect();
        const vh = window.innerHeight;
        const distBottom = Math.abs(vh - rect.bottom);
        // näher am unteren Rand → besser
        s += Math.max(0, 100 - Math.min(100, distBottom));
        if (el.isContentEditable) s += 40;
        if (el.tagName === 'TEXTAREA') s += 30;
        const attrs = (
          (el.getAttribute('placeholder')||'') + ' ' +
          (el.getAttribute('aria-label')||'') + ' ' +
          (el.getAttribute('role')||'')
        ).toLowerCase();
        if (keywords.some(k => attrs.includes(k))) s += 60;
        if (searchWords.some(k => attrs.includes(k))) s -= 120;
        const type = (el.getAttribute('type')||'').toLowerCase();
        if (type === 'search') s -= 120;
        // Ausschlüsse: Header/Search-Bereiche
        if (el.closest('header, [role="search"], [data-testid*="search" i], [aria-label*="such" i], [aria-label*="search" i]')) s -= 150;
        // Bonus: im Footer/Composer-Bereich
        if (el.closest('footer')) s += 30;
        // Bonus: Send-Button in der Nähe
        const container = el.closest('form, footer, section, div');
        if (container && container.querySelector("button[aria-label*='send' i], button[type='submit'], [data-testid*='send' i]")) s += 25;
        return s;
      };
      let best = null, bestScore = -1;
      for (const el of cands) {
        const sc = scoreEl(el);
        if (sc > bestScore) { best = el; bestScore = sc; }
      }
      document.querySelectorAll('[data-__agent]').forEach(e => e.removeAttribute('data-__agent'));
      if (best) { best.setAttribute('data-__agent','1'); return {ok:true}; }
      return {ok:false};
    })()
    """
    res = await page.evaluate(js)
    if not res or not res.get('ok'):
        return None
    try:
        return page.locator("[data-__agent='1']").first
    except Exception:
        return None

# --- Auto-Senden --------------------------------------------------------------
async def _maybe_auto_send(page):
    if not AUTO_SEND:
        return
    host = (urlparse(page.url).hostname or "").lower()

    # WhatsApp: Enter sendet
    if "web.whatsapp.com" in host:
        try:
            await page.keyboard.press("Enter")
            console.print("[green]Senden ausgelöst (Enter).[/green]")
            return
        except Exception:
            pass
        try:
            await page.locator("button[aria-label='Senden'], button[data-testid='compose-btn-send']").first.click()
            console.print("[green]Senden-Button geklickt.[/green]")
            return
        except Exception:
            console.print("[yellow]Konnte Senden nicht automatisch auslösen.[/yellow]")

    # Bumble: oft Enter oder Submit
    if "bumble.com" in host:
        try:
            await page.keyboard.press("Enter")
            console.print("[green]Senden ausgelöst (Enter).[/green]")
            return
        except Exception:
            pass
        try:
            await page.locator("button[aria-label*='Send'], button[type='submit']").first.click()
            console.print("[green]Senden-Button geklickt.[/green]")
            return
        except Exception:
            console.print("[yellow]Konnte Senden nicht automatisch auslösen.[/yellow]")


# --- Tippen -------------------------------------------------------------------
async def cmd_tippe(page, selector: str, text: str):
    selector = (selector or "").strip()

    # Domain-Lock: bleib auf der Seite (nur Info-Output)
    if LOCK_TO_DOMAIN:
        cur_host = (urlparse(page.url).hostname or "").lower()
        if _LOCK_HOST and cur_host != _LOCK_HOST:
            console.print("[yellow]Domain-Lock aktiv – wechsle nicht die Seite.[/yellow]")

    # Ziel ermitteln
    if selector:
        loc = page.locator(selector).first
        if await loc.count() == 0:
            console.print(f"[red]Kein Element gefunden für Selector:[/red] {selector}")
            return
    else:
        loc = await _find_dom_input(page)
        if not loc:
            console.print("[red]Kein passendes Eingabefeld gefunden (Auto-Suche).[/red]")
            return

    try:
        await loc.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        await loc.click()
    except Exception:
        pass
    # contenteditable hat oft kein .fill()
    try:
        await loc.fill("")
    except Exception:
        pass

    await loc.type(text, delay=15)
    console.print("[green]Text geschrieben.[/green]")
    await _maybe_auto_send(page)
    
    # Nach Senden: eigenen letzten Text merken (Echo-Schutz)
    global _last_sent_text, _last_sent_time
    _last_sent_text = text.strip()
    _last_sent_time = time.time()


async def cmd_scrolle(page, pixels: int):
    await page.evaluate("(y) => window.scrollBy(0, y)", pixels)
    console.print(f"[green]Gescr ollt: {pixels}px.[/green]")

async def cmd_auswahl(page, selector: str):
    loc = page.locator(selector)
    count = await loc.count()
    console.print(f"[cyan]Treffer: {count}[/cyan]")
    for i in range(min(count, 10)):
        el = loc.nth(i)
        try:
            txt = await el.inner_text()
        except Exception:
            txt = ""
        txt = shorten(txt or "", 200)
        console.print(f"[bold]{i:02d}[/bold]: {txt or '[kein sichtbarer Text]'}")

# --------------------- KI-Planung (Stub/LLM) ---------------------------------
def is_short_chat(text: str) -> bool:
    t = text.strip()
    return len(t) <= 200 and not t.lower().startswith(("klicke ", "scrolle ", "auswahl ", "gehe ", "tippe "))

async def call_llm_stub(context: str, user_msg: str) -> str:
    u = user_msg.strip(); low = u.lower()
    if FORCE_CHAT_MODE:
        if low.startswith("sag "):
            return f"tippe :: {u[4:].strip()}"
        if low.startswith("tippe "):
            return u
        return f"tippe :: {u}"
    if low.startswith("sag "):
        return f"tippe :: {u[4:].strip()}"
    if is_short_chat(u):
        return f"tippe :: {u}"
    if low.startswith(("gehe ", "klicke ", "tippe ")) or "scroll" in low:
        return u
    return "lese"

class LLMPlanner:
    def __init__(self):
        self.enabled = HAVE_OPENAI and bool(OPENAI_API_KEY)
        self.client = OpenAI(api_key=OPENAI_API_KEY) if self.enabled else None
        self.model = "gpt-4o-mini"

    async def suggest_command(self, context: str, user_msg: str) -> str:
        # Fallback: Stub, falls kein OpenAI aktiv
        if not self.enabled:
            return await call_llm_stub(context, user_msg)

        try:
            persona = get_ai_character()
            sys = (
                "Du bist ein Assistent, der Chrome über Befehle steuert.\n"
                "Beachte strikt den folgenden Charakter (Persona/Style):\n"
                f"{persona}\n\n"
                "Gib genau EINEN Befehl zurück, ohne Erklärung. Erlaubte Befehle:\n"
                "  - lese\n"
                "  - gehe <url>\n"
                "  - klicke <css-selector>\n"
                "  - tippe <css-selector> :: <text>\n"
                "  - scrolle <integer>\n"
                "  - auswahl <css-selector>\n"
                "Wähle robuste Selektoren (rollen-/label-basiert)."
            )
            msgs = [
                {"role": "system", "content": sys},
                {"role": "user", "content": f"Seitendaten (kompakt): {context}"},
                {"role": "user", "content": f"Anliegen: {user_msg}"},
            ]
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=msgs,
                temperature=0.2,
                max_tokens=120,
            )
            out = resp.choices[0].message.content.strip()
            return out
        except Exception as e:
            console.print(f"[yellow]KI-Fehler → nutze Stub:[/yellow] {e}")
            return await call_llm_stub(context, user_msg)

# Antwort-LLM für Auto-Responder
async def generate_reply(planner: "LLMPlanner", history_snippet: str, last_msg: str) -> str:
    if not planner.enabled:
        return f"Klingt gut! {last_msg[:60]}"
    try:
        persona = get_ai_character()
        msgs = [
            {"role": "system", "content": f"Du antwortest kurz, freundlich und kontextbezogen. Persona: {persona}"},
            {"role": "user", "content": f"Kontext (Auszug): {history_snippet}"},
            {"role": "user", "content": f"Letzte Nachricht der Gegenseite: {last_msg}"},
            {"role": "user", "content": "Formuliere eine kurze, natürliche Antwort (1–2 Sätze)."},
        ]
        resp = planner.client.chat.completions.create(
            model=planner.model,
            messages=msgs,
            temperature=0.7,
            max_tokens=80,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        console.print(f"[yellow]KI-Fehler (Auto-Reply) → Fallback:[/yellow] {e}")
        return "Alles klar! 🙂"

# --------------------- Persona + GUI -----------------------------------------
from threading import RLock
_AI_CHARACTER = (
    "Hilfsbereit, präzise, sicherheitsbewusst. Antworte knapp; übersetze natürliche Sprache in genau einen robusten Befehl."
)
_CHARACTER_LOCK = RLock()

def set_ai_character(text: str):
    global _AI_CHARACTER
    with _CHARACTER_LOCK:
        _AI_CHARACTER = text.strip() or _AI_CHARACTER

def get_ai_character() -> str:
    with _CHARACTER_LOCK:
        return _AI_CHARACTER

# GUI (Tk) in eigenem Thread
import tkinter as tk
from tkinter import ttk

class GUI:
    def __init__(self):
        self.msg_queue: "queue.Queue[str]" = queue.Queue()
        self._thread = threading.Thread(target=self._gui_thread_main, daemon=True)
        self._thread.start()

    def _gui_thread_main(self):
        self._root = tk.Tk()
        self._root.title("AI-Charakter")
        self._root.attributes("-topmost", True)
        self._root.geometry("460x260+60+60")

        frm = ttk.Frame(self._root, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text="Charakter / Persona der AI:").pack(anchor="w")
        self._txt_persona = tk.Text(frm, height=5, wrap=tk.WORD)
        self._txt_persona.insert("1.0", get_ai_character())
        self._txt_persona.pack(fill=tk.BOTH, expand=True, pady=(4, 6))

        # Auto-Modus Toggle
        self._auto_var = tk.BooleanVar(value=AUTO_MODE)
        row = ttk.Frame(frm); row.pack(fill=tk.X, pady=(4,0))
        ttk.Checkbutton(row, text="Auto-Antworten aktivieren", variable=self._auto_var, command=self._toggle_auto).pack(side=tk.LEFT)
        self._lbl_status = ttk.Label(row, text="Auto: AUS", foreground="#a00")
        self._lbl_status.pack(side=tk.LEFT, padx=8)

        btns = ttk.Frame(frm); btns.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(btns, text="Übernehmen Persona (Strg+Enter)", command=self._apply_persona).pack(side=tk.LEFT)
        self._txt_persona.bind("<Control-Return>", lambda e: self._apply_persona())

        # Chat-Fenster
        self._chat = tk.Toplevel(self._root)
        self._chat.title("Agent Chat")
        self._chat.attributes("-topmost", True)
        self._chat.geometry("620x360+540+60")
        frm2 = ttk.Frame(self._chat, padding=10); frm2.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm2, text="Befehl oder natürliche Sprache eingeben:").pack(anchor="w")
        self._entry = tk.Text(frm2, height=4, wrap=tk.WORD)
        self._entry.pack(fill=tk.BOTH, expand=True, pady=(4,6))

        btns2 = ttk.Frame(frm2); btns2.pack(fill=tk.X)
        ttk.Button(btns2, text="Senden (Enter)", command=self._send_from_gui).pack(side=tk.LEFT)
        ttk.Button(btns2, text="lese",  command=lambda: self.msg_queue.put("lese")).pack(side=tk.LEFT, padx=(8,0))
        ttk.Button(btns2, text="hilfe", command=lambda: self.msg_queue.put("hilfe")).pack(side=tk.LEFT, padx=(8,0))
        self._entry.bind("<Return>", self._on_enter)

        # History
        ttk.Label(frm2, text="History:").pack(anchor="w", pady=(8,0))
        self._history = tk.Text(frm2, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self._history.pack(fill=tk.BOTH, expand=True, pady=(4,0))

        try:
            self._root.lift(); self._chat.lift(); self._root.focus_force()
        except Exception:
            pass

        self._root.protocol("WM_DELETE_WINDOW", lambda: self._root.iconify())
        self._chat.protocol("WM_DELETE_WINDOW", lambda: self._chat.iconify())

        self._root.mainloop()

    # History-Helfer
    def _log_history(self, role: str, msg: str):
        self._history.configure(state=tk.NORMAL)
        self._history.insert("end", f"[{role}] {msg}\n")
        self._history.configure(state=tk.DISABLED)
        self._history.see("end")

    # Callbacks
    def _apply_persona(self):
        txt = self._txt_persona.get("1.0", "end").strip()
        set_ai_character(txt)
        try:
            self._root.attributes("-topmost", True); self._chat.attributes("-topmost", True)
        except Exception:
            pass

    def _toggle_auto(self):
        global AUTO_MODE
        AUTO_MODE = bool(self._auto_var.get())
        self._lbl_status.configure(text=f"Auto: {'AN' if AUTO_MODE else 'AUS'}", foreground=("#0a0" if AUTO_MODE else "#a00"))

    def _on_enter(self, event):
        if event.state & 0x0001:  # Shift = Zeilenumbruch
            return
        self._send_from_gui(); return "break"

    def _send_from_gui(self):
        text = self._entry.get("1.0", "end").strip()
        if text:
            self._log_history("Du", text)
            self.msg_queue.put(text)
            self._entry.delete("1.0", "end")

# --------------------- REPL (aus GUI-Queue) ----------------------------------
HELP = """Befehle (Beispiele):
  lese                        -> Seitentitel/URL + Textauszug anzeigen
  klicke <selector>           -> z.B. klicke input[name="q"]
  tippe <selector> :: <text>  -> Text in Feld schreiben
  tippe :: <text>             -> Auto-Zielfeld (Composer)
  sag <text>                  -> Kurzform für: tippe :: <text>
  scrolle <px>                -> positive Zahl runter, negative hoch
  auswahl <css>               -> alle Treffer listen (Textauszug)
  hilfe                       -> diese Hilfe anzeigen
  ende                        -> beenden
"""

async def gui_repl(page, gui: GUI):
    planner = LLMPlanner()
    console.print(Panel.fit("Gib Befehle im Agent-Chat-Fenster ein (Enter = senden, Shift+Enter = neue Zeile).",
                            title="GUI aktiv", border_style="magenta"))
    console.print(f"[cyan]KI-Status:[/cyan] {'OpenAI aktiv' if planner.enabled else 'Stub aktiv'}")
    console.print(Panel.fit("Tipp: `sag <Text>` oder `tippe :: <Text>` → schreibt direkt ins Nachrichtenfeld.",
                            border_style="green", title="Schnellbefehl"))

    await cmd_lese(page)

    # Auto-Responder Task
    auto_task = asyncio.create_task(auto_responder_loop(page, planner))

    while True:
        try:
            msg = gui.msg_queue.get(timeout=0.1)
        except queue.Empty:
            await asyncio.sleep(0.05)
            continue

        raw = msg.strip()
        if not raw:
            continue
        low = raw.lower()
        if low in ("ende", "quit", "exit"):
            break
        if low in ("hilfe", "help", "?"):
            console.print(Panel.fit(HELP, title="Hilfe", border_style="magenta"))
            continue

        # Natürliche Sprache → KI/Stub (liefert genau einen Befehl)
        if raw.split(" ", 1)[0] not in ("lese", "gehe", "klicke", "tippe", "scrolle", "auswahl"):
            page_state = await read_page(page)
            context = json.dumps({"url": page_state["url"], "title": page_state["title"]}, ensure_ascii=False)
            suggested = await planner.suggest_command(context=context, user_msg=raw)
            console.print(f"[dim]Vorgeschlagener Befehl:[/dim] {suggested}")
            gui._log_history("KI", suggested)
            raw = suggested

        # Befehl ausführen
        try:
            if raw.startswith("lese"):
                await cmd_lese(page)
            elif raw.startswith("gehe "):
                _, url = raw.split(" ", 1)
                await cmd_gehe(page, url.strip())
            elif raw.startswith("klicke "):
                _, sel = raw.split(" ", 1)
                await cmd_klicke(page, sel.strip())
            elif raw.startswith("tippe "):
                parts = raw.split("::", 1)
                if len(parts) == 2:
                    left, text = parts
                    if left.strip() == "tippe":
                        sel = ""
                    else:
                        _, sel = left.split(" ", 1)
                        sel = sel.strip()
                else:
                    sel = ""; text = raw.split(" ", 1)[1].strip()
                    if text.startswith(":"):
                        text = text.lstrip(": ")
                await cmd_tippe(page, sel, text.strip())
            elif raw.startswith("scrolle "):
                _, num = raw.split(" ", 1)
                await cmd_scrolle(page, int(num.strip()))
            elif raw.startswith("auswahl "):
                _, sel = raw.split(" ", 1)
                await cmd_auswahl(page, sel.strip())
            else:
                console.print("[red]Unbekannter Befehl. Tippe 'hilfe' im Chat-Fenster.[/red]")
        except Exception as e:
            console.print(f"[red]Fehler:[/red] {e}")

    auto_task.cancel()
    with contextlib.suppress(Exception):
        await auto_task

# --------------------- Auto-Responder ----------------------------------------
import contextlib
_last_seen_messages: dict[str, str] = {}

async def extract_latest_incoming_message(page) -> Optional[str]:
    """Liest die letzte *eingehende* Nachricht (WhatsApp/Bumble). Rückgabe: Text oder None."""
    host = (urlparse(page.url).hostname or "").lower()

    # WhatsApp: nur message-in (eingehend), message-out wird ignoriert
    if "web.whatsapp.com" in host:
        js = r"""
        (() => {
          // Alle Message-Container einsammeln (neue & alte DOMs)
          const containers = Array.from(document.querySelectorAll(
            "[data-testid='conversation-panel-body'] [data-testid='msg-container'], [data-testid='msg-container']"
          ));
          for (let i = containers.length - 1; i >= 0; i--) {
            const c = containers[i];
            const isIncoming = c.classList.contains("message-in") || !!c.querySelector("[data-pre-plain-text]");
            const isOutgoing = c.classList.contains("message-out");
            if (!isIncoming || isOutgoing) continue;

            // Text robust extrahieren
            let text = "";
            const parts = c.querySelectorAll("[data-testid='msg-text'], [dir='ltr'], span, p");
            for (const el of parts) {
              const t = (el.innerText || "").trim();
              if (t) text += (text ? " " : "") + t;
            }
            text = text.trim();
            if (text) return {ok:true, text};
          }
          return {ok:false};
        })()
        """
        try:
            res = await page.evaluate(js)
            if res and res.get("ok") and res.get("text"):
                return res["text"].strip()
        except Exception:
            pass
        return None

    # Bumble: letzte Message-Bubble im Chatverlauf
    if "bumble.com" in host:
        selectors = [
            "[data-qa='message-row'] [data-qa='message-text']",
            "[data-testid='chat-message']",
            "div[role='log'] div[role='article']",
        ]
        for sel in selectors:
            loc = page.locator(sel)
            count = await loc.count()
            if count:
                try:
                    txt = await loc.nth(count-1).inner_text()
                    if txt and len(txt.strip()) > 0:
                        return txt.strip()
                except Exception:
                    pass

    return None

async def auto_responder_loop(page, planner: LLMPlanner):
    """Hintergrund-Task: Pollt neue Nachrichten und antwortet automatisch, wenn AUTO_MODE aktiv ist."""
    while True:
        try:
            if AUTO_MODE:
                host = (urlparse(page.url).hostname or "").lower()
                if AUTO_DOMAINS.get(host, False):
                    latest = await extract_latest_incoming_message(page)
                    if latest:
                        key = f"{host}"
                        if _last_seen_messages.get(key) != latest:
                            # Echo-Schutz: Reagiere nicht auf eigenen Text im Cooldown-Fenster
                            now = time.time()
                            if (
                                _last_sent_text
                                and latest.strip() == _last_sent_text.strip()
                                and (now - _last_sent_time) < _ECHO_COOLDOWN_SEC
                            ):
                                # überspringen – das war sehr wahrscheinlich unsere eigene Nachricht
                                await asyncio.sleep(AUTO_POLL_SECONDS)
                                continue

                            # Kontext (kleiner Auszug der Seite)
                            snap = await read_page(page)
                            snippet = shorten(snap.get("text", ""), 600)
                            reply = await generate_reply(planner, snippet, latest)

                            # kleine natürliche Verzögerung
                            await asyncio.sleep(random.uniform(AUTO_MIN_REPLY_DELAY, AUTO_MAX_REPLY_DELAY))

                            await cmd_tippe(page, "", reply)
                            _last_seen_messages[key] = latest

            await asyncio.sleep(AUTO_POLL_SECONDS)

        except Exception as e:
            console.print(f"[yellow]Auto-Responder Warnung:[/yellow] {e}")
            await asyncio.sleep(AUTO_POLL_SECONDS)

# --------------------- main ---------------------------------------------------
async def main():
    start_chrome_with_debug_port(_DEBUG_PORT)
    pw, browser, ctx, page = await connect_chrome(_DEBUG_PORT)

    gui = GUI()
    try:
        await gui_repl(page, gui)
    finally:
        await browser.close()
        await pw.stop()

if __name__ == "__main__":
    asyncio.run(main())
