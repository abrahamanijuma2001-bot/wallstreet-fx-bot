"""
WallStreet FX Signal Bot - GBPUSD
Kaduma_fx | CHOOSE THE PAIN
Stack: Finnhub API + Flask + Telegram
Hosted: Render.com (FREE)
"""

import os
import time
import threading
import requests
from datetime import datetime, timezone
from flask import Flask, jsonify

# ═══════════════════════════════════════
#              FLASK APP (Render keepalive)
# ═══════════════════════════════════════
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "bot": "WallStreet FX Signal Bot",
        "pair": "GBPUSD",
        "brand": "Kaduma_fx | CHOOSE THE PAIN",
        "status": "running",
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S GMT")
    })

@app.route('/status')
def status():
    return jsonify({
        "signals_sent": bot_state["signals_sent"],
        "last_signal": bot_state["last_signal_time"],
        "in_killzone": is_in_killzone()[0],
        "session": is_in_killzone()[1]
    })

# ═══════════════════════════════════════
#              CONFIGURATION
# ═══════════════════════════════════════
FINNHUB_KEY    = os.environ.get("FINNHUB_API_KEY", "")
TG_TOKEN       = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOL         = "OANDA:GBP_USD"
ACCOUNT_BAL    = float(os.environ.get("ACCOUNT_BALANCE", "100"))
RISK_PCT       = float(os.environ.get("RISK_PERCENT", "2"))
RR_RATIO       = 2.0
CHECK_INTERVAL = 900   # Check every 15 minutes
SIGNAL_COOLDOWN= 3600  # 1 hour between signals

# Killzones (GMT hours)
LONDON_OPEN  = 7;  LONDON_CLOSE  = 11
NY_OPEN      = 13; NY_CLOSE      = 17

# Bot state tracker
bot_state = {
    "signals_sent": 0,
    "last_signal_time": "Never",
    "last_check": "Never"
}

# ═══════════════════════════════════════
#              DATA FETCHING
# ═══════════════════════════════════════
def get_candles(resolution="15", count=80):
    """Fetch GBPUSD candles from Finnhub (FREE, no credit card)"""
    now      = int(time.time())
    secs     = int(resolution) * 60 if resolution != "D" else 86400
    from_t   = now - (count * secs * 2)  # Extra buffer

    url = "https://finnhub.io/api/v1/forex/candle"
    params = {
        "symbol": SYMBOL,
        "resolution": resolution,
        "from": from_t,
        "to": now,
        "token": FINNHUB_KEY
    }

    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()

        if data.get("s") != "ok":
            print(f"Finnhub returned: {data.get('s')} for resolution {resolution}")
            return []

        candles = []
        for i in range(len(data["c"])):
            candles.append({
                "open":   data["o"][i],
                "high":   data["h"][i],
                "low":    data["l"][i],
                "close":  data["c"][i],
                "time":   data["t"][i]
            })
        return candles

    except Exception as e:
        print(f"[Finnhub Error] {e}")
        return []

# ═══════════════════════════════════════
#              SESSION CHECK
# ═══════════════════════════════════════
def is_in_killzone():
    h = datetime.now(timezone.utc).hour
    if LONDON_OPEN <= h < LONDON_CLOSE:
        return True, "🇬🇧 LONDON"
    if NY_OPEN <= h < NY_CLOSE:
        return True, "🇺🇸 NEW YORK"
    return False, ""

# ═══════════════════════════════════════
#              EMA CALCULATION
# ═══════════════════════════════════════
def calc_ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema

def get_trend(candles_h1):
    """H1 EMA50 trend bias"""
    if len(candles_h1) < 55:
        return 0
    closes = [c["close"] for c in candles_h1]
    ema50  = calc_ema(closes, 50)
    if ema50 is None:
        return 0
    price = closes[-1]
    prev  = closes[-2]

    # EMA must also be sloping
    ema50_prev = calc_ema(closes[:-1], 50)
    if ema50_prev is None:
        return 0

    if price > ema50 and ema50 > ema50_prev:
        return 1   # Bullish
    if price < ema50 and ema50 < ema50_prev:
        return -1  # Bearish
    return 0

# ═══════════════════════════════════════
#              MSS DETECTION
# ═══════════════════════════════════════
def detect_mss(candles, direction):
    """Market Structure Shift on M15"""
    if len(candles) < 12:
        return False

    curr = candles[-1]
    body = abs(curr["close"] - curr["open"])
    rng  = curr["high"] - curr["low"]
    strong_candle = rng > 0 and (body / rng) > 0.5

    if direction == 1:  # Bullish MSS
        swing_high = max(c["high"] for c in candles[-10:-2])
        broke = curr["close"] > swing_high
        bullish_candle = curr["close"] > curr["open"]
        return broke and strong_candle and bullish_candle

    if direction == -1:  # Bearish MSS
        swing_low = min(c["low"] for c in candles[-10:-2])
        broke = curr["close"] < swing_low
        bearish_candle = curr["close"] < curr["open"]
        return broke and strong_candle and bearish_candle

    return False

# ═══════════════════════════════════════
#              FVG DETECTION
# ═══════════════════════════════════════
def detect_fvg(candles, direction):
    """Fair Value Gap — price retesting imbalance zone"""
    if len(candles) < 6:
        return False, 0, 0

    price = candles[-1]["close"]

    for i in range(1, min(8, len(candles) - 2)):
        idx  = -(i + 1)
        prev = -(i - 1) if i > 1 else -1

        try:
            if direction == 1:
                fvg_low  = candles[-(i + 2)]["high"]
                fvg_high = candles[-i]["low"]
                if fvg_low < fvg_high:
                    if fvg_low <= price <= fvg_high:
                        return True, round(fvg_low, 5), round(fvg_high, 5)

            if direction == -1:
                fvg_high = candles[-(i + 2)]["low"]
                fvg_low  = candles[-i]["high"]
                if fvg_low < fvg_high:
                    if fvg_low <= price <= fvg_high:
                        return True, round(fvg_low, 5), round(fvg_high, 5)
        except IndexError:
            break

    return False, 0, 0

# ═══════════════════════════════════════
#              ORDER BLOCK DETECTION
# ═══════════════════════════════════════
def detect_ob(candles, direction):
    """Order Block — last opposing candle before strong impulse"""
    if len(candles) < 8:
        return False, 0, 0

    price = candles[-1]["close"]

    for i in range(2, min(14, len(candles) - 1)):
        try:
            c    = candles[-(i + 1)]
            nxt  = candles[-i]

            if direction == 1:  # Bullish OB
                is_bearish  = c["close"] < c["open"]
                strong_push = nxt["close"] > c["high"]
                if is_bearish and strong_push:
                    if c["low"] <= price <= c["high"]:
                        return True, round(c["high"], 5), round(c["low"], 5)

            if direction == -1:  # Bearish OB
                is_bullish  = c["close"] > c["open"]
                strong_drop = nxt["close"] < c["low"]
                if is_bullish and strong_drop:
                    if c["low"] <= price <= c["high"]:
                        return True, round(c["high"], 5), round(c["low"], 5)
        except IndexError:
            break

    return False, 0, 0

# ═══════════════════════════════════════
#              LOT SIZE
# ═══════════════════════════════════════
def calc_lot(sl_distance):
    """Dynamic lot based on risk % — safe for $100 account"""
    if sl_distance <= 0:
        return 0.01
    risk_amount = ACCOUNT_BAL * RISK_PCT / 100.0
    pip_value   = 10.0  # GBPUSD: $10 per pip per standard lot
    pips        = sl_distance / 0.0001
    lot         = risk_amount / (pips * pip_value)
    lot         = round(max(0.01, min(0.05, lot)), 2)
    return lot

# ═══════════════════════════════════════
#              TELEGRAM
# ═══════════════════════════════════════
def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[Telegram] No token/chat_id configured!")
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        r = requests.post(url, data={
            "chat_id": TG_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"[Telegram Error] {e}")
        return False

def format_signal(direction, entry, sl, tp, lot, session, confirmations, sl_pips):
    """Format beautiful Telegram signal message"""
    emoji   = "🟢" if direction == 1 else "🔴"
    dir_txt = "BUY  📈" if direction == 1 else "SELL 📉"
    tp_pips = round(sl_pips * RR_RATIO)
    risk_usd   = round(ACCOUNT_BAL * RISK_PCT / 100, 2)
    reward_usd = round(risk_usd * RR_RATIO, 2)
    now = datetime.now(timezone.utc).strftime("%H:%M GMT")
    conf_txt = " | ".join(confirmations)

    return f"""{emoji}<b>WALLSTREET FX — SIGNAL</b>{emoji}
<b>━━━━━━━━━━━━━━━━━━━━━━━</b>
🏦 <b>Kaduma_fx | CHOOSE THE PAIN</b>
<b>━━━━━━━━━━━━━━━━━━━━━━━</b>
📊 Pair    : <b>GBPUSD</b>
📌 Action  : <b>{dir_txt}</b>
🕐 Session : <b>{session}</b> | {now}
<b>━━━━━━━━━━━━━━━━━━━━━━━</b>
📍 Entry   : <code>{entry:.5f}</code>
🛑 SL      : <code>{sl:.5f}</code>  ({sl_pips} pips)
🎯 TP      : <code>{tp:.5f}</code>  ({tp_pips} pips)
<b>━━━━━━━━━━━━━━━━━━━━━━━</b>
📦 Lot Size : <b>{lot}</b>
💸 Risk     : <b>-${risk_usd}</b>
💰 Target   : <b>+${reward_usd}</b>
📈 RR Ratio : <b>1:{RR_RATIO}</b>
<b>━━━━━━━━━━━━━━━━━━━━━━━</b>
🔍 <b>Confirmations:</b>
<code>{conf_txt}</code>
<b>━━━━━━━━━━━━━━━━━━━━━━━</b>
⚠️ <i>Always manage your risk. Trade responsibly.</i>"""

# ═══════════════════════════════════════
#              MAIN ANALYSIS
# ═══════════════════════════════════════
def analyze():
    """Core analysis — runs every 15 minutes in killzone"""
    in_kz, session = is_in_killzone()
    if not in_kz:
        return None

    print(f"[{datetime.now(timezone.utc).strftime('%H:%M')}] Analyzing GBPUSD in {session}...")

    # Fetch data
    candles_m15 = get_candles("15",  80)
    candles_h1  = get_candles("60",  60)

    if len(candles_m15) < 20 or len(candles_h1) < 55:
        print("  Not enough data")
        return None

    # Step 1: H1 Trend
    trend = get_trend(candles_h1)
    if trend == 0:
        print("  No clear H1 trend")
        return None

    direction = trend

    # Step 2: MSS
    mss = detect_mss(candles_m15, direction)
    if not mss:
        print(f"  No MSS for {'BUY' if direction==1 else 'SELL'}")
        return None

    # Step 3: FVG
    fvg, fvg_low, fvg_high = detect_fvg(candles_m15, direction)

    # Step 4: OB
    ob, ob_high, ob_low = detect_ob(candles_m15, direction)

    # Need at least one confirmation
    if not fvg and not ob:
        print("  No FVG or OB confirmation")
        return None

    # Build setup
    price = candles_m15[-1]["close"]
    confirmations = ["✅ MSS"]
    if fvg: confirmations.append("✅ FVG")
    if ob:  confirmations.append("✅ OB")

    if direction == 1:  # BUY
        sl_ref = ob_low if ob else (price - 0.0020)
        entry  = price
        sl     = round(sl_ref - 0.0005, 5)
        sl_d   = entry - sl
        tp     = round(entry + sl_d * RR_RATIO, 5)
    else:  # SELL
        sl_ref = ob_high if ob else (price + 0.0020)
        entry  = price
        sl     = round(sl_ref + 0.0005, 5)
        sl_d   = sl - entry
        tp     = round(entry - sl_d * RR_RATIO, 5)

    # Validate SL distance (5-50 pips for GBPUSD scalping)
    sl_pips = round(sl_d / 0.0001)
    if sl_pips < 5 or sl_pips > 50:
        print(f"  SL distance invalid: {sl_pips} pips")
        return None

    lot = calc_lot(sl_d)

    return {
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "lot": lot,
        "session": session,
        "confirmations": confirmations,
        "sl_pips": sl_pips
    }

# ═══════════════════════════════════════
#              BOT MAIN LOOP
# ═══════════════════════════════════════
last_signal_ts = 0

def bot_loop():
    global last_signal_ts

    print("=" * 50)
    print(" WallStreet FX Signal Bot — STARTING")
    print(" GBPUSD | Kaduma_fx | CHOOSE THE PAIN")
    print("=" * 50)

    send_telegram(
        "🤖 <b>WallStreet FX Signal Bot — ONLINE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 Pair    : <b>GBPUSD</b>\n"
        f"💰 Capital : <b>${ACCOUNT_BAL}</b>\n"
        f"⚡ Risk    : <b>{RISK_PCT}% per trade</b>\n"
        f"📈 RR      : <b>1:{RR_RATIO}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 Monitoring London & NY Killzones\n"
        "<i>Kaduma_fx | CHOOSE THE PAIN</i>"
    )

    while True:
        try:
            now = time.time()
            bot_state["last_check"] = datetime.now(timezone.utc).strftime("%H:%M GMT")

            in_kz, _ = is_in_killzone()
            cooldown_ok = (now - last_signal_ts) > SIGNAL_COOLDOWN

            if in_kz and cooldown_ok:
                setup = analyze()
                if setup:
                    msg = format_signal(
                        setup["direction"],
                        setup["entry"],
                        setup["sl"],
                        setup["tp"],
                        setup["lot"],
                        setup["session"],
                        setup["confirmations"],
                        setup["sl_pips"]
                    )
                    if send_telegram(msg):
                        last_signal_ts = now
                        bot_state["signals_sent"] += 1
                        bot_state["last_signal_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
                        print(f"  ✅ Signal sent! Total: {bot_state['signals_sent']}")
                    else:
                        print("  ❌ Failed to send Telegram message")
            else:
                h = datetime.now(timezone.utc).hour
                print(f"  [{datetime.now(timezone.utc).strftime('%H:%M')}] Waiting... KZ:{in_kz} | GMT hour:{h}")

        except Exception as e:
            print(f"[Bot Error] {e}")

        time.sleep(CHECK_INTERVAL)


# ═══════════════════════════════════════
#              ENTRY POINT
# ═══════════════════════════════════════
if __name__ == "__main__":
    if not FINNHUB_KEY:
        print("⚠️  WARNING: FINNHUB_API_KEY not set in environment variables!")
    if not TG_TOKEN:
        print("⚠️  WARNING: TELEGRAM_TOKEN not set in environment variables!")
    if not TG_CHAT_ID:
        print("⚠️  WARNING: TELEGRAM_CHAT_ID not set in environment variables!")

    # Start bot in background thread
    thread = threading.Thread(target=bot_loop, daemon=True)
    thread.start()

    # Start Flask server (Render.com requires this)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
