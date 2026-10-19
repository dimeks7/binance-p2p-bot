import asyncio
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

from binance_client import BinanceAPIError, BinanceP2PClient
from config import load_config
from database import (
    get_or_create_user,
    get_payment_methods,
    init_db,
    create_trigger,
    add_trigger_bank,
    get_user_triggers,
    get_price_history_last_24h,
    get_conn,
    delete_trigger,
    get_user_settings,
    set_user_settings,
)
from monitor import monitor_worker

config = load_config()
bot = Bot(
    token=config.bot_token,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()
binance_client = BinanceP2PClient()


# --- СТАНИ --- #
class CreateTriggerStates(StatesGroup):
    choosing_mode = State()
    choosing_banks = State()
    choosing_threshold_mode = State()
    entering_threshold_common = State()
    entering_threshold_per_bank = State()
    choosing_condition = State()
    entering_min_amount = State()
    entering_max_amount = State()
    entering_min_order = State()
    entering_max_order = State()        # ДОДАНО: максимальний ліміт угоди продавця
    entering_interval = State()
    choosing_repeat = State()
    entering_repeat_interval = State()
    confirming = State()


class SettingsStates(StatesGroup):
    entering_default_interval = State()
    entering_history_days = State()


# --- КЛАВІАТУРИ --- #

def main_menu_inline_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📊 Поточний курс", callback_data="menu_rate")],
        [InlineKeyboardButton(text="🔔 Мої тригери", callback_data="menu_triggers")],
        [InlineKeyboardButton(text="➕ Створити тригер", callback_data="menu_create_trigger")],
        [InlineKeyboardButton(text="🏦 Налаштувати банки", callback_data="menu_banks")],
        [InlineKeyboardButton(text="⚙️ Налаштування", callback_data="menu_settings")],
        [InlineKeyboardButton(text="📈 Статистика", callback_data="menu_stats")],
        [InlineKeyboardButton(text="❓ Допомога", callback_data="menu_help")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def main_menu_reply_kb() -> ReplyKeyboardMarkup:
    buttons = [
        [
            KeyboardButton(text="📊 Поточний курс"),
            KeyboardButton(text="🔔 Мої тригери"),
        ],
        [
            KeyboardButton(text="➕ Створити тригер"),
            KeyboardButton(text="🏦 Налаштувати банки"),
        ],
        [
            KeyboardButton(text="⚙️ Налаштування"),
            KeyboardButton(text="📈 Статистика"),
        ],
        [
            KeyboardButton(text="❓ Допомога"),
        ],
    ]
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
    )


# --- Допоміжні функції для меню --- #

async def show_main_menu(message: Message):
    await message.answer(
        "Головне меню:",
        reply_markup=main_menu_inline_kb(),
    )


async def show_triggers_list(message: Message):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE chat_id=?;", (message.chat.id,))
        row = cur.fetchone()
        if not row:
            await message.answer("Користувача не знайдено.")
            return
        user_id = row["id"]

    triggers = get_user_triggers(user_id)
    if not triggers:
        await message.answer("У вас ще немає тригерів.")
        return

    text = "🔔 Ваші тригери:\n\n"
    for idx, t in enumerate(triggers, start=1):
        text += (
            f"{idx}. {t['name']} "
            f"(ID: {t['id']}, режим: {t['mode']}, статус: {t['status']})\n"
        )

    text += (
        "\nЩоб видалити тригер, надішліть команду:\n"
        "/deltrigger ID\n"
        "Наприклад: /deltrigger 12"
    )

    await message.answer(text)


async def show_banks_menu(message: Message):
    methods = get_payment_methods(enabled_only=False)
    if not methods:
        await message.answer(
            "Ще не завантажено жодних банків.\n"
            "Виконайте команду /rate, щоб бот отримав оголошення з Binance P2P\n"
            "і автоматично зберіг доступні платіжні методи."
        )
        return

    keyboard_rows = []
    for m in methods:
        status = "✅" if m["enabled"] else "❌"
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {m['display_name']}",
                    callback_data=f"toggle_bank_{m['id']}",
                )
            ]
        )

    keyboard_rows.append(
        [InlineKeyboardButton(text="⬅ Назад у меню", callback_data="back_to_main")]
    )

    kb = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    await message.answer(
        "🏦 Налаштування банків\n\n"
        "Натисніть на банк, щоб увімкнути/вимкнути його в списку доступних.\n"
        "Це впливає на список при створенні тригерів.",
        reply_markup=kb,
    )


async def show_settings_menu(message: Message):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE chat_id=?;", (message.chat.id,))
        row = cur.fetchone()
        if not row:
            await message.answer("Користувача не знайдено.")
            return
        user_id = row["id"]

    settings = get_user_settings(user_id)

    notif_status = "Увімкнені" if settings["notifications_enabled"] else "Вимкнені"
    interval = settings["default_monitor_interval"] or "не заданий (системний)"
    history_days = settings["history_days"] or f"системний ({config.history_retention_days} днів)"

    text = (
        "⚙️ Налаштування\n\n"
        f"Сповіщення: {notif_status}\n"
        f"Дефолтний інтервал моніторингу: {interval}\n"
        f"Зберігати історію цін: {history_days}\n\n"
        "Оберіть, що змінити:"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏱ Інтервал моніторингу", callback_data="set_def_interval"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📅 Дні зберігання історії", callback_data="set_history_days"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔕 Перемкнути сповіщення", callback_data="toggle_notifications"
                )
            ],
            [InlineKeyboardButton(text="⬅ Назад", callback_data="back_to_main")],
        ]
    )

    await message.answer(text, reply_markup=kb)


async def show_stats(message: Message):
    rows = get_price_history_last_24h()
    if not rows:
        await message.answer("Немає даних за останні 24 години.")
        return
    prices = [r["price"] for r in rows]
    min_p = min(prices)
    max_p = max(prices)
    await message.answer(
        "📈 Статистика за останні 24 години:\n\n"
        f"Найнижчий зафіксований курс: {min_p:.2f} UAH\n"
        f"Найвищий зафіксований курс: {max_p:.2f} UAH\n"
        f"Кількість спостережень: {len(rows)}"
    )


# --- СТАРТ --- #
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    get_or_create_user(
        chat_id=message.chat.id,
        username=message.from_user.username if message.from_user else None,
        first_name=message.from_user.first_name if message.from_user else None,
        last_name=message.from_user.last_name if message.from_user and message.from_user.last_name else None,
    )
    await state.clear()
    await message.answer(
        "Вітаю! Це бот моніторингу Binance P2P USDT/UAH.\n"
        "Скористайтеся меню нижче або кнопками внизу.",
        reply_markup=main_menu_reply_kb(),
    )
    await show_main_menu(message)


# --- ПОТОЧНИЙ КУРС --- #
@dp.message(Command("rate"))
async def cmd_rate(message: Message):
    try:
        result = await binance_client.fetch_ads(pay_types=[])
    except BinanceAPIError:
        await message.answer(
            "Неможливо отримати дані з Binance P2P. Сервіс тимчасово недоступний або змінився інтерфейс API."
        )
        return

    if not result.ads:
        await message.answer("Наразі немає доступних оголошень.")
        return

    prices = [a.price for a in result.ads]
    min_price = min(prices)
    max_price = max(prices)

    await message.answer(
        "📊 Поточний курс USDT/UAH\n\n"
        f"Мінімальна ціна: {min_price:.2f} UAH\n"
        f"Максимальна ціна: {max_price:.2f} UAH\n"
        f"Перевірено оголошень: {result.checked} (заявлено: {result.total})"
    )


@dp.callback_query(F.data == "menu_rate")
async def cb_menu_rate(call: CallbackQuery):
    await cmd_rate(call.message)
    await call.answer()


# --- REPLY-КНОПКИ ОСНОВНОГО МЕНЮ --- #

@dp.message(F.text == "📊 Поточний курс")
async def msg_btn_rate(message: Message):
    await cmd_rate(message)


@dp.message(F.text == "🔔 Мої тригери")
async def msg_btn_triggers(message: Message):
    await show_triggers_list(message)


@dp.message(F.text == "➕ Створити тригер")
async def msg_btn_create_trigger(message: Message, state: FSMContext):
    await cb_menu_create_trigger_from_message(message, state)


@dp.message(F.text == "🏦 Налаштувати банки")
async def msg_btn_banks(message: Message):
    await show_banks_menu(message)


@dp.message(F.text == "⚙️ Налаштування")
async def msg_btn_settings(message: Message):
    await show_settings_menu(message)


@dp.message(F.text == "📈 Статистика")
async def msg_btn_stats(message: Message):
    await show_stats(message)


@dp.message(F.text == "❓ Допомога")
async def msg_btn_help(message: Message):
    await message.answer(
        "❓ Допомога\n\n"
        "Бот моніторить Binance P2P USDT/UAH (продаж USDT за гривні).\n"
        "Використовуйте меню (кнопки в повідомленні) або кнопки підказок внизу.\n\n"
        "Основні функції:\n"
        "• 📊 Поточний курс — /rate або кнопка\n"
        "• 🔔 Мої тригери — список ваших тригерів\n"
        "• ➕ Створити тригер — майстер створення нового\n"
        "• 🏦 Налаштувати банки — вибір банків\n"
        "• ⚙️ Налаштування — глобальні параметри\n"
        "• 📈 Статистика — історія цін за 24 години\n\n"
        "Доступні команди:\n"
        "/start, /rate, /status, /deltrigger ID."
    )


# --- INLINE ДОПОМОГА --- #
@dp.callback_query(F.data == "menu_help")
async def cb_menu_help(call: CallbackQuery):
    await msg_btn_help(call.message)
    await call.answer()


# --- НАЛАШТУВАННЯ БАНКІВ (INLINE) --- #
@dp.callback_query(F.data == "menu_banks")
async def cb_menu_banks(call: CallbackQuery):
    await show_banks_menu(call.message)
    await call.answer()


@dp.callback_query(F.data.startswith("toggle_bank_"))
async def cb_toggle_bank(call: CallbackQuery):
    from database import get_payment_methods, set_payment_method_enabled

    try:
        pm_id = int(call.data.split("_", 2)[2])
    except (IndexError, ValueError):
        await call.answer("Помилка ідентифікатора банку.", show_alert=True)
        return

    methods = get_payment_methods(enabled_only=False)
    target = None
    for m in methods:
        if m["id"] == pm_id:
            target = m
            break

    if not target:
        await call.answer("Банк не знайдено.", show_alert=True)
        return

    new_enabled = not bool(target["enabled"])
    set_payment_method_enabled(pm_id, new_enabled)

    status = "увімкнено" if new_enabled else "вимкнено"
    await call.answer(f"Банк {target['display_name']} {status}.", show_alert=False)

    await show_banks_menu(call.message)


@dp.callback_query(F.data == "back_to_main")
async def cb_back_to_main(call: CallbackQuery):
    await show_main_menu(call.message)
    await call.answer()


# --- STATUS --- #
@dp.message(Command("status"))
async def cmd_status(message: Message):
    await message.answer(
        "Стан моніторингу: активний.\nПеревірка тригерів відбувається фоново.",
        reply_markup=main_menu_reply_kb(),
    )


# --- Мої тригери (INLINE) --- #
@dp.callback_query(F.data == "menu_triggers")
async def cb_menu_triggers(call: CallbackQuery):
    await show_triggers_list(call.message)
    await call.answer()


# --- Видалення тригера --- #
@dp.message(Command("deltrigger"))
async def cmd_deltrigger(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "Використання: /deltrigger ID_тригера\n"
            "Приклад: /deltrigger 1"
        )
        return

    try:
        trigger_id = int(parts[1])
    except ValueError:
        await message.answer("ID тригера має бути числом. Приклад: /deltrigger 1")
        return

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE chat_id=?;", (message.chat.id,))
        row = cur.fetchone()
        if not row:
            await message.answer("Користувача не знайдено.")
            return
        user_id = row["id"]

        cur.execute(
            "SELECT id, name FROM triggers WHERE id=? AND user_id=?;",
            (trigger_id, user_id),
        )
        trg = cur.fetchone()
        if not trg:
            await message.answer(
                "Тригер з таким ID не знайдено або він вам не належить."
            )
            return

    delete_trigger(trigger_id)
    await message.answer(f"Тригер ID {trigger_id} («{trg['name']}») видалено.")


# --- НАЛАШТУВАННЯ (INLINE) --- #
@dp.callback_query(F.data == "menu_settings")
async def cb_menu_settings(call: CallbackQuery):
    await show_settings_menu(call.message)
    await call.answer()


@dp.callback_query(F.data == "toggle_notifications")
async def cb_toggle_notifications(call: CallbackQuery):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE chat_id=?;", (call.message.chat.id,))
        row = cur.fetchone()
        if not row:
            await call.answer("Користувача не знайдено.", show_alert=True)
            return
        user_id = row["id"]

    settings = get_user_settings(user_id)
    current = settings["notifications_enabled"]
    new_value = 0 if current else 1
    set_user_settings(user_id, notifications_enabled=new_value)

    status = "вимкнено" if new_value == 0 else "увімкнено"
    await call.answer(f"Сповіщення {status}.", show_alert=True)

    await show_settings_menu(call.message)


@dp.callback_query(F.data == "set_def_interval")
async def cb_set_def_interval(call: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.entering_default_interval)
    await call.message.answer(
        "Введіть дефолтний інтервал моніторингу в секундах "
        "(наприклад: 60, 120, 300). "
        "Введіть 0, щоб повернутись до системного значення."
    )
    await call.answer()


@dp.message(SettingsStates.entering_default_interval)
async def msg_set_def_interval(message: Message, state: FSMContext):
    try:
        value = int(message.text.strip())
    except ValueError:
        await message.answer("Введіть ціле число в секундах (0, 30, 60, 120, 300, ...).")
        return

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE chat_id=?;", (message.chat.id,))
        row = cur.fetchone()
        if not row:
            await message.answer("Користувача не знайдено.")
            await state.clear()
            return
        user_id = row["id"]

    if value <= 0:
        set_user_settings(user_id, default_monitor_interval=None)
        await message.answer("Дефолтний інтервал скинуто. Використовується системний.")
    else:
        set_user_settings(user_id, default_monitor_interval=value)
        await message.answer(f"Дефолтний інтервал моніторингу змінено на {value} сек.")

    await state.clear()


@dp.callback_query(F.data == "set_history_days")
async def cb_set_history_days(call: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.entering_history_days)
    await call.message.answer(
        "Введіть кількість днів, протягом яких зберігати історію цін (наприклад: 1, 3, 7, 30).\n"
        "Введіть 0, щоб використовувати системне значення."
    )
    await call.answer()


@dp.message(SettingsStates.entering_history_days)
async def msg_set_history_days(message: Message, state: FSMContext):
    try:
        value = int(message.text.strip())
    except ValueError:
        await message.answer("Введіть ціле число (0, 1, 3, 7, 30, ...).")
        return

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE chat_id=?;", (message.chat.id,))
        row = cur.fetchone()
        if not row:
            await message.answer("Користувача не знайдено.")
            await state.clear()
            return
        user_id = row["id"]

    if value <= 0:
        set_user_settings(user_id, history_days=None)
        await message.answer(
            "Кількість днів історії скинуто. Використовується системне значення."
        )
    else:
        set_user_settings(user_id, history_days=value)
        await message.answer(f"Зберігання історії встановлено на {value} днів.")

    await state.clear()


# --- Створення тригера (INLINE/REPLY) --- #
@dp.callback_query(F.data == "menu_create_trigger")
async def cb_menu_create_trigger(call: CallbackQuery, state: FSMContext):
    await cb_menu_create_trigger_from_message(call.message, state)
    await call.answer()


async def cb_menu_create_trigger_from_message(message: Message, state: FSMContext):
    await state.set_state(CreateTriggerStates.choosing_mode)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Постійний", callback_data="mode_continuous"),
                InlineKeyboardButton(text="Перетин", callback_data="mode_cross"),
            ],
            [
                InlineKeyboardButton(
                    text="Разове сповіщення", callback_data="mode_one_time"
                )
            ],
            [InlineKeyboardButton(text="Скасувати", callback_data="create_cancel")],
        ]
    )
    await message.answer("Оберіть режим тригера:", reply_markup=kb)


@dp.callback_query(F.data.startswith("mode_"))
async def cb_choose_mode(call: CallbackQuery, state: FSMContext):
    mode_map = {
        "mode_continuous": "continuous",
        "mode_cross": "cross",
        "mode_one_time": "one_time",
    }
    mode = mode_map.get(call.data)
    if not mode:
        await call.answer("Невідомий режим.", show_alert=True)
        return
    await state.update_data(mode=mode)

    pms = get_payment_methods()
    if not pms:
        await call.message.answer(
            "Ще не відомі жодні банки. Спробуйте спочатку /rate, щоб завантажити список платіжних методів."
        )
        await call.answer()
        return

    kb_buttons = []
    for pm in pms:
        kb_buttons.append(
            [
                InlineKeyboardButton(
                    text=pm["display_name"], callback_data=f"bank_{pm['id']}"
                )
            ]
        )
    kb_buttons.append(
        [InlineKeyboardButton(text="Продовжити", callback_data="banks_done")]
    )
    kb_buttons.append(
        [InlineKeyboardButton(text="Скасувати", callback_data="create_cancel")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await state.set_state(CreateTriggerStates.choosing_banks)
    await call.message.answer("Оберіть один або кілька банків:", reply_markup=kb)
    await state.update_data(selected_banks=set())
    await call.answer()


@dp.callback_query(CreateTriggerStates.choosing_banks, F.data.startswith("bank_"))
async def cb_choose_bank(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get("selected_banks", set()))
    pm_id = int(call.data.split("_", 1)[1])
    if pm_id in selected:
        selected.remove(pm_id)
    else:
        selected.add(pm_id)
    await state.update_data(selected_banks=selected)
    await call.answer("Банк оновлено.")


@dp.callback_query(CreateTriggerStates.choosing_banks, F.data == "banks_done")
async def cb_banks_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_banks", set())
    if not selected:
        await call.answer("Оберіть хоча б один банк.", show_alert=True)
        return
    await state.set_state(CreateTriggerStates.choosing_threshold_mode)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Спільний поріг", callback_data="threshold_common"
                ),
                InlineKeyboardButton(
                    text="Окремий для кожного банку",
                    callback_data="threshold_per_bank",
                ),
            ],
            [InlineKeyboardButton(text="Скасувати", callback_data="create_cancel")],
        ]
    )
    await call.message.answer("Оберіть режим порогів:", reply_markup=kb)
    await call.answer()


@dp.callback_query(
    CreateTriggerStates.choosing_threshold_mode, F.data == "threshold_common"
)
async def cb_threshold_common(call: CallbackQuery, state: FSMContext):
    await state.update_data(common_threshold=True)
    await state.set_state(CreateTriggerStates.entering_threshold_common)
    await call.message.answer(
        "Введіть цільовий курс (UAH/USDT) для спільного порогу, наприклад: 42.15"
    )
    await call.answer()


@dp.message(CreateTriggerStates.entering_threshold_common)
async def msg_threshold_common(message: Message, state: FSMContext):
    try:
        value = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Невірний формат числа. Спробуйте ще раз.")
        return
    await state.update_data(threshold_common=value)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Курс ≥ порогу", callback_data="cond_ge"),
                InlineKeyboardButton(text="Курс ≤ порогу", callback_data="cond_le"),
            ],
            [
                InlineKeyboardButton(
                    text="Перетин знизу вгору", callback_data="cond_cross_up"
                ),
                InlineKeyboardButton(
                    text="Перетин зверху вниз", callback_data="cond_cross_down"
                ),
            ],
        ]
    )
    await state.set_state(CreateTriggerStates.choosing_condition)
    await message.answer("Оберіть умову спрацювання:", reply_markup=kb)


@dp.callback_query(
    CreateTriggerStates.choosing_threshold_mode, F.data == "threshold_per_bank"
)
async def cb_threshold_per_bank(call: CallbackQuery, state: FSMContext):
    await state.update_data(common_threshold=False)
    await state.set_state(CreateTriggerStates.entering_threshold_per_bank)
    data = await state.get_data()
    selected = data.get("selected_banks", set())
    pms = {pm["id"]: pm for pm in get_payment_methods()}

    text = "Введіть пороги у форматі:\nbank_id курс\nНаприклад:\n"
    for pm_id in selected:
        pm = pms.get(pm_id)
        if pm:
            text += f"{pm_id} {pm['display_name']}: 42.10\n"
    text += "\nНадішліть список порогів одним повідомленням."

    await call.message.answer(text)
    await call.answer()


@dp.message(CreateTriggerStates.entering_threshold_per_bank)
async def msg_threshold_per_bank(message: Message, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_banks", set())
    threshold_map = {}
    lines = message.text.splitlines()
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pm_id = int(parts[0])
            if pm_id not in selected:
                continue
            value = float(parts[1].replace(",", "."))
            threshold_map[pm_id] = value
        except ValueError:
            continue
    if not threshold_map:
        await message.answer("Не вдалося розпізнати жоден поріг. Спробуйте ще раз.")
        return
    await state.update_data(threshold_map=threshold_map)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Курс ≥ порогу", callback_data="cond_ge"),
                InlineKeyboardButton(text="Курс ≤ порогу", callback_data="cond_le"),
            ],
            [
                InlineKeyboardButton(
                    text="Перетин знизу вгору", callback_data="cond_cross_up"
                ),
                InlineKeyboardButton(
                    text="Перетин зверху вниз", callback_data="cond_cross_down"
                ),
            ],
        ]
    )
    await state.set_state(CreateTriggerStates.choosing_condition)
    await message.answer("Оберіть умову спрацювання:", reply_markup=kb)


@dp.callback_query(CreateTriggerStates.choosing_condition, F.data.startswith("cond_"))
async def cb_choose_condition(call: CallbackQuery, state: FSMContext):
    cond_map = {
        "cond_ge": "ge",
        "cond_le": "le",
        "cond_cross_up": "cross_up",
        "cond_cross_down": "cross_down",
    }
    cond = cond_map.get(call.data)
    if not cond:
        await call.answer("Невідомий режим.", show_alert=True)
        return
    await state.update_data(condition_type=cond)
    await state.set_state(CreateTriggerStates.entering_min_amount)
    await call.message.answer(
        "Введіть мінімальну суму оголошення в UAH (або 0, якщо неважливо):"
    )
    await call.answer()


@dp.message(CreateTriggerStates.entering_min_amount)
async def msg_min_amount(message: Message, state: FSMContext):
    try:
        value = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Невірний формат числа. Спробуйте ще раз.")
        return
    await state.update_data(min_amount_uah=value if value > 0 else None)
    await state.set_state(CreateTriggerStates.entering_max_amount)
    await message.answer(
        "Введіть максимальну суму оголошення в UAH (або 0, якщо неважливо):"
    )


@dp.message(CreateTriggerStates.entering_max_amount)
async def msg_max_amount(message: Message, state: FSMContext):
    try:
        value = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Невірний формат числа. Спробуйте ще раз.")
        return
    await state.update_data(max_amount_uah=value if value > 0 else None)
    await state.set_state(CreateTriggerStates.entering_min_order)
    await message.answer(
        "Введіть мінімальний ліміт угоди продавця (мінімальна сума однієї угоди, UAH, або 0):"
    )


@dp.message(CreateTriggerStates.entering_min_order)
async def msg_min_order(message: Message, state: FSMContext):
    try:
        value = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Невірний формат числа. Спробуйте ще раз.")
        return
    await state.update_data(min_order_uah=value if value > 0 else None)
    await state.set_state(CreateTriggerStates.entering_max_order)
    await message.answer(
        "Введіть максимальний ліміт угоди продавця "
        "(максимальна сума однієї угоди, UAH, або 0, якщо неважливо):"
    )


@dp.message(CreateTriggerStates.entering_max_order)
async def msg_max_order(message: Message, state: FSMContext):
    # НОВИЙ крок – максимальний ліміт угоди продавця
    try:
        value = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Невірний формат числа. Спробуйте ще раз.")
        return
    await state.update_data(max_order_uah=value if value > 0 else None)
    await state.set_state(CreateTriggerStates.entering_interval)
    await message.answer(
        "Оберіть інтервал перевірки (в секундах): 30, 60, 120 або 300."
    )


@dp.message(CreateTriggerStates.entering_interval)
async def msg_interval(message: Message, state: FSMContext):
    try:
        value = int(message.text.strip())
    except ValueError:
        await message.answer("Введіть ціле число: 30, 60, 120 або 300.")
        return
    if value not in (30, 60, 120, 300):
        await message.answer("Допустимі значення: 30, 60, 120, 300.")
        return
    await state.update_data(interval_sec=value)
    data = await state.get_data()
    if data.get("mode") == "continuous":
        await state.set_state(CreateTriggerStates.choosing_repeat)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Увімкнути повторні сповіщення", callback_data="repeat_on"
                    ),
                    InlineKeyboardButton(
                        text="Вимкнути", callback_data="repeat_off"
                    ),
                ]
            ]
        )
        await message.answer("Увімкнути повторні сповіщення?", reply_markup=kb)
    else:
        await finalize_trigger(message, state)


@dp.callback_query(
    CreateTriggerStates.choosing_repeat, F.data.in_(("repeat_on", "repeat_off"))
)
async def cb_choose_repeat(call: CallbackQuery, state: FSMContext):
    if call.data == "repeat_off":
        await state.update_data(repeat_enabled=False, repeat_interval_sec=None)
        await finalize_trigger(call.message, state)
        await call.answer()
        return

    await state.update_data(repeat_enabled=True)
    await state.set_state(CreateTriggerStates.entering_repeat_interval)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1 хв", callback_data="rep_60"),
                InlineKeyboardButton(text="5 хв", callback_data="rep_300"),
            ],
            [
                InlineKeyboardButton(text="15 хв", callback_data="rep_900"),
                InlineKeyboardButton(text="30 хв", callback_data="rep_1800"),
            ],
            [InlineKeyboardButton(text="60 хв", callback_data="rep_3600")],
        ]
    )
    await call.message.answer(
        "Оберіть інтервал повторних сповіщень:", reply_markup=kb
    )
    await call.answer()


@dp.callback_query(
    CreateTriggerStates.entering_repeat_interval, F.data.startswith("rep_")
)
async def cb_repeat_interval(call: CallbackQuery, state: FSMContext):
    sec = int(call.data.split("_", 1)[1])
    await state.update_data(repeat_interval_sec=sec)
    await finalize_trigger(call.message, state)
    await call.answer()


async def finalize_trigger(message: Message, state: FSMContext):
    data = await state.get_data()

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE chat_id=?;", (message.chat.id,))
        row = cur.fetchone()
        if not row:
            await message.answer("Помилка: користувача не знайдено.")
            await state.clear()
            return
        user_id = row["id"]

    pms = {pm["id"]: pm for pm in get_payment_methods()}
    selected_banks = data.get("selected_banks", set())
    banks_names = [
        pms[pm_id]["display_name"] for pm_id in selected_banks if pm_id in pms
    ]
    name = f"Тригер: {', '.join(banks_names)}"

    trigger_id = create_trigger(
        user_id=user_id,
        name=name,
        mode=data["mode"],
        common_threshold=data.get("common_threshold", True),
        condition_type=data["condition_type"],
        # зберігаємо спільний поріг у min_amount_uah для простоти
        min_amount_uah=data.get("threshold_common")
        if data.get("common_threshold", True)
        else None,
        max_amount_uah=data.get("max_amount_uah"),
        min_order_uah=data.get("min_order_uah"),
        max_order_uah=data.get("max_order_uah"),  # ВАЖЛИВО: пишемо максимум
        interval_sec=data.get("interval_sec", 60),
        repeat_enabled=data.get("repeat_enabled", False),
        repeat_interval_sec=data.get("repeat_interval_sec"),
    )

    if data.get("common_threshold", True):
        common_thr = data.get("threshold_common")
        for pm_id in selected_banks:
            add_trigger_bank(
                trigger_id=trigger_id,
                payment_method_id=pm_id,
                use_separate_threshold=False,
                threshold_price=common_thr,
            )
    else:
        thr_map = data.get("threshold_map", {})
        for pm_id in selected_banks:
            thr = thr_map.get(pm_id)
            add_trigger_bank(
                trigger_id=trigger_id,
                payment_method_id=pm_id,
                use_separate_threshold=True,
                threshold_price=thr,
            )

    await message.answer(f"Тригер створено (ID {trigger_id}).")
    await state.clear()


@dp.callback_query(F.data == "create_cancel")
async def cb_create_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Створення тригера скасовано.")
    await call.answer()


# --- Статистика (INLINE) --- #
@dp.callback_query(F.data == "menu_stats")
async def cb_menu_stats(call: CallbackQuery):
    await show_stats(call.message)
    await call.answer()


# --- Моніторинг --- #
async def run_monitoring():
    stop_event = asyncio.Event()
    tasks = []
    for i in range(config.monitor_worker_count):
        tasks.append(asyncio.create_task(monitor_worker(bot, i + 1, stop_event)))
    return stop_event, tasks


async def main():
    init_db()
    stop_event, tasks = await run_monitoring()
    try:
        await dp.start_polling(bot)
    finally:
        stop_event.set()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
