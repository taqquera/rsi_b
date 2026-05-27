"""
Форекс сигнальний бот для Telegram v4.2
Стратегія: RSI + сповіщення про різкі рухи (за 1 свічку І за 4 свічки)
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

TELEGRAM_TOKEN = "8886661285:AAF6p7w_BR4WIHo2oVrEhxi1pDqroXOilSA"
CHAT_ID        = "-5103360859"

SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "XAU/USD": "GC=F",
}

TIMEFRAME   = "15m"
CHECK_EVERY = 15

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

# Пороги різкого руху
# За 1 свічку (15 хвилин)
SPIKE_1_CANDLE = {
    "EUR/USD": 0.3,
    "GBP/USD": 0.3,
    "USD/JPY": 0.3,
    "XAU/USD": 0.5,
}

# За 4 свічки (1 година) — ловить поступові рухи
SPIKE_4_CANDLES = {
    "EUR/USD": 0.5,
    "GBP/USD": 0.5,
    "USD/JPY": 0.5,
    "XAU/USD": 1.0,
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
    data = yf.download(ticker, period="5d", interval="15m", progress=False)
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

    last    = df.iloc[-1]
    prev_1  = df.iloc[-2]   # 1 свічка тому (15 хвилин)
    prev_4  = df.iloc[-5]   # 4 свічки тому (1 година)

    # Перевірка свіжості даних — якщо старіша ніж 2 години, пропускаємо
    last_ts = last["timestamp"]
    if hasattr(last_ts, "tzinfo") and last_ts.tzinfo is not None:
        last_ts = last_ts.replace(tzinfo=None)
    data_age_minutes = (datetime.utcnow() - last_ts).total_seconds() / 60
    if data_age_minutes > 120:
        raise ValueError(f"Застарілі дані — остання свічка {data_age_minutes:.0f} хв тому")

    current_price   = float(last["close"])
    price_1ago      = float(prev_1["close"])
    price_4ago      = float(prev_4["close"])
    current_rsi     = float(last["rsi"])
    current_atr     = float(last["atr"])
    atr_pct         = (current_atr / current_price) * 100

    # Рух за 1 свічку (15 хв)
    change_1c = ((current_price - price_1ago) / price_1ago) * 100
    # Рух за 4 свічки (1 година)
    change_4c = ((current_price - price_4ago) / price_4ago) * 100

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
    spike_signal = None
    spike_period = None
    spike_change = None

    thr_1 = SPIKE_1_CANDLE.get(symbol_name, 0.3)
    thr_4 = SPIKE_4_CANDLES.get(symbol_name, 1.0)

    if abs(change_4c) >= thr_4:
        # Пріоритет — рух за годину
        spike_change = change_4c
        spike_period = "1 год"
        spike_signal = "📉 РІЗКЕ ПАДІННЯ" if change_4c < 0 else "📈 РІЗКЕ ЗРОСТАННЯ"
    elif abs(change_1c) >= thr_1:
        # Рух за 15 хвилин
        spike_change = change_1c
        spike_period = "15 хв"
        spike_signal = "📉 РІЗКЕ ПАДІННЯ" if change_1c < 0 else "📈 РІЗКЕ ЗРОСТАННЯ"

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
        "symbol":        symbol_name,
        "price":         current_price,
        "price_1ago":    price_1ago,
        "price_4ago":    price_4ago,
        "change_1c":     change_1c,
        "change_4c":     change_4c,
        "rsi":           current_rsi,
        "atr":           current_atr,
        "atr_pct":       atr_pct,
        "signal":        signal,
        "spike_signal":  spike_signal,
        "spike_period":  spike_period,
        "spike_change":  spike_change,
        "reasons":       reason,
        "take_profit":   take_profit,
        "stop_loss":     stop_loss,
        "tp_pct":        tp_pct,
        "sl_pct":        sl_pct,
        "timestamp":     last["timestamp"],
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
    reasons_text = "\n".join(f"  • {r}" for r in data["reasons"])
    time_str = data["timestamp"].strftime("%H:%M %d.%m.%Y") if hasattr(data["timestamp"], "strftime") else str(data["timestamp"])

    if data["take_profit"]:
        tp  = format_price(data["symbol"], data["take_profit"])
        sl  = format_price(data["symbol"], data["stop_loss"])
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
    p     = format_price(data["symbol"], data["price"])
    p_ref = format_price(data["symbol"], data["price_4ago"] if data["spike_period"] == "1 год" else data["price_1ago"])
    direction = "⬇️" if data["spike_change"] < 0 else "⬆️"
    time_str = data["timestamp"].strftime("%H:%M %d.%m.%Y") if hasattr(data["timestamp"], "strftime") else str(data["timestamp"])

    return (
        f"⚡ *{data['symbol']}* — {data['spike_signal']}\n"
        f"🕐 {time_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Ціна зараз:  *{p}*\n"
        f"💰 Була ({data['spike_period']} тому): *{p_ref}*\n"
        f"{direction} Рух: *{data['spike_change']:+.3f}%* за {data['spike_period']}\n"
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
            print(f"  {symbol_name}: {data['price']:.4f} | RSI: {data['rsi']:.1f} | 15хв: {data['change_1c']:+.3f}% | 1год: {data['change_4c']:+.3f}% | {data['signal']}")

            # RSI сигнал
            if should_send(symbol_name, data["signal"], data["rsi"]):
                asyncio.run(send_telegram(format_message(data)))
                last_signal[symbol_name] = data["signal"]
                print(f"  ✅ RSI сигнал: {data['signal']}")
                signals_sent += 1

            # Різкий рух
            if data["spike_signal"] and should_send_spike(symbol_name, data["spike_signal"]):
                asyncio.run(send_telegram(format_spike_message(data)))
                print(f"  ⚡ {data['spike_signal']} за {data['spike_period']}: {data['spike_change']:+.3f}%")
                signals_sent += 1
            elif not data["spike_signal"]:
                last_spike_signal[symbol_name] = None

        except Exception as e:
            print(f"  ❌ {symbol_name}: Помилка — {e}")

    if signals_sent == 0:
        print(f"  — Нових сигналів немає")


def main():
    print("=" * 55)
    print("🤖 Форекс сигнальний бот v4.2 запущено")
    print(f"   Пари:         {', '.join(SYMBOLS.keys())}")
    print(f"   Таймфрейм:    {TIMEFRAME}")
    print(f"   Перевірка:    кожні {CHECK_EVERY} хв")
    print(f"   Різкий рух:   15хв >0.3%/0.5% | 1год >0.5%/1.0%")
    print("=" * 55)

    scan_all()
    schedule.every(CHECK_EVERY).minutes.do(scan_all)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
