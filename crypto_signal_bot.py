"""
Крипто-сигнальний бот для Telegram
Стратегія: RSI + EMA crossover + ATR для розумних TP/SL
Автор: твій фінансовий коуч (Claude)

Що робить цей бот:
- Кожні 15 хвилин сканує всі монети зі списку
- Надсилає сигнал ТІЛЬКИ якщо є КУПУЙ або ПРОДАВАЙ
- ТП і СЛ розраховуються індивідуально через ATR для кожної монети

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

# Список монет для сканування
SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT",
    "SOL/USDT",
    "SUI/USDT",
    "HYPE/USDT:USDT",
]

TIMEFRAME   = "1h"   # таймфрейм: 1m, 5m, 15m, 1h, 4h, 1d
CHECK_EVERY = 15     # перевіряти кожні N хвилин

# Параметри індикаторів
RSI_PERIOD     = 14
RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70
EMA_FAST       = 9
EMA_SLOW       = 21

# Параметри ATR для розумних TP/SL
ATR_PERIOD     = 14    # період ATR (скільки свічок для розрахунку волатильності)
ATR_TP_MULT    = 2.0   # тейк профіт = ATR * 2.0
ATR_SL_MULT    = 1.0   # стоп лос   = ATR * 1.0  (співвідношення ризик 1:2)

# ============================================================
# ПІДКЛЮЧЕННЯ ДО OKX
# ============================================================

exchange = ccxt.okx({
    "enableRateLimit": True,
})


def get_candles(symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
    raw = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


# ============================================================
# РОЗРАХУНОК ІНДИКАТОРІВ
# ============================================================

def calculate_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """RSI — показує чи монета перекуплена/перепродана."""
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_ema(closes: pd.Series, period: int) -> pd.Series:
    """EMA — ковзне середнє."""
    return closes.ewm(span=period, adjust=False).mean()


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    ATR (Average True Range) — середній діапазон руху свічки.
    Показує наскільки монета рухається в середньому за одну свічку.
    BTC може мати ATR $500, а SUI — $0.03.
    Це дозволяє ставити ТП/СЛ відповідно до волатильності кожної монети.
    """
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    # True Range = максимум з трьох варіантів:
    tr1 = high - low                        # діапазон поточної свічки
    tr2 = (high - close.shift()).abs()      # від максимуму до попереднього закриття
    tr3 = (low  - close.shift()).abs()      # від мінімуму до попереднього закриття

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(com=period - 1, min_periods=period).mean()


def analyze(symbol: str, timeframe: str) -> dict:
    """Аналізує ринок і повертає сигнал з розумними TP/SL через ATR."""
    df     = get_candles(symbol, timeframe)
    closes = df["close"]

    df["rsi"]      = calculate_rsi(closes, RSI_PERIOD)
    df["ema_fast"] = calculate_ema(closes, EMA_FAST)
    df["ema_slow"] = calculate_ema(closes, EMA_SLOW)
    df["atr"]      = calculate_atr(df, ATR_PERIOD)

    last  = df.iloc[-1]
    prev  = df.iloc[-2]

    current_price = last["close"]
    current_rsi   = last["rsi"]
    current_atr   = last["atr"]
    ema_fast_now  = last["ema_fast"]
    ema_slow_now  = last["ema_slow"]
    ema_fast_prev = prev["ema_fast"]
    ema_slow_prev = prev["ema_slow"]

    # ATR у відсотках від ціни (для розуміння волатильності)
    atr_pct = (current_atr / current_price) * 100

    ema_crossed_up   = (ema_fast_prev < ema_slow_prev) and (ema_fast_now > ema_slow_now)
    ema_crossed_down = (ema_fast_prev > ema_slow_prev) and (ema_fast_now < ema_slow_now)

    signal = "НЕЙТРАЛЬНО"
    reason = []

    if current_rsi < RSI_OVERSOLD:
        reason.append(f"RSI перепроданий ({current_rsi:.1f} < {RSI_OVERSOLD})")
    if current_rsi > RSI_OVERBOUGHT:
        reason.append(f"RSI перекуплений ({current_rsi:.1f} > {RSI_OVERBOUGHT})")
    if ema_crossed_up:
        reason.append(f"EMA{EMA_FAST} перетнула EMA{EMA_SLOW} знизу вгору")
    if ema_crossed_down:
        reason.append(f"EMA{EMA_FAST} перетнула EMA{EMA_SLOW} зверху вниз")

    buy_signals  = (current_rsi < RSI_OVERSOLD) or ema_crossed_up
    sell_signals = (current_rsi > RSI_OVERBOUGHT) or ema_crossed_down

    if buy_signals and not sell_signals:
        signal = "🟢 КУПУЙ"
    elif sell_signals and not buy_signals:
        signal = "🔴 ПРОДАВАЙ"

    # Розумні TP/SL через ATR — індивідуально для кожної монети
    if signal == "🟢 КУПУЙ":
        take_profit = current_price + (current_atr * ATR_TP_MULT)
        stop_loss   = current_price - (current_atr * ATR_SL_MULT)
    elif signal == "🔴 ПРОДАВАЙ":
        take_profit = current_price - (current_atr * ATR_TP_MULT)
        stop_loss   = current_price + (current_atr * ATR_SL_MULT)
    else:
        take_profit = None
        stop_loss   = None

    # Відсотки для зручності
    tp_pct = ((take_profit - current_price) / current_price * 100) if take_profit else None
    sl_pct = ((stop_loss  - current_price) / current_price * 100) if stop_loss  else None

    return {
        "symbol":      symbol,
        "timeframe":   timeframe,
        "price":       current_price,
        "rsi":         current_rsi,
        "ema_fast":    ema_fast_now,
        "ema_slow":    ema_slow_now,
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
# ФОРМУВАННЯ ПОВІДОМЛЕННЯ
# ============================================================

def format_message(data: dict) -> str:
    reasons_text = "\n".join(f"  • {r}" for r in data["reasons"])

    # Форматуємо ціну залежно від розміру (BTC vs SUI)
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

    if data["take_profit"] and data["stop_loss"]:
        tp_sl_block = (
            f"━━━━━━━━━━━━━━━\n"
            f"📐 ATR: `{atr_fmt}` ({data['atr_pct']:.2f}% волатильність)\n"
            f"🎯 Тейк профіт: *{tp_fmt}* ({data['tp_pct']:+.2f}%)\n"
            f"🛑 Стоп лос:    *{sl_fmt}* ({data['sl_pct']:+.2f}%)\n"
            f"📊 Ризик/прибуток: 1:{ATR_TP_MULT/ATR_SL_MULT:.0f}\n"
        )
    else:
        tp_sl_block = ""

    return (
        f"📊 *{data['symbol'].replace(':USDT', '')}* | {data['timeframe']}\n"
        f"🕐 {data['timestamp'].strftime('%H:%M %d.%m.%Y')}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Ціна входу: *{price_fmt}*\n"
        f"📈 RSI ({RSI_PERIOD}): `{data['rsi']:.1f}`\n"
        f"〽️ EMA{EMA_FAST}: `{data['ema_fast']:,.4f}`\n"
        f"〽️ EMA{EMA_SLOW}: `{data['ema_slow']:,.4f}`\n"
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


# ============================================================
# ГОЛОВНИЙ ЦИКЛ
# ============================================================

def scan_all() -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{now}] Сканую {len(SYMBOLS)} монет...")

    signals_found = 0

    for symbol in SYMBOLS:
        try:
            data = analyze(symbol, TIMEFRAME)
            print(f"  {symbol}: ${data['price']:,.4f} | RSI: {data['rsi']:.1f} | ATR: {data['atr_pct']:.2f}% | {data['signal']}")

            if data["signal"] != "НЕЙТРАЛЬНО":
                message = format_message(data)
                asyncio.run(send_telegram(message))
                print(f"  ✅ Сигнал надіслано для {symbol}")
                signals_found += 1

        except Exception as e:
            print(f"  ❌ {symbol}: Помилка — {e}")

    if signals_found == 0:
        print(f"  — Всі монети нейтральні, мовчимо")


def main():
    print("=" * 50)
    print("🤖 Мульти-монетний сканер з ATR запущено")
    print(f"   Монети:      {', '.join(SYMBOLS)}")
    print(f"   Таймфрейм:   {TIMEFRAME}")
    print(f"   Перевірка:   кожні {CHECK_EVERY} хв")
    print(f"   ATR період:  {ATR_PERIOD} свічок")
    print(f"   ТП множник:  {ATR_TP_MULT}x ATR")
    print(f"   СЛ множник:  {ATR_SL_MULT}x ATR")
    print("=" * 50)

    scan_all()

    schedule.every(CHECK_EVERY).minutes.do(scan_all)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
