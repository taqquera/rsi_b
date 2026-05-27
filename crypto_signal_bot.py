"""
Крипто-сигнальний бот для Telegram v3.0
Стратегія: тільки RSI + пам'ять сигналів (без дублікатів)
Автор: твій фінансовий коуч (Claude)

Зміни v3.0:
- Прибрано EMA crossover (давав забагато шуму)
- Додано пам'ять: кожна монета надсилає сигнал тільки 1 раз
- Повторний сигнал тільки коли RSI вийшов з зони і повернувся знову

ВСТАНОВЛЕННЯ:
pip install ccxt pandas python-telegram-bot schedule

НАЛАШТУВАННЯ:
1. Створи бота через @BotFather -> отримай TELEGRAM_TOKEN
2. Дізнайся свій CHAT_ID через @userinfobot
3. Заповни змінні нижче
"""

import ccxt
import pandas as pd
import schedule
import time
import asyncio
from telegram import Bot
from datetime import datetime

# ============================================================
# НАЛАШТУВАННЯ — заповни свої дані тут
# ============================================================

TELEGRAM_TOKEN = "8886661285:AAF6p7w_BR4WIHo2oVrEhxi1pDqroXOilSA"
CHAT_ID        = "-5103360859"

SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "DOT/USDT",
    "AVAX/USDT",
]

TIMEFRAME   = "1h"
CHECK_EVERY = 15

# Параметри RSI
RSI_PERIOD     = 14
RSI_OVERSOLD   = 30   # сигнал КУПУЙ
RSI_OVERBOUGHT = 70   # сигнал ПРОДАВАЙ
RSI_RESET_LOW  = 40   # RSI має піднятись вище 40 щоб скинути сигнал КУПУЙ
RSI_RESET_HIGH = 60   # RSI має впасти нижче 60 щоб скинути сигнал ПРОДАВАЙ

# Параметри ATR
ATR_PERIOD  = 14
ATR_TP_MULT = 2.0
ATR_SL_MULT = 1.0

# ============================================================
# ПАМ'ЯТЬ СИГНАЛІВ — зберігає останній сигнал для кожної монети
# Формат: { "BTC/USDT:USDT": "🟢 КУПУЙ" або "🔴 ПРОДАВАЙ" або None }
# ============================================================
last_signal = {symbol: None for symbol in SYMBOLS}

# ============================================================
# ПІДКЛЮЧЕННЯ ДО OKX
# ============================================================

exchange = ccxt.okx({
    "enableRateLimit": True,
})


def get_candles(symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
    raw = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df  = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


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


def analyze(symbol: str, timeframe: str) -> dict:
    df     = get_candles(symbol, timeframe)
    closes = df["close"]

    df["rsi"] = calculate_rsi(closes, RSI_PERIOD)
    df["atr"] = calculate_atr(df, ATR_PERIOD)

    last = df.iloc[-1]

    current_price = last["close"]
    current_rsi   = last["rsi"]
    current_atr   = last["atr"]
    atr_pct       = (current_atr / current_price) * 100

    # Тільки RSI сигнали — без EMA
    signal = "НЕЙТРАЛЬНО"
    reason = []

    if current_rsi < RSI_OVERSOLD:
        signal = "🟢 КУПУЙ"
        reason.append(f"RSI перепроданий ({current_rsi:.1f} < {RSI_OVERSOLD})")
    elif current_rsi > RSI_OVERBOUGHT:
        signal = "🔴 ПРОДАВАЙ"
        reason.append(f"RSI перекуплений ({current_rsi:.1f} > {RSI_OVERBOUGHT})")

    # TP/SL через ATR
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
        "symbol":      symbol,
        "timeframe":   timeframe,
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


def should_send(symbol: str, new_signal: str, current_rsi: float) -> bool:
    """
    Перевіряє чи треба надсилати сигнал.

    Логіка пам'яті:
    - Якщо сигнал НЕЙТРАЛЬНО — не надсилаємо
    - Якщо такий самий сигнал вже був надісланий — не надсилаємо
    - Якщо попередній сигнал був КУПУЙ і RSI піднявся вище RSI_RESET_LOW — скидаємо пам'ять
    - Якщо попередній сигнал був ПРОДАВАЙ і RSI впав нижче RSI_RESET_HIGH — скидаємо пам'ять
    """
    global last_signal

    if new_signal == "НЕЙТРАЛЬНО":
        return False

    prev = last_signal[symbol]

    # Скидаємо пам'ять якщо RSI вийшов з зони
    if prev == "🟢 КУПУЙ" and current_rsi > RSI_RESET_LOW:
        last_signal[symbol] = None
        prev = None
        print(f"  🔄 {symbol}: сигнал КУПУЙ скинуто (RSI вийшов з зони: {current_rsi:.1f})")

    if prev == "🔴 ПРОДАВАЙ" and current_rsi < RSI_RESET_HIGH:
        last_signal[symbol] = None
        prev = None
        print(f"  🔄 {symbol}: сигнал ПРОДАВАЙ скинуто (RSI вийшов з зони: {current_rsi:.1f})")

    # Надсилаємо тільки якщо сигнал новий
    if prev == new_signal:
        return False

    return True


def format_message(data: dict) -> str:
    if data["price"] > 100:
        price_fmt = f"${data['price']:,.2f}"
        tp_fmt    = f"${data['take_profit']:,.2f}"
        sl_fmt    = f"${data['stop_loss']:,.2f}"
        atr_fmt   = f"${data['atr']:,.2f}"
    else:
        price_fmt = f"${data['price']:,.4f}"
        tp_fmt    = f"${data['take_profit']:,.4f}"
        sl_fmt    = f"${data['stop_loss']:,.4f}"
        atr_fmt   = f"${data['atr']:,.4f}"

    reasons_text = "\n".join(f"  • {r}" for r in data["reasons"])

    tp_sl_block = (
        f"━━━━━━━━━━━━━━━\n"
        f"📐 ATR: `{atr_fmt}` ({data['atr_pct']:.2f}% волатильність)\n"
        f"🎯 Тейк профіт: *{tp_fmt}* ({data['tp_pct']:+.2f}%)\n"
        f"🛑 Стоп лос:    *{sl_fmt}* ({data['sl_pct']:+.2f}%)\n"
        f"📊 Ризик/прибуток: 1:{ATR_TP_MULT/ATR_SL_MULT:.0f}\n"
    )

    return (
        f"📊 *{data['symbol'].replace(':USDT', '')}* | {data['timeframe']}\n"
        f"🕐 {data['timestamp'].strftime('%H:%M %d.%m.%Y')}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Ціна входу: *{price_fmt}*\n"
        f"📈 RSI ({RSI_PERIOD}): `{data['rsi']:.1f}`\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Сигнал: *{data['signal']}*\n"
        f"Причини:\n{reasons_text}\n"
        f"{tp_sl_block}"
        f"━━━━━━━━━━━━━━━\n"
        f"⚠️ _Це не фінансова порада. Завжди аналізуй самостійно._"
    )


async def send_telegram(message: str) -> None:
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")


def scan_all() -> None:
    global last_signal
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{now}] Сканую {len(SYMBOLS)} монет...")

    signals_sent = 0

    for symbol in SYMBOLS:
        try:
            data = analyze(symbol, TIMEFRAME)
            print(f"  {symbol}: ${data['price']:,.4f} | RSI: {data['rsi']:.1f} | {data['signal']}")

            if should_send(symbol, data["signal"], data["rsi"]):
                message = format_message(data)
                asyncio.run(send_telegram(message))
                last_signal[symbol] = data["signal"]
                print(f"  ✅ Сигнал надіслано: {data['signal']}")
                signals_sent += 1
            elif data["signal"] != "НЕЙТРАЛЬНО":
                print(f"  ⏭️  Пропущено (вже надсилався раніше)")

        except Exception as e:
            print(f"  ❌ {symbol}: Помилка — {e}")

    if signals_sent == 0:
        print(f"  — Нових сигналів немає")


def main():
    print("=" * 55)
    print("🤖 Крипто-сигнальний бот v3.0 запущено")
    print(f"   Монети:       {', '.join(s.replace(':USDT','') for s in SYMBOLS)}")
    print(f"   Таймфрейм:    {TIMEFRAME}")
    print(f"   Перевірка:    кожні {CHECK_EVERY} хв")
    print(f"   Сигнал КУПУЙ: RSI < {RSI_OVERSOLD} (скидання > {RSI_RESET_LOW})")
    print(f"   Сигнал ПРОДАВАЙ: RSI > {RSI_OVERBOUGHT} (скидання < {RSI_RESET_HIGH})")
    print("=" * 55)

    scan_all()
    schedule.every(CHECK_EVERY).minutes.do(scan_all)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
