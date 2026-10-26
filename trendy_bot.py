#!/usr/bin/env python3
"""
Bot que vigila el lanzamiento del Calendario de Adviento de Trendy
(https://www.maquillajetrendyshop.com, tienda VTEX).

Detecta:
  1. APARECE      -> un producto nuevo que coincide con las palabras clave
  2. DISPONIBLE   -> pasa de agotado/no listado a poder comprarse
  3. PRECIO       -> cambia el precio
  4. PAGINA       -> la portada o la página de términos empiezan a mencionar
                     el calendario de adviento (señal temprana)

Solo usa la librería estándar de Python 3.8+. Sin dependencias.

Uso:
  python3 trendy_bot.py              # revisa cada 5 min hasta que lo pares (Ctrl+C)
  python3 trendy_bot.py --once       # una sola revisión (ideal para cron)
  python3 trendy_bot.py --interval 120

Avisos por WhatsApp (CallMeBot): define WHATSAPP_PHONE (con código de país,
ej. 573001234567) y CALLMEBOT_APIKEY.
Avisos por Telegram (opcional): define TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID.
Sin eso, el aviso sale por la terminal (con pitido).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "https://www.maquillajetrendyshop.com"

# Palabras que identifican el producto. Ajústalas si Trendy lo llama distinto.
KEYWORDS = ["calendario de adviento", "adviento", "advent", "calendario 2026", "calendario navide"]

# Términos que se envían al buscador de la tienda.
SEARCH_TERMS = ["calendario de adviento", "adviento", "calendario 2026", "calendario navidad"]

# Páginas HTML que se vigilan buscando menciones del calendario.
WATCH_PAGES = [
    BASE + "/",
    BASE + "/servicio-al-cliente",
]

STATE_FILE = Path(__file__).with_name("trendy_state.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


# ----------------------------------------------------------------- red
def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def search_products(term):
    """API pública de catálogo de VTEX."""
    url = f"{BASE}/api/catalog_system/pub/products/search?ft={urllib.parse.quote(term)}&_from=0&_to=49"
    try:
        return json.loads(http_get(url))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        log(f"  aviso: búsqueda '{term}' falló ({e})")
        return []


# ----------------------------------------------------------- utilidades
def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def matches(text):
    t = (text or "").lower()
    return any(k in t for k in KEYWORDS)


def money(v):
    return f"${v:,.0f}".replace(",", ".") if isinstance(v, (int, float)) else "?"


def summarize(product):
    """Reduce un producto VTEX a lo que nos interesa comparar."""
    best_price, list_price, available = None, None, False
    for item in product.get("items", []):
        for seller in item.get("sellers", []):
            offer = seller.get("commertialOffer", {})
            if offer.get("AvailableQuantity", 0) > 0:
                available = True
                p = offer.get("Price")
                if best_price is None or (p is not None and p < best_price):
                    best_price, list_price = p, offer.get("ListPrice")
            elif best_price is None:
                best_price = offer.get("Price")
                list_price = offer.get("ListPrice")
    return {
        "id": str(product.get("productId")),
        "name": product.get("productName", "?"),
        "url": product.get("link") or f"{BASE}/{product.get('linkText', '')}/p",
        "price": best_price,
        "list_price": list_price,
        "available": available,
    }


def fetch_matching_products():
    found = {}
    for term in SEARCH_TERMS:
        for prod in search_products(term):
            s = summarize(prod)
            haystack = " ".join([s["name"], prod.get("description", ""), " ".join(prod.get("categories", []))])
            if matches(haystack):
                found[s["id"]] = s
    return found


def page_mentions():
    """Devuelve {url: [fragmentos]} con menciones de adviento en las páginas vigiladas."""
    out = {}
    for url in WATCH_PAGES:
        try:
            html = http_get(url)
        except (urllib.error.URLError, TimeoutError) as e:
            log(f"  aviso: no pude leer {url} ({e})")
            continue
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        hits = set()
        for m in re.finditer(r"adviento|advent", text, flags=re.I):
            hits.add(text[max(0, m.start() - 40): m.end() + 60].strip())
        if hits:
            out[url] = sorted(hits)[:3]
    return out


# ------------------------------------------------------------- avisos
def notify(message):
    print("\a" + "=" * 60 + f"\n{message}\n" + "=" * 60, flush=True)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        data = urllib.parse.urlencode({"chat_id": chat, "text": message, "disable_web_page_preview": "false"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)
        except Exception as e:  # noqa: BLE001
            log(f"  aviso: Telegram falló ({e})")

    # WhatsApp vía CallMeBot (gratis, a tu propio número)
    wa_phone = os.environ.get("WHATSAPP_PHONE")
    wa_key = os.environ.get("CALLMEBOT_APIKEY")
    if wa_phone and wa_key:
        qs = urllib.parse.urlencode({"phone": wa_phone, "text": message, "apikey": wa_key})
        try:
            urllib.request.urlopen(f"https://api.callmebot.com/whatsapp.php?{qs}", timeout=30)
        except Exception as e:  # noqa: BLE001
            log(f"  aviso: WhatsApp falló ({e})")


# --------------------------------------------------------------- estado
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"products": {}, "pages": {}, "first_run_done": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- lógica
def check_once(state):
    alerts = []
    first = not state.get("first_run_done")

    current = fetch_matching_products()
    prev = state["products"]

    for pid, now in current.items():
        old = prev.get(pid)
        if old is None:
            alerts.append(
                f"🎄 APARECE: {now['name']}\nPrecio: {money(now['price'])} | "
                f"{'DISPONIBLE ✅' if now['available'] else 'aún no disponible'}\n{now['url']}"
            )
        else:
            if now["available"] and not old["available"]:
                alerts.append(f"🛒 YA SE PUEDE COMPRAR: {now['name']}\nPrecio: {money(now['price'])}\n{now['url']}")
            if now["price"] != old["price"] and now["price"] is not None:
                alerts.append(
                    f"💸 CAMBIÓ EL PRECIO: {now['name']}\n{money(old['price'])} → {money(now['price'])}\n{now['url']}"
                )
    state["products"] = {**prev, **current}

    pages = page_mentions()
    for url, hits in pages.items():
        if url not in state["pages"]:
            alerts.append(f"👀 PÁGINA: ahora menciona el adviento\n{url}\n…{hits[0]}…")
    state["pages"] = {**state["pages"], **{u: h for u, h in pages.items()}}

    if first:
        # Primera pasada: línea base. No spamear con lo que ya existía.
        log(f"Línea base guardada: {len(current)} producto(s) coinciden, {len(pages)} página(s) con menciones.")
        for pid, s in current.items():
            log(f"  - {s['name']} | {money(s['price'])} | {'disponible' if s['available'] else 'no disponible'}")
        if not current and not pages:
            log("  Todavía no hay nada del calendario. Te aviso apenas aparezca.")
        state["first_run_done"] = True
        return []
    return alerts


def main():
    ap = argparse.ArgumentParser(description="Vigila el calendario de adviento de Trendy")
    ap.add_argument("--once", action="store_true", help="una sola revisión y salir")
    ap.add_argument("--interval", type=int, default=300, help="segundos entre revisiones (def. 300)")
    ap.add_argument("--reset", action="store_true", help="borra el estado guardado")
    args = ap.parse_args()

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()

    log(f"Vigilando {BASE} (cada {args.interval}s). Ctrl+C para parar.")
    state = load_state()
    while True:
        try:
            alerts = check_once(state)
            save_state(state)
            for a in alerts:
                notify(a)
            if not alerts:
                log("Sin novedades.")
        except Exception as e:  # noqa: BLE001
            log(f"Error en la revisión: {e}")
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
