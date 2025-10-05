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
from typing import Optional, Callable

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

# Versionshinweis für die CMD-Ausgabe
AGENT_VERSION = "1.0.0"

# -------------------- Konfiguration ------------------------------------------
ASK_CONFIRM      = False  # keine Rückfragen vor Aktionen
AUTO_SEND        = True   # nach Tippen automatisch senden (domain-spezifisch)
FORCE_CHAT_MODE  = False  # nur Chat-Befehle zulassen (keine Navigation)
LOCK_TO_DOMAIN   = False  # Tab/Domain sperren – kein Wechsel erlaubt

# Auto-Responder
AUTO_MODE             = False   # Startzustand; im GUI umschaltbar
AUTO_POLL_SECONDS     = 3
AUTO_MIN_REPLY_DELAY  = 0.4
AUTO_MAX_REPLY_DELAY  = 1.4
AUTO_DOMAINS          = {
    "web.whatsapp.com": True,
    "bumble.com": True,
}

CHAT_DOMAINS = set(AUTO_DOMAINS.keys())
CHAT_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "web.whatsapp.com": ("whatsapp", "wa"),
    "bumble.com": ("bumble",),
}
DEFAULT_PROMPT_KEYWORDS = (
    "schreib",
    "schreibe",
    "sage",
    "sag",
    "antworte",
    "antwort",
    "antworten",
    "reply",
    "message",
    "nachricht",
)

_DEBUG_PORT = 9222
_LOCK_HOST  = ""  # wird in connect_chrome gesetzt

_last_sent_text: str = ""
_last_sent_time: float = 0.0
_last_sent_per_chat: dict[str, tuple[str, float]] = {}
_ECHO_COOLDOWN_SEC = 10.0  # innerhalb dieses Fensters niemals auf eigenen Text antworten

_context_update_handler: Optional[Callable[[str, str, str], None]] = None


def register_context_update_handler(handler: Optional[Callable[[str, str, str], None]]) -> None:
    global _context_update_handler
    _context_update_handler = handler


def _notify_context_update(title: str, url: str, text: str) -> None:
    if not _context_update_handler:
        return
    try:
        _context_update_handler(title, url, text)
    except Exception:
        pass

AUTO_REPLY_TOKEN = "__AUTO_REPLY__"
AUTO_REPLY_DRAFT_TOKEN = "__AUTO_REPLY_DRAFT__"

# -------------------- Transparenz-Hinweis ------------------------------------
console.print(f"[bold green]ChatHelper CMD Version {AGENT_VERSION}[/bold green]")
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
async def _ensure_active_page(page):
    """Stellt sicher, dass wir den sichtbaren Tab referenzieren."""
    if page is None:
        return page
    candidate = None
    try:
        ctx = page.context
    except Exception:
        ctx = None

    if ctx:
        try:
            pages = list(ctx.pages)
        except Exception:
            pages = []
        for p in reversed(pages):  # zuletzt geöffneter Tab zuerst prüfen
            try:
                if p.is_closed():
                    continue
            except Exception:
                continue
            try:
                url = p.url
            except Exception:
                url = ""
            if url.startswith(("http://", "https://")):
                candidate = p
                break
            if candidate is None:
                candidate = p
        if candidate:
            page = candidate

    try:
        await page.bring_to_front()
    except Exception:
        pass
    try:
        await page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass

    global _LOCK_HOST
    try:
        host = urlparse(page.url).hostname or ""
    except Exception:
        host = ""
    if LOCK_TO_DOMAIN and not _LOCK_HOST and host:
        _LOCK_HOST = host

    return page


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _chat_storage_key(host: str, identity: Optional[str]) -> Optional[str]:
    host = (host or "").strip().lower()
    identity = (identity or "").strip()
    if not host:
        return None
    if identity:
        return f"{host}::{identity}"
    return host


async def get_active_chat_identity(page) -> Optional[str]:
    try:
        host = (urlparse(page.url).hostname or "").lower()
    except Exception:
        host = ""

    if "web.whatsapp.com" in host:
        js = r"""
        (() => {
          const headerSelectors = [
            "[data-testid='conversation-info-header']",
            "[data-testid='conversation-header']",
            "header [data-testid='conversation-panel']"
          ];
          let header = null;
          for (const sel of headerSelectors) {
            const candidate = document.querySelector(sel);
            if (candidate) { header = candidate; break; }
          }
          if (!header) return null;
          const nameSelectors = [
            "[data-testid='conversation-info-header-chat-title']",
            "h1[role='heading'] span",
            "div[role='heading'] span",
            "span[title]",
            "span[dir='auto']"
          ];
          for (const sel of nameSelectors) {
            const el = header.querySelector(sel);
            if (el) {
              const txt = (el.innerText || el.textContent || "").trim();
              if (txt) return txt;
            }
          }
          const fallback = (header.innerText || "").trim();
          return fallback || null;
        })()
        """
        try:
            name = await page.evaluate(js)
        except Exception:
            name = None
        if isinstance(name, str) and name.strip():
            return name.strip()

    return None


async def cmd_lese(page):
    page = await _ensure_active_page(page)
    data = await read_page(page)
    snippet = shorten(data["text"], 1500)
    _notify_context_update(
        data["title"] or "(ohne Titel)",
        data["url"],
        snippet or "[kein sichtbarer Text]",
    )
    console.print(Panel.fit(
        f"[bold]{data['title'] or '(ohne Titel)'}[/bold]\n[url]{data['url']}[/url]\n\n{snippet or '[kein sichtbarer Text]'}",
        title="Seite lesen", border_style="cyan"
    ))
    return page

async def cmd_gehe(page, url: str):
    page = await _ensure_active_page(page)
    # Domain-Lock & Chat-Only: Navigation unterbinden
    if FORCE_CHAT_MODE or LOCK_TO_DOMAIN:
        console.print("[yellow]Navigation ist im Chat-Modus gesperrt.[/yellow]")
        return page
    if not url.startswith("http"):
        url = "https://" + url
    if await safe_confirm(f"Zu dieser URL navigieren: {url}?"):
        await page.goto(url, wait_until="domcontentloaded")
        page = await cmd_lese(page)
    return page

async def cmd_klicke(page, selector: str):
    page = await _ensure_active_page(page)
    if not await safe_confirm(f'Klicke Element: {selector}?'):
        console.print("[dim]Abgebrochen.[/dim]")
        return page
    loc = page.locator(selector).first
    await loc.scroll_into_view_if_needed()
    await loc.click()
    console.print("[green]Klick ausgeführt.[/green]")
    return page

# --- Generische Auto-Zielsuche (vermeidet Suchleisten) -----------------------
async def _find_dom_input(page):
    """Generischer Finder für Chat-Eingabefelder.
    Markiert das beste Feld mit data-__agent="1" und liefert den Locator.
    Vermeidet Suchleisten, bevorzugt contenteditable/Textareas unten auf der Seite.
    Untersucht zusätzlich iframes/Shadow-DOMs, um Chat-Composer auf verschiedenen
    Plattformen robuster zu erkennen.
    """

    async def _mark_locator(frame, loc):
        try:
            handle = await loc.element_handle()
        except Exception:
            handle = None
        if not handle:
            return None
        try:
            await frame.evaluate(
                """
                (el) => {
                  try {
                    document.querySelectorAll('[data-__agent]')
                      .forEach(e => e.removeAttribute('data-__agent'));
                  } catch (err) {}
                  el.setAttribute('data-__agent', '1');
                }
                """,
                handle,
            )
        except Exception:
            return loc
        finally:
            try:
                await handle.dispose()
            except Exception:
                pass
        try:
            return frame.locator("[data-__agent='1']").first
        except Exception:
            return loc

    async def _try_frame(frame, host_hint: str):
        try:
            frame_host = (urlparse(frame.url).hostname or host_hint).lower()
        except Exception:
            frame_host = host_hint

        # Spezieller Pfad für WhatsApp: der eigentliche Composer sitzt mittig im Layout.
        if "web.whatsapp.com" in frame_host:
            wa_locator = frame.locator(
                "[data-testid='conversation-compose-box-input'] [contenteditable='true']"
            ).first
            if await wa_locator.count():
                marked = await _mark_locator(frame, wa_locator)
                if marked:
                    return marked
            # Fallback: bekannte data-tab Werte des Composer-Bereichs
            wa_locator = frame.locator("div[contenteditable='true'][data-tab='10']").first
            if await wa_locator.count():
                marked = await _mark_locator(frame, wa_locator)
                if marked:
                    return marked

        js = r"""
        (() => {
          const clearMarks = (root) => {
            try {
              root.querySelectorAll('[data-__agent]').forEach(el => el.removeAttribute('data-__agent'));
            } catch (err) {}
          };
          const selectors = "textarea, input[type='text']:not([type='search']), input:not([type])," +
                             " [contenteditable='true'], [role='textbox']";
          const collectCandidates = (root, acc, seen) => {
            if (!root || seen.has(root)) return;
            seen.add(root);
            let elements = [];
            try {
              elements = Array.from(root.querySelectorAll(selectors));
            } catch (err) {}
            for (const el of elements) {
              if (!acc.includes(el)) acc.push(el);
            }
            let walker;
            try {
              walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
            } catch (err) {
              walker = null;
            }
            if (!walker) return;
            let current = walker.nextNode();
            while (current) {
              if (current.shadowRoot) {
                collectCandidates(current.shadowRoot, acc, seen);
              }
              current = walker.nextNode();
            }
          };

          const isVisible = (el) => {
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            return r.width > 20 && r.height > 16 &&
                   st.visibility !== 'hidden' && st.display !== 'none';
          };
          const keywords = [
            'message','nachricht','reply','antwort','write a message','type a message',
            'gib eine nachricht ein','messaggio','mensaje','mensagem','メッセージ','сообщение'
          ];
          const searchWords = ['search','suche','suchen','buscar','recherche','поиск','pesquisa'];

          const candidates = [];
          collectCandidates(document, candidates, new Set());

          const scoreEl = (el) => {
            if (!isVisible(el)) return -1;
            let s = 0;
            const rect = el.getBoundingClientRect();
            const vh = window.innerHeight || document.documentElement.clientHeight || 0;
            const distBottom = Math.abs(vh - rect.bottom);
            // näher am unteren Rand → besser
            s += Math.max(0, 120 - Math.min(120, distBottom));
            if (el.isContentEditable) s += 50;
            const tag = el.tagName;
            if (tag === 'TEXTAREA') s += 40;
            if (tag === 'DIV') s += 10;
            const attrs = (
              (el.getAttribute('placeholder')||'') + ' ' +
              (el.getAttribute('data-placeholder')||'') + ' ' +
              (el.getAttribute('aria-label')||'') + ' ' +
              (el.getAttribute('role')||'') + ' ' +
              (el.getAttribute('id')||'') + ' ' +
              (el.getAttribute('name')||'')
            ).toLowerCase();
            if (keywords.some(k => attrs.includes(k))) s += 70;
            if (searchWords.some(k => attrs.includes(k))) s -= 140;
            const type = (el.getAttribute('type')||'').toLowerCase();
            if (type === 'search') s -= 140;
            if (type === 'email' || type === 'password') s -= 60;
            const dataTab = (el.getAttribute('data-tab')||'').toLowerCase();
            if (dataTab === '3') s -= 220; // WhatsApp Suchfeld
            if (['6','7','9','10','11'].includes(dataTab)) s += 60; // WhatsApp Composer-Bereich
            if (el.closest('header, [role="search"], [data-testid*="search" i], [aria-label*="such" i], [aria-label*="search" i]')) s -= 160;
            if (el.closest('[data-testid="chat-list-search"]')) s -= 220;
            if (el.closest('aside')) s -= 30;
            if (el.closest('footer, [data-testid*="composer" i], [data-testid*="footer" i]')) s += 35;
            const container = el.closest('form, footer, section, div');
            if (container && container.querySelector("button[aria-label*='send' i], button[type='submit'], [data-testid*='send' i]")) s += 30;
            const labelledBy = el.getAttribute('aria-labelledby');
            if (labelledBy) {
              const labels = labelledBy.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean);
              for (const lbl of labels) {
                const txt = (lbl.innerText || lbl.textContent || '').toLowerCase();
                if (keywords.some(k => txt.includes(k))) { s += 40; break; }
                if (searchWords.some(k => txt.includes(k))) { s -= 120; break; }
              }
            }
            return s;
          };

          let best = null, bestScore = -1;
          for (const el of candidates) {
            const sc = scoreEl(el);
            if (sc > bestScore) {
              best = el;
              bestScore = sc;
            }
          }

          clearMarks(document);
          if (best) {
            try { best.setAttribute('data-__agent','1'); } catch (err) {}
            return {ok:true};
          }
          return {ok:false};
        })()
        """

        try:
            res = await frame.evaluate(js)
        except Exception:
            return None
        if not res or not res.get("ok"):
            return None
        try:
            return frame.locator("[data-__agent='1']").first
        except Exception:
            return None

    host = (urlparse(page.url).hostname or "").lower()

    seen_frames = set()
    frames_to_check = []
    try:
        main_frame = page.main_frame
    except Exception:
        main_frame = None
    if main_frame:
        frames_to_check.append(main_frame)
        seen_frames.add(main_frame)

    for fr in getattr(page, "frames", []):
        if fr is None or fr in seen_frames:
            continue
        frames_to_check.append(fr)
        seen_frames.add(fr)

    for frame in frames_to_check:
        loc = await _try_frame(frame, host)
        if loc:
            return loc

    return None


async def _focus_locator_with_retries(loc, attempts: int = 3, delay: float = 0.25) -> bool:
    """Versucht wiederholt, einen Locator zu fokussieren."""

    for attempt in range(attempts):
        try:
            await loc.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            await loc.click()
            return True
        except Exception:
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
    return False

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
    page = await _ensure_active_page(page)
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

    async def _current_text() -> str:
        try:
            return await loc.evaluate(
                """
                (el) => {
                  const readValue = (target) => {
                    if (!target) return '';
                    if (target.tagName === 'TEXTAREA' || target.tagName === 'INPUT') {
                      return target.value || '';
                    }
                    if (target.isContentEditable) {
                      return target.innerText || '';
                    }
                    return target.textContent || '';
                  };
                  const direct = readValue(el);
                  if (direct) return direct;
                  const nested = el.querySelector('[contenteditable="true"], textarea, input');
                  return readValue(nested);
                }
                """
            )
        except Exception:
            return ""

    def _normalize(txt: str) -> str:
        return " ".join((txt or "").replace("\xa0", " ").split())

    async def _has_typed_text(expected: str) -> bool:
        normalized_expected = _normalize(expected)
        if not normalized_expected:
            return True
        current = _normalize(await _current_text())
        if not current:
            return False
        return normalized_expected in current or current.endswith(normalized_expected)

    focused = await _focus_locator_with_retries(loc)
    if not focused:
        console.print("[yellow]Konnte Eingabefeld nicht zuverlässig fokussieren.[/yellow]")
    # contenteditable hat oft kein .fill()
    try:
        await loc.fill("")
    except Exception:
        pass
    try:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
    except Exception:
        pass

    last_error: Optional[Exception] = None
    typed_ok = False

    # 1) Regulär tippen
    try:
        await loc.type(text, delay=15)
        typed_ok = await _has_typed_text(text)
    except Exception as e:
        last_error = e
        typed_ok = False

    # 2) Fallback: insert_text über Keyboard (z.B. für contenteditable Lexical)
    if not typed_ok:
        try:
            await page.keyboard.insert_text(text)
            typed_ok = await _has_typed_text(text)
        except Exception as e:
            last_error = e
            typed_ok = False

    # 3) Fallback: direkte DOM-Manipulation mit Events
    if not typed_ok:
        try:
            await loc.evaluate(
                """
                (el, value) => {
                  const apply = (target) => {
                    if (!target) return false;
                    if (target.tagName === 'TEXTAREA' || target.tagName === 'INPUT') {
                      target.value = value;
                      target.dispatchEvent(new Event('input', {bubbles: true}));
                      target.dispatchEvent(new Event('change', {bubbles: true}));
                      return true;
                    }
                    if (target.isContentEditable) {
                      target.focus();
                      target.innerHTML = '';
                      const textNode = document.createTextNode(value);
                      target.appendChild(textNode);
                      target.dispatchEvent(new InputEvent('input', {data: value, bubbles: true}));
                      target.dispatchEvent(new Event('change', {bubbles: true}));
                      return true;
                    }
                    return false;
                  };
                  if (apply(el)) return;
                  const nested = el.querySelector('[contenteditable="true"], textarea, input');
                  if (nested) apply(nested);
                }
                """,
                text,
            )
            typed_ok = await _has_typed_text(text)
        except Exception as e:
            last_error = e
            typed_ok = False

    if not typed_ok:
        err_msg = "[red]Konnte Text nicht in das Eingabefeld eintragen.[/red]"
        if last_error:
            err_msg += f" [dim]({last_error})[/dim]"
        console.print(err_msg)
        return page

    console.print("[green]Text geschrieben.[/green]")
    await _maybe_auto_send(page)

    # Nach Senden: eigenen letzten Text merken (Echo-Schutz)
    global _last_sent_text, _last_sent_time, _last_sent_per_chat
    _last_sent_text = text.strip()
    _last_sent_time = time.time()

    try:
        host = (urlparse(page.url).hostname or "").lower()
    except Exception:
        host = ""
    chat_identity = await get_active_chat_identity(page) if host in CHAT_DOMAINS else None
    key = _chat_storage_key(host, chat_identity)
    if key:
        _last_sent_per_chat[key] = (_last_sent_text, _last_sent_time)
    return page


async def cmd_scrolle(page, pixels: int):
    page = await _ensure_active_page(page)
    await page.evaluate("(y) => window.scrollBy(0, y)", pixels)
    console.print(f"[green]Gescrollt: {pixels}px.[/green]")
    return page

async def cmd_auswahl(page, selector: str):
    page = await _ensure_active_page(page)
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
    return page

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


async def _maybe_reuse_existing_page(page, command: str, prompt: str) -> str:
    """Verhindert unnötige Reloads, wenn wir bereits auf der Zielseite sind."""

    if not command or not command.startswith("gehe "):
        return command

    current_host = _hostname(getattr(page, "url", ""))
    target = command.split(" ", 1)[1].strip()
    if not target:
        return command
    if not target.startswith("http"):
        target = f"https://{target}"
    target_host = _hostname(target)

    if not prompt.strip():
        return command

    if not _should_reuse_existing_page(current_host, target_host, prompt):
        return command

    console.print(
        f"[cyan]Bereits auf {target_host} – benutze bestehendes Eingabefeld statt neu zu laden.[/cyan]"
    )
    return f"tippe :: {prompt.strip()}"


def _should_reuse_existing_page(current_host: str, target_host: str, prompt: str) -> bool:
    if not current_host or not target_host or current_host != target_host:
        return False

    low_prompt = prompt.lower()
    domain_keywords = CHAT_DOMAIN_KEYWORDS.get(current_host, tuple())

    if current_host in CHAT_DOMAINS:
        return True

    keywords = DEFAULT_PROMPT_KEYWORDS + domain_keywords
    return any(word in low_prompt for word in keywords)


# Antwort-LLM für Auto-Responder
async def generate_reply(
    planner: "LLMPlanner",
    history_snippet: str,
    last_msg: str,
    instruction: str = "",
    chat_identity: Optional[str] = None,
) -> str:
    if not planner.enabled:
        base = "Alles klar!"
        if last_msg:
            base = f"Klingt gut! {last_msg[:60]}"
        extra = instruction.strip()
        label = (chat_identity or "").strip()
        if label:
            base = f"[{label}] {base}"
        if extra:
            return f"{base} ({extra})"
        return base
    try:
        persona = get_ai_character()
        chat_label = (chat_identity or "").strip()
        msgs = [
            {
                "role": "system",
                "content": (
                    "Du antwortest kurz, freundlich und kontextbezogen. "
                    f"Persona: {persona}" + (f" Du schreibst gerade mit '{chat_label}'." if chat_label else "")
                ),
            },
            {"role": "user", "content": f"Kontext (Auszug): {history_snippet}"},
            {"role": "user", "content": f"Letzte Nachricht der Gegenseite: {last_msg}"},
        ]
        if instruction.strip():
            msgs.append({"role": "user", "content": f"Zusätzliche Anweisung: {instruction}"})
        msgs.append({"role": "user", "content": "Formuliere eine kurze, natürliche Antwort (1–2 Sätze)."})
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


async def compose_chat_reply(page, planner: "LLMPlanner", instruction: str) -> Optional[str]:
    page = await _ensure_active_page(page)
    try:
        host = (urlparse(page.url).hostname or "").lower()
    except Exception:
        host = ""

    if host not in CHAT_DOMAINS:
        return None

    chat_identity = await get_active_chat_identity(page) if host in CHAT_DOMAINS else None
    latest = await extract_latest_incoming_message(page)
    history = await extract_chat_history(page, max_messages=12)

    snippet_source = history
    if not snippet_source:
        snap = await read_page(page)
        snippet_source = shorten(snap.get("text", ""), 600)

    if snippet_source and chat_identity:
        snippet_source = f"Chat mit {chat_identity}:\n{snippet_source}"

    if snippet_source:
        try:
            current_url = page.url
        except Exception:
            current_url = ""
        context_title = chat_identity or "Aktueller Kontext"
        _notify_context_update(context_title, current_url, snippet_source)

    if not latest and not snippet_source:
        return None

    reply = await generate_reply(
        planner,
        snippet_source,
        latest or "",
        instruction,
        chat_identity=chat_identity,
    )
    if reply:
        return reply.strip()
    return None

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
        ttk.Button(btns2, text="lese",  command=lambda: self.msg_queue.put("lese")).pack(side=tk.LEFT)
        ttk.Button(btns2, text="hilfe", command=lambda: self.msg_queue.put("hilfe")).pack(side=tk.LEFT, padx=(8,0))
        ttk.Button(btns2, text="AI Entwurf", command=self._request_ai_draft).pack(side=tk.LEFT, padx=(8,0))
        ttk.Button(btns2, text="AI antwortet", command=self._request_ai_reply).pack(side=tk.LEFT, padx=(8,0))
        self._entry.bind("<Return>", self._on_enter)

        # History
        ttk.Label(frm2, text="History:").pack(anchor="w", pady=(8,0))
        self._history = tk.Text(frm2, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self._history.pack(fill=tk.BOTH, expand=True, pady=(4,0))

        ttk.Label(frm2, text="Kontext (letzte Analyse):").pack(anchor="w", pady=(8,0))
        self._context = tk.Text(frm2, height=8, wrap=tk.WORD, state=tk.DISABLED)
        self._context.pack(fill=tk.BOTH, expand=True, pady=(4,0))

        try:
            self._root.lift(); self._chat.lift(); self._root.focus_force()
        except Exception:
            pass

        register_context_update_handler(self._update_context)

        self._root.protocol("WM_DELETE_WINDOW", lambda: self._root.iconify())
        self._chat.protocol("WM_DELETE_WINDOW", lambda: self._chat.iconify())

        self._root.mainloop()

    # History-Helfer
    def _log_history(self, role: str, msg: str):
        self._history.configure(state=tk.NORMAL)
        self._history.insert("end", f"[{role}] {msg}\n")
        self._history.configure(state=tk.DISABLED)
        self._history.see("end")

    def _update_context(self, title: str, url: str, text: str):
        def _apply():
            header = f"{title}\n{url}\n\n" if url else f"{title}\n\n"
            body = text.strip() or "[kein sichtbarer Text]"
            self._context.configure(state=tk.NORMAL)
            self._context.delete("1.0", "end")
            self._context.insert("1.0", header + body)
            self._context.configure(state=tk.DISABLED)
            self._context.see("1.0")

        try:
            self._context.after(0, _apply)
        except Exception:
            pass

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

    def _request_ai_reply(self):
        self._log_history("System", "AI-Antwort ausgelöst")
        self.msg_queue.put(AUTO_REPLY_TOKEN)

    def _request_ai_draft(self):
        self._log_history("System", "AI-Entwurf angefordert")
        self.msg_queue.put(AUTO_REPLY_DRAFT_TOKEN)

    def _set_entry_text(self, text: str):
        def _update():
            self._entry.delete("1.0", "end")
            self._entry.insert("1.0", text)
            try:
                self._entry.focus_set()
            except Exception:
                pass

        try:
            self._entry.after(0, _update)
        except Exception:
            pass

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

    page = await cmd_lese(page)

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
        if raw == AUTO_REPLY_TOKEN:
            reply_text = await compose_chat_reply(page, planner, "")
            if reply_text:
                gui._log_history("KI", reply_text)
                page = await cmd_tippe(page, "", reply_text)
            else:
                console.print("[yellow]Keine eingehende Nachricht gefunden, auf die geantwortet werden kann.[/yellow]")
            continue
        if raw == AUTO_REPLY_DRAFT_TOKEN:
            reply_text = await compose_chat_reply(page, planner, "")
            if reply_text:
                gui._log_history("KI", f"Entwurf: {reply_text}")
                gui._set_entry_text(reply_text)
            else:
                console.print("[yellow]Keine eingehende Nachricht gefunden, für die ein Entwurf erstellt werden kann.[/yellow]")
            continue
        low = raw.lower()
        if low in ("ende", "quit", "exit"):
            break
        if low in ("hilfe", "help", "?"):
            console.print(Panel.fit(HELP, title="Hilfe", border_style="magenta"))
            continue

        # Natürliche Sprache → KI/Stub (liefert genau einen Befehl)
        if raw.split(" ", 1)[0] not in ("lese", "gehe", "klicke", "tippe", "scrolle", "auswahl"):
            reply_text = await compose_chat_reply(page, planner, raw)
            if reply_text:
                gui._log_history("KI", reply_text)
                page = await cmd_tippe(page, "", reply_text)
                continue

            page = await _ensure_active_page(page)
            page_state = await read_page(page)
            context = json.dumps({"url": page_state["url"], "title": page_state["title"]}, ensure_ascii=False)
            suggested = await planner.suggest_command(context=context, user_msg=raw)
            suggested = await _maybe_reuse_existing_page(page, suggested, raw)
            console.print(f"[dim]Vorgeschlagener Befehl:[/dim] {suggested}")
            gui._log_history("KI", suggested)
            raw = suggested

        # Befehl ausführen
        try:
            if raw.startswith("lese"):
                page = await cmd_lese(page)
            elif raw.startswith("gehe "):
                _, url = raw.split(" ", 1)
                page = await cmd_gehe(page, url.strip())
            elif raw.startswith("klicke "):
                _, sel = raw.split(" ", 1)
                page = await cmd_klicke(page, sel.strip())
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
                page = await cmd_tippe(page, sel, text.strip())
            elif raw.startswith("scrolle "):
                _, num = raw.split(" ", 1)
                page = await cmd_scrolle(page, int(num.strip()))
            elif raw.startswith("auswahl "):
                _, sel = raw.split(" ", 1)
                page = await cmd_auswahl(page, sel.strip())
            else:
                console.print("[red]Unbekannter Befehl. Tippe 'hilfe' im Chat-Fenster.[/red]")
        except Exception as e:
            console.print(f"[red]Fehler:[/red] {e}")

    auto_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
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
          const ACTIVE_CHAT_SELECTORS = [
            "[data-testid='conversation-panel-body']",
            "[data-testid='conversation-panel']",
            "[data-testid='conversation-panel-messages']",
            "[data-testid='pane-chat-body']",
            "[aria-label='Nachrichten']",
            "[aria-label='Conversation thread']",
          ];

          const isInActiveChat = (el) => {
            return ACTIVE_CHAT_SELECTORS.some(sel => el.closest(sel));
          };

          // Alle Message-Container einsammeln (neue & alte DOMs), dabei die Chatliste links ignorieren
          const containers = Array.from(document.querySelectorAll("[data-testid='msg-container']"))
            .filter(c => isInActiveChat(c));

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


async def extract_chat_history(page, max_messages: int = 12) -> Optional[str]:
    """Erzeugt einen kurzen Chat-Auszug (inkl. Sprecher), falls verfügbar."""
    host = (urlparse(page.url).hostname or "").lower()

    if "web.whatsapp.com" in host:
        js = r"""
        (limit) => {
          const ACTIVE_CHAT_SELECTORS = [
            "[data-testid='conversation-panel-body']",
            "[data-testid='conversation-panel']",
            "[data-testid='conversation-panel-messages']",
            "[data-testid='pane-chat-body']",
            "[aria-label='Nachrichten']",
            "[aria-label='Conversation thread']",
          ];

          const isInActiveChat = (el) => {
            return ACTIVE_CHAT_SELECTORS.some(sel => el.closest(sel));
          };

          const containers = Array.from(document.querySelectorAll("[data-testid='msg-container']"))
            .filter((c) => isInActiveChat(c));

          const take = containers.slice(-Math.max(1, limit));
          const items = [];

          const extractText = (node) => {
            let text = "";
            const parts = node.querySelectorAll("[data-testid='msg-text'], [dir='ltr'], span, p");
            for (const el of parts) {
              const t = (el.innerText || "").trim();
              if (t) text += (text ? " " : "") + t;
            }
            return text.trim();
          };

          for (const c of take) {
            const isIncoming = c.classList.contains("message-in") || !!c.querySelector("[data-pre-plain-text]");
            const isOutgoing = c.classList.contains("message-out");
            let role = null;
            if (isOutgoing) role = "me";
            else if (isIncoming) role = "them";
            if (!role) continue;

            const text = extractText(c);
            if (!text) continue;

            items.push({ role, text });
          }

          return { ok: items.length > 0, items };
        }
        """
        try:
            res = await page.evaluate(js, max_messages)
        except Exception:
            res = None

        if res and res.get("ok") and res.get("items"):
            lines = []
            for item in res["items"]:
                role = "Du" if item.get("role") == "me" else "Gegenüber"
                text = item.get("text", "").strip()
                if text:
                    lines.append(f"{role}: {text}")
            if lines:
                summary = "\n".join(lines)
                return shorten(summary, 800)

    return None

async def auto_responder_loop(page, planner: LLMPlanner):
    """Hintergrund-Task: Pollt neue Nachrichten und antwortet automatisch, wenn AUTO_MODE aktiv ist."""
    while True:
        try:
            if AUTO_MODE:
                page = await _ensure_active_page(page)
                host = (urlparse(page.url).hostname or "").lower()
                if AUTO_DOMAINS.get(host, False):
                    chat_identity = await get_active_chat_identity(page)
                    chat_key = _chat_storage_key(host, chat_identity)
                    if not chat_key:
                        await asyncio.sleep(AUTO_POLL_SECONDS)
                        continue

                    latest = await extract_latest_incoming_message(page)
                    if latest:
                        if _last_seen_messages.get(chat_key) != latest:
                            # Echo-Schutz: Reagiere nicht auf eigenen Text im Cooldown-Fenster
                            now = time.time()
                            last_sent_text, last_sent_time = _last_sent_per_chat.get(chat_key, ("", 0.0))
                            if (
                                last_sent_text
                                and latest.strip() == last_sent_text.strip()
                                and (now - last_sent_time) < _ECHO_COOLDOWN_SEC
                            ):
                                # überspringen – das war sehr wahrscheinlich unsere eigene Nachricht
                                await asyncio.sleep(AUTO_POLL_SECONDS)
                                continue

                            # Kontext (kleiner Auszug der Seite)
                            snap = await read_page(page)
                            snippet = shorten(snap.get("text", ""), 600)
                            _notify_context_update(
                                chat_identity or "Aktueller Kontext",
                                snap.get("url", ""),
                                snippet or "",
                            )
                            reply = await generate_reply(
                                planner,
                                snippet,
                                latest,
                                "",
                                chat_identity=chat_identity,
                            )

                            # kleine natürliche Verzögerung
                            await asyncio.sleep(random.uniform(AUTO_MIN_REPLY_DELAY, AUTO_MAX_REPLY_DELAY))

                            page = await cmd_tippe(page, "", reply)
                            _last_seen_messages[chat_key] = latest

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
