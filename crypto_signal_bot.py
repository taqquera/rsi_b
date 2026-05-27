"""
Форекс сигнальний бот для Telegram v4.0
Стратегія: тільки RSI + пам'ять сигналів (без дублікатів)
Пари: EUR/USD, GBP/USD, USD/JPY, XAU/USD
Автор: твій фінансовий коуч (Claude)

Дані: безкоштовний API від exchangerate.host + yfinance для золота
Сигнали надсилаються в Telegram — торгуєш вручну на FxPro

ВСТАНОВЛЕННЯ:
pip install pandas python-telegram-bot schedule yfinance requests

НАЛАШТУВАННЯ:
1. Створи бота через @BotFather -> отримай TELEGRAM_TOKEN
2. Дізнайся свій CHAT_ID через @userinfobot
3. Заповни змінні нижче
"""

import pandas as pd
import schedule
import time
import asyncio
import requests
import yfinance as yf
from telegram import Bot
from datetime import datetime, timedelta

# ============================================================
# НАЛАШТУВАННЯ — заповни свої дані тут
# ============================================================

TELEGRAM_TOKEN = "8886661285:AAF6p7w_BR4WIHo2oVrEhxi1pDqroXOilSA"
CHAT_ID        = "-5103360859"

# Форекс пари — тікери для yfinance
SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "XAU/USD": "GC=F",      # золото (ф'ючерс)
}

TIMEFRAME   = "1h"    # таймфрейм
CHECK_EVERY = 60      # перевіряти кожні N хвилин (форекс рухається повільніше)

# Параметри RSI
RSI_PERIOD     = 14
RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70
RSI_RESET_LOW  = 40   # скидання сигналу КУПУЙ
RSI_RESET_HIGH = 60   # скидання сигналу ПРОДАВАЙ

# Параметри ATR
ATR_PERIOD  = 14
ATR_TP_MULT = 2.0
ATR_SL_MULT = 1.0

# ============================================================
# ПАМ'ЯТЬ СИГНАЛІВ
# ============================================================
last_signal = {symbol: None for symbol in SYMBOLS.keys()}


# ============================================================
# ОТРИМАННЯ ДАНИХ ЧЕРЕЗ YFINANCE
# ============================================================

def get_candles(symbol_name: str, ticker: str, period: str = "7d", interval: str = "1h") -> pd.DataFrame:
    """
    Отримує свічки через yfinance.
    Безкоштовно, без API ключів, підтримує форекс і золото.
    """
    data = yf.download(ticker, period=period, interval=interval, progress=False)
    if data.empty:
        raise ValueError(f"Немає даних для {symbol_name}")

    df = data[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    df.index.name = "timestamp"
    df = df.reset_index()
    return df


# ============================================================
# РОЗРАХУНОК ІНДИКАТОРІВ
# ============================================================

def calculate_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    tr1   = high - low
    tr2   = (high - close.shift()).abs()
    tr3   = (low  - close.shift()).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(com=period - 1, min_periods=period).mean()


def analyze(symbol_name: str, ticker: str) -> dict:
    df     = get_candles(symbol_name, ticker)
    closes = df["close"]

    df["rsi"] = calculate_rsi(closes, RSI_PERIOD)
    df["atr"] = calculate_atr(df, ATR_PERIOD)

    last = df.iloc[-1]

    current_price = float(last["close"])
    current_rsi   = float(last["rsi"])
    current_atr   = float(last["atr"])
    atr_pct       = (current_atr / current_price) * 100

    signal = "НЕЙТРАЛЬНО"
    reason = []

    if current_rsi < RSI_OVERSOLD:
        signal = "🟢 КУПУЙ"
        reason.append(f"RSI перепроданий ({current_rsi:.1f} < {RSI_OVERSOLD})")
    elif current_rsi > RSI_OVERBOUGHT:
        signal = "🔴 ПРОДАВАЙ"
        reason.append(f"RSI перекуплений ({current_rsi:.1f} > {RSI_OVERBOUGHT})")

    if signal == "🟢 КУПУЙ":
        take_profit = current_price + (current_atr * ATR_TP_MULT)
        stop_loss   = current_price - (current_atr * ATR_SL_MULT)
    elif signal == "🔴 ПРОДАВАЙ":
        take_profit = current_price - (current_atr * ATR_TP_MULT)
        stop_loss   = current_price + (current_atr * ATR_SL_MULT)
    else:
        take_profit = None
        stop_loss   = None

    tp_pct = ((take_profit - current_price) / current_price * 100) if take_profit else None
    sl_pct = ((stop_loss  - current_price) / current_price * 100) if stop_loss  else None

    return {
        "symbol":      symbol_name,
        "price":       current_price,
        "rsi":         current_rsi,
        "atr":         current_atr,
        "atr_pct":     atr_pct,
        "signal":      signal,
        "reasons":     reason,
        "take_profit": take_profit,
        "stop_loss":   stop_loss,
        "tp_pct":      tp_pct,
        "sl_pct":      sl_pct,
        "timestamp":   last["timestamp"],
    }


# ============================================================
# ПАМ'ЯТЬ — перевірка чи надсилати сигнал
# ============================================================

def should_send(symbol: str, new_signal: str, current_rsi: float) -> bool:
    global last_signal

    if new_signal == "НЕЙТРАЛЬНО":
        return False

    prev = last_signal[symbol]

    if prev == "🟢 КУПУЙ" and current_rsi > RSI_RESET_LOW:
        last_signal[symbol] = None
        prev = None
        print(f"  🔄 {symbol}: КУПУЙ скинуто (RSI {current_rsi:.1f})")

    if prev == "🔴 ПРОДАВАЙ" and current_rsi < RSI_RESET_HIGH:
        last_signal[symbol] = None
        prev = None
        print(f"  🔄 {symbol}: ПРОДАВАЙ скинуто (RSI {current_rsi:.1f})")

    if prev == new_signal:
        return False

    return True


# ============================================================
# ФОРМУВАННЯ ПОВІДОМЛЕННЯ
# ============================================================

def format_message(data: dict) -> str:
    # Форматування ціни залежно від пари
    if data["symbol"] == "USD/JPY":
        price_fmt = f"{data['price']:.3f}"
        tp_fmt    = f"{data['take_profit']:.3f}"
        sl_fmt    = f"{data['stop_loss']:.3f}"
        atr_fmt   = f"{data['atr']:.4f}"
    elif data["symbol"] == "XAU/USD":
        price_fmt = f"${data['price']:,.2f}"
        tp_fmt    = f"${data['take_profit']:,.2f}"
        sl_fmt    = f"${data['stop_loss']:,.2f}"
        atr_fmt   = f"${data['atr']:.2f}"
    else:
        price_fmt = f"{data['price']:.5f}"
        tp_fmt    = f"{data['take_profit']:.5f}"
        sl_fmt    = f"{data['stop_loss']:.5f}"
        atr_fmt   = f"{data['atr']:.5f}"

    reasons_text = "\n".join(f"  • {r}" for r in data["reasons"])
    time_str = data["timestamp"].strftime("%H:%M %d.%m.%Y") if hasattr(data["timestamp"], "strftime") else str(data["timestamp"])

    return (
        f"📊 *{data['symbol']}* | {TIMEFRAME}\n"
        f"🕐 {time_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Ціна входу: *{price_fmt}*\n"
        f"📈 RSI ({RSI_PERIOD}): `{data['rsi']:.1f}`\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Сигнал: *{data['signal']}*\n"
        f"Причини:\n{reasons_text}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📐 ATR: `{atr_fmt}` ({data['atr_pct']:.3f}% волатильність)\n"
        f"🎯 Тейк профіт: *{tp_fmt}* ({data['tp_pct']:+.3f}%)\n"
        f"🛑 Стоп лос:    *{sl_fmt}* ({data['sl_pct']:+.3f}%)\n"
        f"📊 Ризик/прибуток: 1:{ATR_TP_MULT/ATR_SL_MULT:.0f}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⚠️ _Це не фінансова порада. Торгуй на FxPro самостійно._"
    )


async def send_telegram(message: str) -> None:
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")


# ============================================================
# ГОЛОВНИЙ ЦИКЛ
# ============================================================

def scan_all() -> None:
    global last_signal
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{now}] Сканую {len(SYMBOLS)} пари...")

    signals_sent = 0

    for symbol_name, ticker in SYMBOLS.items():
        try:
            data = analyze(symbol_name, ticker)
            print(f"  {symbol_name}: {data['price']:.5f} | RSI: {data['rsi']:.1f} | {data['signal']}")

            if should_send(symbol_name, data["signal"], data["rsi"]):
                message = format_message(data)
                asyncio.run(send_telegram(message))
                last_signal[symbol_name] = data["signal"]
                print(f"  ✅ Сигнал надіслано: {data['signal']}")
                signals_sent += 1
            elif data["signal"] != "НЕЙТРАЛЬНО":
                print(f"  ⏭️  Пропущено (вже надсилався)")

        except Exception as e:
            print(f"  ❌ {symbol_name}: Помилка — {e}")

    if signals_sent == 0:
        print(f"  — Нових сигналів немає")


def main():
    print("=" * 55)
    print("🤖 Форекс сигнальний бот v4.0 запущено")
    print(f"   Пари:         {', '.join(SYMBOLS.keys())}")
    print(f"   Таймфрейм:    {TIMEFRAME}")
    print(f"   Перевірка:    кожні {CHECK_EVERY} хв")
    print(f"   Брокер:       FxPro (торгуєш вручну)")
    print("=" * 55)

    scan_all()
    schedule.every(CHECK_EVERY).minutes.do(scan_all)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
