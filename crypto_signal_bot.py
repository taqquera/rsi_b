"""
Крипто-сигнальний бот для Telegram
Стратегія: RSI + EMA crossover — мульти-монетний сканер
Автор: твій фінансовий коуч (Claude)

Що робить цей бот:
- Кожні 15 хвилин сканує всі монети зі списку
- Надсилає сигнал ТІЛЬКИ якщо є КУПУЙ або ПРОДАВАЙ
- При НЕЙТРАЛЬНО — мовчить

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

TELEGRAM_TOKEN = "ВАШ_ТОКЕН_ВІД_BOTFATHER"
CHAT_ID        = "ВАШ_CHAT_ID"

# Список монет для сканування
SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "SUI/USDT",
    "HYPE/USDT:USDT",
]

TIMEFRAME   = "5m"    # таймфрейм: 1m, 5m, 15m, 1h, 4h, 1d
CHECK_EVERY = 15      # перевіряти кожні N хвилин

# Параметри індикаторів
RSI_PERIOD     = 14
RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70
EMA_FAST       = 9
EMA_SLOW       = 21

# Параметри ризик-менеджменту
STOP_LOSS_PCT   = 2.0
TAKE_PROFIT_PCT = 4.0

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
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_ema(closes: pd.Series, period: int) -> pd.Series:
    return closes.ewm(span=period, adjust=False).mean()


def analyze(symbol: str, timeframe: str) -> dict:
    df     = get_candles(symbol, timeframe)
    closes = df["close"]

    df["rsi"]      = calculate_rsi(closes, RSI_PERIOD)
    df["ema_fast"] = calculate_ema(closes, EMA_FAST)
    df["ema_slow"] = calculate_ema(closes, EMA_SLOW)

    last  = df.iloc[-1]
    prev  = df.iloc[-2]

    current_price = last["close"]
    current_rsi   = last["rsi"]
    ema_fast_now  = last["ema_fast"]
    ema_slow_now  = last["ema_slow"]
    ema_fast_prev = prev["ema_fast"]
    ema_slow_prev = prev["ema_slow"]

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

    if signal == "🟢 КУПУЙ":
        take_profit = current_price * (1 + TAKE_PROFIT_PCT / 100)
        stop_loss   = current_price * (1 - STOP_LOSS_PCT / 100)
    elif signal == "🔴 ПРОДАВАЙ":
        take_profit = current_price * (1 - TAKE_PROFIT_PCT / 100)
        stop_loss   = current_price * (1 + STOP_LOSS_PCT / 100)
    else:
        take_profit = None
        stop_loss   = None

    return {
        "symbol":      symbol,
        "timeframe":   timeframe,
        "price":       current_price,
        "rsi":         current_rsi,
        "ema_fast":    ema_fast_now,
        "ema_slow":    ema_slow_now,
        "signal":      signal,
        "reasons":     reason,
        "take_profit": take_profit,
        "stop_loss":   stop_loss,
        "timestamp":   last["timestamp"],
    }


# ============================================================
# ФОРМУВАННЯ ПОВІДОМЛЕННЯ
# ============================================================

def format_message(data: dict) -> str:
    reasons_text = "\n".join(f"  • {r}" for r in data["reasons"])

    if data["take_profit"] and data["stop_loss"]:
        rr = TAKE_PROFIT_PCT / STOP_LOSS_PCT
        tp_sl_block = (
            f"━━━━━━━━━━━━━━━\n"
            f"🎯 Тейк профіт: *${data['take_profit']:,.4f}* (+{TAKE_PROFIT_PCT}%)\n"
            f"🛑 Стоп лос:    *${data['stop_loss']:,.4f}* (-{STOP_LOSS_PCT}%)\n"
            f"📊 Ризик/прибуток: 1:{rr:.0f}\n"
        )
    else:
        tp_sl_block = ""

    return (
        f"📊 *{data['symbol']}* | {data['timeframe']}\n"
        f"🕐 {data['timestamp'].strftime('%H:%M %d.%m.%Y')}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Ціна входу: *${data['price']:,.4f}*\n"
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
# ГОЛОВНИЙ ЦИКЛ — сканує всі монети
# ============================================================

def scan_all() -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{now}] Сканую {len(SYMBOLS)} монет...")

    signals_found = 0

    for symbol in SYMBOLS:
        try:
            data = analyze(symbol, TIMEFRAME)
            print(f"  {symbol}: ${data['price']:,.4f} | RSI: {data['rsi']:.1f} | {data['signal']}")

            # Надсилаємо ТІЛЬКИ при реальному сигналі
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
    print("🤖 Мульти-монетний сканер запущено")
    print(f"   Монети:      {', '.join(SYMBOLS)}")
    print(f"   Таймфрейм:   {TIMEFRAME}")
    print(f"   Перевірка:   кожні {CHECK_EVERY} хв")
    print(f"   Стоп лос:    {STOP_LOSS_PCT}%")
    print(f"   Тейк профіт: {TAKE_PROFIT_PCT}%")
    print("=" * 50)

    scan_all()

    schedule.every(CHECK_EVERY).minutes.do(scan_all)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
