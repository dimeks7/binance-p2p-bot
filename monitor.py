import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from aiogram import Bot

from binance_client import BinanceAPIError, BinanceP2PClient
from config import load_config
from database import (
    get_conn,
    get_trigger_banks,
    get_last_notification_time_for_bank,
    log_notification,
    log_price_history,
    set_trigger_bank_previous_price,
    update_trigger,
    get_user_settings,
)

config = load_config()
binance_client = BinanceP2PClient()


@dataclass
class TriggerContext:
    id: int
    user_id: int
    name: str
    mode: str
    common_threshold: bool
    condition_type: str
    min_amount_uah: Optional[float]
    max_amount_uah: Optional[float]
    min_order_uah: Optional[float]
    max_order_uah: Optional[float]
    interval_sec: int
    repeat_enabled: bool
    repeat_interval_sec: Optional[int]
    status: str


async def monitor_worker(bot: Bot, worker_id: int, stop_event: asyncio.Event):
    # Один воркер, періодично обробляє всі активні тригери
    while not stop_event.is_set():
        try:
            await process_all_triggers(bot)
        except Exception as e:
            print(f"[monitor_worker {worker_id}] Error: {e}")
        await asyncio.sleep(config.monitor_default_interval)


async def process_all_triggers(bot: Bot):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM triggers WHERE status IN ('active');")
        rows = cur.fetchall()

    for row in rows:
        trigger = TriggerContext(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            mode=row["mode"],
            common_threshold=bool(row["common_threshold"]),
            condition_type=row["condition_type"],
            min_amount_uah=row["min_amount_uah"],
            max_amount_uah=row["max_amount_uah"],
            min_order_uah=row["min_order_uah"],
            max_order_uah=row["max_order_uah"],
            interval_sec=row["interval_sec"],
            repeat_enabled=bool(row["repeat_enabled"]),
            repeat_interval_sec=row["repeat_interval_sec"],
            status=row["status"],
        )
        await process_trigger(bot, trigger)


async def process_trigger(bot: Bot, trigger: TriggerContext):
    banks = get_trigger_banks(trigger.id)
    if not banks:
        return

    pay_types = [b["binance_pay_type"] for b in banks]
    try:
        result = await binance_client.fetch_ads(pay_types=pay_types)
    except BinanceAPIError:
        return

    # Записуємо середній курс у історію
    if result.ads:
        avg_price = sum(a.price for a in result.ads) / len(result.ads)
        log_price_history(
            payment_method_id=None,
            price=avg_price,
            total_ads=result.total,
            checked_ads=result.checked,
        )

    # Групуємо оголошення за payType
    ads_by_bank: Dict[str, List] = {}
    for ad in result.ads:
        for pt in ad.payment_types:
            if pt not in pay_types:
                continue
            ads_by_bank.setdefault(pt, []).append(ad)

    for bank in banks:
        binance_pay_type = bank["binance_pay_type"]
        pm_id = bank["payment_method_id"]
        bank_ads = ads_by_bank.get(binance_pay_type, [])

        if not bank_ads:
            continue

        # ФІЛЬТР: ліміти угоди й сума (за потреби)
        filtered_ads: List = []
        for ad in bank_ads:
            if (
                trigger.min_order_uah is not None
                and ad.min_single_trans_amount < trigger.min_order_uah
            ):
                continue
            if (
                trigger.max_order_uah is not None
                and ad.max_single_trans_amount > trigger.max_order_uah
            ):
                continue
            filtered_ads.append(ad)

        if not filtered_ads:
            continue

        # Поточна ціна тільки по відфільтрованих
        if trigger.condition_type in ("ge", "cross_up"):
            current_price = max(a.price for a in filtered_ads)
        else:
            current_price = min(a.price for a in filtered_ads)

        # Поріг
        if bool(bank["use_separate_threshold"]) and bank["threshold_price"] is not None:
            threshold = float(bank["threshold_price"])
        else:
            threshold = float(trigger.min_amount_uah or 0)

        await handle_trigger_logic(
            bot=bot,
            trigger=trigger,
            pm_id=pm_id,
            bank_display_name=bank["display_name"],
            current_price=current_price,
            threshold=threshold,
            previous_price=bank["previous_price"],
            ads=filtered_ads,
        )


async def handle_trigger_logic(
    bot: Bot,
    trigger: TriggerContext,
    pm_id: int,
    bank_display_name: str,
    current_price: float,
    threshold: float,
    previous_price: Optional[float],
    ads: List,
):
    # Глобальні налаштування сповіщень
    settings = get_user_settings(trigger.user_id)
    if not settings.get("notifications_enabled", 1):
        set_trigger_bank_previous_price(trigger.id, pm_id, current_price)
        return

    # Анти-дубль: по trigger_id + bank (payment_method)
    MIN_NOTIFY_INTERVAL_SEC = 10
    last_ntf_bank = get_last_notification_time_for_bank(trigger.id, pm_id)
    if last_ntf_bank:
        last_dt_bank = datetime.fromisoformat(last_ntf_bank)
        if datetime.utcnow() - last_dt_bank < timedelta(seconds=MIN_NOTIFY_INTERVAL_SEC):
            set_trigger_bank_previous_price(trigger.id, pm_id, current_price)
            return

    # Режим: постійний
    if trigger.mode == "continuous":
        condition_ok = False
        if trigger.condition_type == "ge":
            condition_ok = current_price >= threshold
        elif trigger.condition_type == "le":
            condition_ok = current_price <= threshold

        if not condition_ok:
            set_trigger_bank_previous_price(trigger.id, pm_id, current_price)
            return

        # Додатковий інтервал для постійного режиму
        if trigger.repeat_enabled and trigger.repeat_interval_sec:
            last_ntf_bank = get_last_notification_time_for_bank(trigger.id, pm_id)
            if last_ntf_bank:
                last_dt_bank = datetime.fromisoformat(last_ntf_bank)
                if datetime.utcnow() - last_dt_bank < timedelta(
                    seconds=trigger.repeat_interval_sec
                ):
                    set_trigger_bank_previous_price(trigger.id, pm_id, current_price)
                    return

        await send_notification(
            bot=bot,
            trigger=trigger,
            bank_display_name=bank_display_name,
            threshold=threshold,
            ads=ads,
        )
        log_notification(
            trigger_id=trigger.id,
            payment_method_id=pm_id,
            price=current_price,
            condition_type=trigger.condition_type,
        )
        set_trigger_bank_previous_price(trigger.id, pm_id, current_price)

    # Режим: перетин
    elif trigger.mode == "cross":
        if previous_price is None:
            set_trigger_bank_previous_price(trigger.id, pm_id, current_price)
            return

        crossed = False
        if trigger.condition_type == "cross_up":
            if previous_price < threshold <= current_price:
                crossed = True
        elif trigger.condition_type == "cross_down":
            if previous_price > threshold >= current_price:
                crossed = True

        if not crossed:
            set_trigger_bank_previous_price(trigger.id, pm_id, current_price)
            return

        await send_notification(
            bot=bot,
            trigger=trigger,
            bank_display_name=bank_display_name,
            threshold=threshold,
            ads=ads,
        )
        log_notification(
            trigger_id=trigger.id,
            payment_method_id=pm_id,
            price=current_price,
            condition_type=trigger.condition_type,
        )
        set_trigger_bank_previous_price(trigger.id, pm_id, current_price)

    # Режим: разовий
    elif trigger.mode == "one_time":
        condition_ok = False
        if trigger.condition_type == "ge":
            condition_ok = current_price >= threshold
        elif trigger.condition_type == "le":
            condition_ok = current_price <= threshold
        elif trigger.condition_type == "cross_up" and previous_price is not None:
            condition_ok = previous_price < threshold <= current_price
        elif trigger.condition_type == "cross_down" and previous_price is not None:
            condition_ok = previous_price > threshold >= current_price

        if not condition_ok:
            set_trigger_bank_previous_price(trigger.id, pm_id, current_price)
            return

        await send_notification(
            bot=bot,
            trigger=trigger,
            bank_display_name=bank_display_name,
            threshold=threshold,
            ads=ads,
        )
        log_notification(
            trigger_id=trigger.id,
            payment_method_id=pm_id,
            price=current_price,
            condition_type=trigger.condition_type,
        )
        set_trigger_bank_previous_price(trigger.id, pm_id, current_price)
        update_trigger(trigger.id, status="done")


async def send_notification(
    bot: Bot,
    trigger: TriggerContext,
    bank_display_name: str,
    threshold: float,
    ads: List,
):
    # Сортуємо список оголошень по ціні (залежно від умови)
    if trigger.condition_type in ("ge", "cross_up"):
        sorted_ads = sorted(ads, key=lambda a: a.price, reverse=True)
    else:
        sorted_ads = sorted(ads, key=lambda a: a.price)

    best_ad = sorted_ads[0]
    current_price = best_ad.price
    top_ads = sorted_ads[:3]  # топ-3 оголошень

    total_available = sum(a.available_amount for a in ads)

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT chat_id FROM users WHERE id=?;", (trigger.user_id,))
        row = cur.fetchone()
        if not row:
            return
        chat_id = row["chat_id"]

    condition_text_map = {
        "ge": "курс вище або дорівнює цілі",
        "le": "курс нижче або дорівнює цілі",
        "cross_up": "курс перетнув рівень знизу вгору",
        "cross_down": "курс перетнув рівень зверху вниз",
    }
    condition_text = condition_text_map.get(
        trigger.condition_type, trigger.condition_type
    )

    text_lines = [
        "🔔 СПРАЦЮВАВ ЦІНОВИЙ ТРИГЕР",
        "",
        f"Назва: {trigger.name}",
        "",
        f"Банк: {bank_display_name}",
        f"Курс: {current_price:.2f} UAH/USDT",
        f"Цільовий курс: {threshold:.2f} UAH/USDT",
        f"Умова: {condition_text}",
        "",
        f"Доступний обсяг (сума по оголошеннях): {total_available:.2f} USDT",
        f"Ліміт угоди (краще оголошення): {best_ad.min_single_trans_amount:.0f}–{best_ad.max_single_trans_amount:.0f} UAH",
        f"Продавець (краще оголошення): {best_ad.merchant_name}",
        f"Час виявлення: {datetime.utcnow().strftime('%H:%M:%S')}",
        "",
        "Топ оголошень:",
    ]

    for idx, ad in enumerate(top_ads, start=1):
        text_lines.append(
            f"{idx}) {ad.price:.2f} UAH/USDT, "
            f"ліміт {ad.min_single_trans_amount:.0f}–{ad.max_single_trans_amount:.0f} UAH, "
            f"{ad.merchant_name}"
        )

    text_lines.append("")
    text_lines.append(
        "Відкрити Binance P2P: https://p2p.binance.com/uk-UA/trade/sell/USDT?fiat=UAH"
    )

    text = "\n".join(text_lines)

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        disable_web_page_preview=True,
    )
