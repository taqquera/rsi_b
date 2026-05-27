"""
Форекс сигнальний бот для Telegram v4.1
Стратегія: RSI + сповіщення про різкі рухи
Пари: EUR/USD, GBP/USD, USD/JPY, XAU/USD
Автор: твій фінансовий коуч (Claude)

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
import yfinance as yf
from telegram import Bot
from datetime import datetime

# ============================================================
# НАЛАШТУВАННЯ
# ============================================================

TELEGRAM_TOKEN = "ВАШ_ТОКЕН_ВІД_BOTFATHER"
CHAT_ID        = "ВАШ_CHAT_ID"

SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "XAU/USD": "GC=F",
}

TIMEFRAME   = "1h"
CHECK_EVERY = 60

# Параметри RSI
RSI_PERIOD     = 14
RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70
RSI_RESET_LOW  = 40
RSI_RESET_HIGH = 60

# Параметри ATR
ATR_PERIOD  = 14
ATR_TP_MULT = 2.0
ATR_SL_MULT = 1.0

# Поріг різкого руху за одну свічку (%)
# Форекс: 0.5% вже великий рух. Золото: 1.0%
SPIKE_THRESHOLDS = {
    "EUR/USD": 0.5,
    "GBP/USD": 0.5,
    "USD/JPY": 0.5,
    "XAU/USD": 1.0,  # золото рухається більше
}

# ============================================================
# ПАМ'ЯТЬ СИГНАЛІВ
# ============================================================
last_signal       = {symbol: None for symbol in SYMBOLS.keys()}
last_spike_signal = {symbol: None for symbol in SYMBOLS.keys()}


# ============================================================
# ОТРИМАННЯ ДАНИХ
# ============================================================

def get_candles(symbol_name: str, ticker: str) -> pd.DataFrame:
    data = yf.download(ticker, period="7d", interval="1h", progress=False)
    if data.empty:
        raise ValueError(f"Немає даних для {symbol_name}")
    df = data[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    df.index.name = "timestamp"
    df = df.reset_index()
    return df


# ============================================================
# ІНДИКАТОРИ
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


# ============================================================
# АНАЛІЗ
# ============================================================

def analyze(symbol_name: str, ticker: str) -> dict:
    df     = get_candles(symbol_name, ticker)
    closes = df["close"]

    df["rsi"] = calculate_rsi(closes, RSI_PERIOD)
    df["atr"] = calculate_atr(df, ATR_PERIOD)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    current_price    = float(last["close"])
    prev_price       = float(prev["close"])
    current_rsi      = float(last["rsi"])
    current_atr      = float(last["atr"])
    atr_pct          = (current_atr / current_price) * 100
    price_change_pct = ((current_price - prev_price) / prev_price) * 100

    # RSI сигнал
    signal = "НЕЙТРАЛЬНО"
    reason = []

    if current_rsi < RSI_OVERSOLD:
        signal = "🟢 КУПУЙ"
        reason.append(f"RSI перепроданий ({current_rsi:.1f} < {RSI_OVERSOLD})")
    elif current_rsi > RSI_OVERBOUGHT:
        signal = "🔴 ПРОДАВАЙ"
        reason.append(f"RSI перекуплений ({current_rsi:.1f} > {RSI_OVERBOUGHT})")

    # Сповіщення про різкий рух
    spike_threshold = SPIKE_THRESHOLDS.get(symbol_name, 0.5)
    spike_signal = None
    if price_change_pct <= -spike_threshold:
        spike_signal = "📉 РІЗКЕ ПАДІННЯ"
    elif price_change_pct >= spike_threshold:
        spike_signal = "📈 РІЗКЕ ЗРОСТАННЯ"

    # TP/SL
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
        "symbol":           symbol_name,
        "price":            current_price,
        "prev_price":       prev_price,
        "price_change_pct": price_change_pct,
        "rsi":              current_rsi,
        "atr":              current_atr,
        "atr_pct":          atr_pct,
        "signal":           signal,
        "spike_signal":     spike_signal,
        "reasons":          reason,
        "take_profit":      take_profit,
        "stop_loss":        stop_loss,
        "tp_pct":           tp_pct,
        "sl_pct":           sl_pct,
        "timestamp":        last["timestamp"],
    }


# ============================================================
# ПАМ'ЯТЬ
# ============================================================

def should_send(symbol: str, new_signal: str, current_rsi: float) -> bool:
    global last_signal

    if new_signal == "НЕЙТРАЛЬНО":
        return False

    prev = last_signal[symbol]

    if prev == "🟢 КУПУЙ" and current_rsi > RSI_RESET_LOW:
        last_signal[symbol] = None
        prev = None
    if prev == "🔴 ПРОДАВАЙ" and current_rsi < RSI_RESET_HIGH:
        last_signal[symbol] = None
        prev = None

    return prev != new_signal


def should_send_spike(symbol: str, spike_signal: str) -> bool:
    """Надсилає сповіщення про різкий рух тільки один раз підряд."""
    global last_spike_signal

    if spike_signal is None:
        last_spike_signal[symbol] = None
        return False

    if last_spike_signal[symbol] == spike_signal:
        return False

    last_spike_signal[symbol] = spike_signal
    return True


# ============================================================
# ПОВІДОМЛЕННЯ
# ============================================================

def format_price(symbol: str, price: float) -> str:
    if symbol == "USD/JPY":
        return f"{price:.3f}"
    elif symbol == "XAU/USD":
        return f"${price:,.2f}"
    else:
        return f"{price:.5f}"


def format_message(data: dict) -> str:
    p = format_price(data["symbol"], data["price"])

    if data["take_profit"]:
        tp = format_price(data["symbol"], data["take_profit"])
        sl = format_price(data["symbol"], data["stop_loss"])
        atr = format_price(data["symbol"], data["atr"])
        tp_sl_block = (
            f"━━━━━━━━━━━━━━━\n"
            f"📐 ATR: `{atr}` ({data['atr_pct']:.3f}%)\n"
            f"🎯 Тейк профіт: *{tp}* ({data['tp_pct']:+.3f}%)\n"
            f"🛑 Стоп лос:    *{sl}* ({data['sl_pct']:+.3f}%)\n"
            f"📊 Ризик/прибуток: 1:{ATR_TP_MULT/ATR_SL_MULT:.0f}\n"
        )
    else:
        tp_sl_block = ""

    reasons_text = "\n".join(f"  • {r}" for r in data["reasons"])
    time_str = data["timestamp"].strftime("%H:%M %d.%m.%Y") if hasattr(data["timestamp"], "strftime") else str(data["timestamp"])

    return (
        f"📊 *{data['symbol']}* | {TIMEFRAME}\n"
        f"🕐 {time_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Ціна: *{p}*\n"
        f"📈 RSI ({RSI_PERIOD}): `{data['rsi']:.1f}`\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Сигнал: *{data['signal']}*\n"
        f"Причини:\n{reasons_text}\n"
        f"{tp_sl_block}"
        f"━━━━━━━━━━━━━━━\n"
        f"⚠️ _Це не фінансова порада. Торгуй самостійно._"
    )


def format_spike_message(data: dict) -> str:
    """Окреме повідомлення про різкий рух."""
    p     = format_price(data["symbol"], data["price"])
    p_prev = format_price(data["symbol"], data["prev_price"])
    direction = "⬇️" if data["price_change_pct"] < 0 else "⬆️"
    spike_threshold = SPIKE_THRESHOLDS.get(data["symbol"], 0.5)
    time_str = data["timestamp"].strftime("%H:%M %d.%m.%Y") if hasattr(data["timestamp"], "strftime") else str(data["timestamp"])

    return (
        f"⚡ *{data['symbol']}* — {data['spike_signal']}\n"
        f"🕐 {time_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Ціна зараз: *{p}*\n"
        f"💰 Попередня:  *{p_prev}*\n"
        f"{direction} Рух за 1 свічку: *{data['price_change_pct']:+.3f}%* (поріг {spike_threshold}%)\n"
        f"📈 RSI: `{data['rsi']:.1f}`\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⚠️ _Можливий вплив новин. Аналізуй самостійно._"
    )


async def send_telegram(message: str) -> None:
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")


# ============================================================
# ГОЛОВНИЙ ЦИКЛ
# ============================================================

def scan_all() -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{now}] Сканую {len(SYMBOLS)} пари...")

    signals_sent = 0

    for symbol_name, ticker in SYMBOLS.items():
        try:
            data = analyze(symbol_name, ticker)
            print(f"  {symbol_name}: {data['price']:.5f} | RSI: {data['rsi']:.1f} | {data['price_change_pct']:+.3f}% | {data['signal']}")

            # RSI сигнал
            if should_send(symbol_name, data["signal"], data["rsi"]):
                message = format_message(data)
                asyncio.run(send_telegram(message))
                last_signal[symbol_name] = data["signal"]
                print(f"  ✅ RSI сигнал: {data['signal']}")
                signals_sent += 1

            # Сповіщення про різкий рух
            if data["spike_signal"] and should_send_spike(symbol_name, data["spike_signal"]):
                spike_msg = format_spike_message(data)
                asyncio.run(send_telegram(spike_msg))
                print(f"  ⚡ Різкий рух: {data['spike_signal']} ({data['price_change_pct']:+.3f}%)")
                signals_sent += 1

        except Exception as e:
            print(f"  ❌ {symbol_name}: Помилка — {e}")

    if signals_sent == 0:
        print(f"  — Нових сигналів немає")


def main():
    print("=" * 55)
    print("🤖 Форекс сигнальний бот v4.1 запущено")
    print(f"   Пари:         {', '.join(SYMBOLS.keys())}")
    print(f"   Таймфрейм:    {TIMEFRAME}")
    print(f"   Перевірка:    кожні {CHECK_EVERY} хв")
    print(f"   Різкий рух:   EUR/GBP/JPY >{list(SPIKE_THRESHOLDS.values())[0]}% | XAU >{list(SPIKE_THRESHOLDS.values())[3]}%")
    print("=" * 55)

    scan_all()
    schedule.every(CHECK_EVERY).minutes.do(scan_all)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
