#!/usr/bin/env python3
"""
poll_gold.py — laeuft alle 15 Minuten als GitHub Actions Workflow, komplett
unabhaengig davon ob irgendein privater Rechner an ist.

Bei jedem Lauf:
  1. Holt den aktuellen Live-Preis (xaus.com, kostenlos, kein Key noetig).
  2. Haengt eine neue "Kerze" fuer das aktuelle 15-Minuten-Fenster an
     gold-data.json an (Open=High=Low=Close=dieser eine Preis-Snapshot,
     da Cron nur alle 15 Min. laeuft - fuer die Zonen-Logik reicht das,
     da nur der Schlusskurs pro Fenster zaehlt).
  3. Fuehrt denselben Zonen-Replay wie die lokale Bridge durch: eine Zone
     bleibt gueltig, bis ein Kerzen-Close sie komplett durchbricht.
  4. Schreibt gold-data.json zurueck - der Workflow committed die Datei.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

DATA_FILE = "gold-data.json"
ZONE_WIDTH = 12.0
M15_MS = 15 * 60 * 1000
MAX_CANDLES = 7 * 24 * 4  # 1 Woche


def fetch_price():
    """Versucht mehrere kostenlose, keyless Quellen der Reihe nach - erst wenn
    ALLE fehlschlagen, wird ein Fehler geworfen. Das haelt den Dienst am Laufen,
    selbst wenn eine einzelne Quelle mal ausfaellt oder sich aendert."""
    errors = []

    # Quelle 1: xaus.com
    try:
        req = urllib.request.Request(
            "https://xaus.com/api/v1/spot",
            headers={"User-Agent": "gold-cloud-poller/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {
            "price": float(data["spot_usd_oz"]),
            "silver": float(data.get("silver_usd_oz", 0)),
            "ratio": float(data.get("gold_silver_ratio", 0)),
        }
    except Exception as e:
        errors.append(f"xaus.com: {e}")

    # Quelle 2 (Fallback): gold-api.com, oeffentlicher Preis-Endpunkt, kein Key noetig
    try:
        req = urllib.request.Request(
            "https://api.gold-api.com/price/XAU",
            headers={"User-Agent": "gold-cloud-poller/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"price": float(data["price"]), "silver": 0.0, "ratio": 0.0}
    except Exception as e:
        errors.append(f"gold-api.com: {e}")

    raise RuntimeError(" | ".join(errors))


def load_state():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"WARNUNG: {DATA_FILE} konnte nicht gelesen werden, starte neu: {e}")
    return {"candles": [], "sessionHigh": None, "sessionLow": None, "firstPrice": None}


def save_state(state):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=None, separators=(",", ":"))


def new_sell_zone(price, t):
    return {"top": round(price + ZONE_WIDTH, 2), "bottom": round(price, 2), "createdAt": t}


def new_buy_zone(price, t):
    return {"top": round(price, 2), "bottom": round(price - ZONE_WIDTH, 2), "createdAt": t}


def replay_zones(candles):
    if not candles:
        return None, None, []
    first = candles[0]
    sz = new_sell_zone(first["o"], first["t"])
    bz = new_buy_zone(first["o"], first["t"])
    hist = []
    for c in candles:
        if sz and c["c"] > sz["top"]:
            hist.append({"type": "sell", "top": sz["top"], "bottom": sz["bottom"], "invalidatedAt": c["t"]})
            sz = new_sell_zone(c["c"], c["t"])
        if bz and c["c"] < bz["bottom"]:
            hist.append({"type": "buy", "top": bz["top"], "bottom": bz["bottom"], "invalidatedAt": c["t"]})
            bz = new_buy_zone(c["c"], c["t"])
    hist.sort(key=lambda h: h["invalidatedAt"], reverse=True)
    return sz, bz, hist[:5]


def main():
    state = load_state()

    try:
        tick = fetch_price()
    except Exception as e:
        # BEIDE Preis-Quellen sind fehlgeschlagen. Trotzdem einen Heartbeat
        # schreiben, damit der Workflow IMMER einen Commit erzeugt - sonst
        # wuerde GitHub den geplanten Job nach 60 Tagen ohne Commit-Aktivitaet
        # automatisch deaktivieren. Preis/Zonen bleiben unveraendert, nur ein
        # Zeitstempel + Fehlermeldung wird aktualisiert.
        print(f"FEHLER: alle Preis-Quellen fehlgeschlagen: {e}", file=sys.stderr)
        state["lastPollError"] = str(e)
        state["lastPollErrorAt"] = datetime.now(timezone.utc).isoformat()
        state["heartbeat"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        sys.exit(0)  # kein harter Fehler - Workflow soll trotzdem committen

    price = tick["price"]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    bucket_start = (now_ms // M15_MS) * M15_MS

    candles = state.get("candles") or []

    if candles and candles[-1]["t"] == bucket_start:
        # Zweiter Lauf im selben 15-Min-Fenster (z.B. bei manuellem Re-Run) -
        # Close aktualisieren statt Duplikat anzuhaengen.
        c = candles[-1]
        c["h"] = max(c["h"], price)
        c["l"] = min(c["l"], price)
        c["c"] = price
    else:
        candles.append({"t": bucket_start, "o": price, "h": price, "l": price, "c": price})

    candles = candles[-MAX_CANDLES:]

    if state.get("firstPrice") is None:
        state["firstPrice"] = price
        state["sessionHigh"] = price
        state["sessionLow"] = price
    state["sessionHigh"] = max(state["sessionHigh"], price)
    state["sessionLow"] = min(state["sessionLow"], price)

    sell_zone, buy_zone, history = replay_zones(candles)

    state["candles"] = candles
    state["sellZone"] = sell_zone
    state["buyZone"] = buy_zone
    state["history"] = history
    state["lastPrice"] = price
    state["lastSilver"] = tick["silver"]
    state["lastRatio"] = tick["ratio"]
    state["lastUpdatedAt"] = datetime.now(timezone.utc).isoformat()
    state["heartbeat"] = datetime.now(timezone.utc).isoformat()
    state["lastPollError"] = None

    save_state(state)
    print(f"OK: Preis {price} gespeichert, {len(candles)} Kerzen, "
          f"SellZone={sell_zone}, BuyZone={buy_zone}")


if __name__ == "__main__":
    main()
