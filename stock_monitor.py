#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stock monitor for bullsportsdirect.com  (variation-aware)
---------------------------------------------------------
Scans ALL products via the WooCommerce Store API. For products with
variations (e.g. color / strap / size) it tracks EACH variation
separately, with its exact stock quantity ("1 in stock").

On each run it alerts via Telegram on:
  - a variation that went OUT-OF-STOCK -> IN-STOCK
  - any change in quantity (e.g. 2 -> 1, or 0 -> 3)

It does NOT place orders. Monitoring + alerts only.

First run prints a FULL report of every product/variation and its
quantity, and saves the baseline.

Run on a schedule (cron / Task Scheduler), e.g. every few hours.
Optional flag:  --report   -> print the full current report any time.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

# ============================================================
# CONFIG — edit these
# ============================================================
BASE_URL   = "https://www.bullsportsdirect.com"
PRODUCTS_PATH   = "/wp-json/wc/store/v1/products"
PER_PAGE   = 100
STATE_FILE = "stock_state.json"
REQUEST_DELAY = 1.0          # seconds between requests (be polite to the site)

# --- Telegram alerts ---
# 1) Create a bot via @BotFather -> get the token.
# 2) Get your chat id via @userinfobot.
TELEGRAM_ENABLED  = True
TELEGRAM_TOKEN    = "PUT_YOUR_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID  = "PUT_YOUR_CHAT_ID_HERE"
# ============================================================


def _get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (stock-monitor)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_product_pages():
    """Yield every product (summary) from the Store API, page by page."""
    page = 1
    while True:
        url = f"{BASE_URL}{PRODUCTS_PATH}?per_page={PER_PAGE}&page={page}"
        try:
            data = _get_json(url)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):   # past the last page
                break
            print(f"HTTP error page {page}: {e}", file=sys.stderr)
            break
        except Exception as e:
            print(f"Error page {page}: {e}", file=sys.stderr)
            break
        if not data:
            break
        for p in data:
            yield p
        page += 1
        time.sleep(REQUEST_DELAY)


def fetch_variations(product_id):
    """Return list of variations for a variable product, with quantities."""
    url = (f"{BASE_URL}{PRODUCTS_PATH}/{product_id}"
           f"?per_page={PER_PAGE}")
    # The single-product endpoint includes a 'variations' list with ids.
    try:
        product = _get_json(url)
    except Exception as e:
        print(f"Error fetching product {product_id}: {e}", file=sys.stderr)
        return []

    variations = product.get("variations") or []
    results = []
    for v in variations:
        vid = v.get("id")
        if not vid:
            continue
        vurl = f"{BASE_URL}{PRODUCTS_PATH}/{vid}"
        try:
            vd = _get_json(vurl)
        except Exception:
            continue
        # Build a readable label from the variation attributes.
        attrs = vd.get("attributes") or []
        label_parts = []
        for a in attrs:
            val = a.get("value") or a.get("name") or ""
            if val:
                label_parts.append(str(val))
        label = " / ".join(label_parts) if label_parts else f"variation {vid}"
        results.append({
            "id": str(vid),
            "label": label,
            "in_stock": bool(vd.get("is_in_stock", False)),
            "qty": vd.get("stock_quantity"),  # int or None if hidden
        })
        time.sleep(REQUEST_DELAY)
    return results


def fetch_all():
    """
    Build a flat dict keyed by a unique stock-unit id.
    Simple products       -> key "p<id>"
    Variation stock-units  -> key "p<id>:v<vid>"
    """
    units = {}
    for p in fetch_product_pages():
        pid = str(p.get("id"))
        name = (p.get("name") or "").strip()
        permalink = p.get("permalink", "")
        has_variations = bool(p.get("variations"))

        if not has_variations:
            units[f"p{pid}"] = {
                "name": name,
                "label": "",
                "permalink": permalink,
                "in_stock": bool(p.get("is_in_stock", False)),
                "qty": p.get("stock_quantity"),
            }
        else:
            for v in fetch_variations(pid):
                units[f"p{pid}:v{v['id']}"] = {
                    "name": name,
                    "label": v["label"],
                    "permalink": permalink,
                    "in_stock": v["in_stock"],
                    "qty": v["qty"],
                }
    return units


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(units):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(units, f, ensure_ascii=False, indent=2)


def qty_str(unit):
    if unit["qty"] is not None:
        return f"{unit['qty']} in stock"
    return "in stock" if unit["in_stock"] else "out of stock"


def full_report(units):
    lines = [f"Full stock report — {len(units)} stock-units\n"]
    # group by product name for readability
    by_name = {}
    for u in units.values():
        by_name.setdefault(u["name"], []).append(u)
    for name in sorted(by_name):
        lines.append(f"\n{name}")
        for u in by_name[name]:
            tag = f"  - {u['label']}: " if u["label"] else "  - "
            lines.append(f"{tag}{qty_str(u)}")
    return "\n".join(lines)


def send_telegram(message):
    if not TELEGRAM_ENABLED:
        return
    # Telegram messages cap ~4096 chars; chunk if needed.
    for chunk in [message[i:i+3500] for i in range(0, len(message), 3500)]:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        try:
            urllib.request.urlopen(url, data=payload, timeout=30)
        except Exception as e:
            print(f"Telegram send failed: {e}", file=sys.stderr)


def main():
    report_only = "--report" in sys.argv

    current = fetch_all()
    if not current:
        print("No products fetched — network issue or API changed/blocked.",
              file=sys.stderr)
        sys.exit(1)

    if report_only:
        print(full_report(current))
        return

    previous = load_state()

    # First run: print + send the full report, save baseline, no diff alerts.
    if previous is None:
        report = full_report(current)
        print(report)
        send_telegram("📋 Baseline report\n\n" + report)
        save_state(current)
        return

    restocked = []      # out -> in
    qty_changed = []    # quantity changed (and still in stock or to 0)

    for key, info in current.items():
        was = previous.get(key)
        if was is None:
            continue  # brand-new unit; skip alerting, will baseline it
        # restock transition
        if (not was["in_stock"]) and info["in_stock"]:
            restocked.append(info)
        # quantity change (only meaningful when site exposes numbers)
        elif (was.get("qty") is not None and info.get("qty") is not None
              and was["qty"] != info["qty"]):
            qty_changed.append((was["qty"], info))

    save_state(current)

    if not restocked and not qty_changed:
        print(f"Checked {len(current)} stock-units. No changes.")
        return

    lines = []
    if restocked:
        lines.append("🔔 Back in stock:")
        for u in restocked:
            label = f" [{u['label']}]" if u["label"] else ""
            lines.append(f"• {u['name']}{label} — {qty_str(u)}\n  {u['permalink']}")
        lines.append("")
    if qty_changed:
        lines.append("🔄 Quantity changed:")
        for old_qty, u in qty_changed:
            label = f" [{u['label']}]" if u["label"] else ""
            lines.append(f"• {u['name']}{label}: {old_qty} → {u['qty']}\n  {u['permalink']}")

    message = "\n".join(lines)
    print(message)
    send_telegram(message)


if __name__ == "__main__":
    main()
