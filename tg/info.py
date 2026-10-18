import time
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

TELEGRAM = True
IST = ZoneInfo("Asia/Kolkata")

NT_SSR_SECRET = "c3a5b7d9e1f02a4c6b8d0f1e3a5c7d9b2e4a6f8c0b1d3e5a7f9c2b4d6e8a0f1c3b5d7e9a1f2b4c6d8e0f1a3b5c7d9e2f4a6b8c0d1e3f5a7b9c2d4e6f8a0b"

def fetch_nt_api_key():
    resp = requests.get(
        "https://onboarding.niftytrader.in/api/api-key",
        headers={
            "User-Agent": "Mozilla/5.0",
            "accept": "application/json",
            "X-SSR-Secret": NT_SSR_SECRET,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    result = data.get("resultData") if isinstance(data, dict) else None
    key = result.get("apiKey") if isinstance(result, dict) else None
    if not isinstance(key, str) or not key:
        raise ValueError("NT API-key response did not contain an apiKey")
    return key

def fetch_nt_total():
    url     = "https://webapi.niftytrader.in/webapi/option/option-chain-data?symbol=nifty&exchange=nse&expiryDate=&atmBelow=5&atmAbove=5"
    headers = {"User-Agent": "Mozilla/5.0", "accept": "application/json"}
    try:
        headers["x-api-key"] = fetch_nt_api_key()
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if not data or not isinstance(data, dict):
            print("❌ NT bad response"); return None
        return data
    except Exception as e:
        print(f"❌ NT fetch error: {e}"); return None

def get_day_change():
    url     = "https://webapi.niftytrader.in/webapi/symbol/today-spot-data?symbol=nifty&created_at="
    headers = {"User-Agent": "Mozilla/5.0", "accept": "application/json"}
    try:
        headers["x-api-key"] = fetch_nt_api_key()
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        result = data.get("resultData") or {}
        return result.get("last_trade_price"), result.get("change_value")
    except Exception as e:
        print(f"❌ Change fetch error: {e}"); return None

def fetch_nt_expiry():
    created_time = (datetime.now(IST) - timedelta(minutes=2)).strftime("%H:%M:%S")
    url = f"https://webapi.niftytrader.in/webapi/Option/option-chain-calculator-data?symbol=nifty&expiryDate=&createdTime={created_time}&isloader=false&atmBelow=5&atmAbove=5"
    try:
        headers = {"User-Agent": "Mozilla/5.0", "accept": "application/json",
                   "x-api-key": fetch_nt_api_key()}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not data:
            raise ValueError("Invalid expiry response")
        return data
    except Exception as error:
        print("❌ NT expiry fetch error:", type(error).__name__)
        return None

def get_futures_symbol(data, today=None):
    today = today or datetime.now(IST).date()
    dates = [datetime.fromisoformat(value.replace("Z", "+00:00")).date()
             for value in data["resultData"]["opExpiryDates"]]
    current = [d for d in dates if (d.year, d.month) == (today.year, today.month)]
    # Keep this month's future through its last expiry date.
    if current and today <= max(current):
        month = today
    else:
        month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        if not any((d.year, d.month) == (month.year, month.month) for d in dates):
            raise ValueError("Required next-month expiry missing from API")
    months = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
    return f"NIFTY{month.year % 100:02d}{months[month.month-1]}FUT"

def fetch_orderflow():
    try:
        expiry_data = fetch_nt_expiry()
        if expiry_data is None:
            return None
        symbol = get_futures_symbol(expiry_data)
        url = f"https://signalr-v2.niftytrader.in/api/v1/footprint/{symbol}"
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "accept": "application/json"}, timeout=10)
        response.raise_for_status()
        bars = response.json().get("bars", [])
        if not bars:
            print("⚠️ Orderflow bars missing")
            return None
        bars = [bar for bar in bars if isinstance(bar, dict) and bar.get("ts")]
        if not bars:
            return None
        latest = max(bars, key=lambda bar: datetime.fromisoformat(bar["ts"]))
        return latest["ts"], latest.get("cvd")
    except Exception as error:
        print("❌ Orderflow fetch error:", type(error).__name__)
        return None

def format_output(ts, coi_pcr, change_pts, cvd):
    try:
        time_str = datetime.fromisoformat(ts).strftime("%H:%M")
    except (TypeError, ValueError):
        time_str = "--"

    def display(value, thousands=False):
        try:
            value = float(value)
            if not math.isfinite(value):
                return "--"
        except (TypeError, ValueError, OverflowError):
            return "--"
        if thousands:
            return f"{round(value / 1000, 1)}K{'🟢' if value > 0 else '🔴'}"
        return f"{value} {'🟢' if value >= 0 else '🔴'}"

    message = f"🕒{time_str} | {display(change_pts)} | {display(cvd, True)} | {display(coi_pcr, True)}"
    print(message)
    return send_telegram_message(message, False)

def send_telegram_message(msg, imp=True):
    if not TELEGRAM:
        return False
    BOT_TOKEN      = "8331147432:AAGSG4mI8d87sWEBsY0qtarAtwWbpa4viq0"
    CHANNEL_ID     = "-1003494200670"
    CHANNEL_ID_IMP = "-1003448158591"
    chat_id        = CHANNEL_ID_IMP if imp else CHANNEL_ID
    url            = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(url, data={"chat_id": chat_id, "text": msg}, timeout=15)
        response.raise_for_status()
        if response.json().get("ok") is not True:
            print("❌ Telegram rejected message")
            return False
        return True
    except Exception as e:
        # Do not print exception URLs: they contain the bot token.
        print("❌ Telegram error:", type(e).__name__)
        return False

if __name__ == "__main__":
    last_minute = None
    while True:
        started = time.monotonic()
        try:
            option_chain = fetch_nt_total()
            spot = get_day_change()
            orderflow = fetch_orderflow()
            ts, cvd = orderflow if orderflow else (None, None)
            # If orderflow is unavailable, still send the other values this minute.
            try:
                dt = datetime.fromisoformat(ts).astimezone(IST)
            except (TypeError, ValueError):
                dt = datetime.now(IST)
            minute = dt.strftime("%Y-%m-%d %H:%M")
            if last_minute is None or minute > last_minute:
                try:
                    totals = option_chain["resultData"]["opTotals"]["total_calls_puts"]
                    coi = float(totals["total_puts_change_oi"]) - float(totals["total_calls_change_oi"])
                except (TypeError, ValueError, KeyError, OverflowError):
                    coi = None
                _, change = spot if spot else (None, None)
                last_minute = minute
                format_output(dt.isoformat(), coi, change, cvd)
        except Exception as error:
            print("Fetch failed:", type(error).__name__)
        time.sleep(max(0, 10 - (time.monotonic() - started)))
