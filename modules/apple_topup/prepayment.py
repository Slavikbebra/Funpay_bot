"""Предоплатный сценарий Apple TopUp: меню, временные цены и очередь по лотам."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from FunPayAPI.common.enums import OrderStatuses

from . import storage

logger = logging.getLogger("modules.apple_topup.prepayment")
logger.info("🔥 PREPAYMENT VERSION: SPLIT PRICE v4")

RESERVATION_SECONDS = 5 * 60
POLL_SECONDS = 2
DATA_FILE = Path("bot_data") / "apple_topup_preorders.json"
PRICES_FILE = Path(__file__).with_name("prepayment_prices.json")

PRODUCTS = {
    "1": {"name": "GO — 1 месяц", "lot_id": "72854958", "buyer_price": 90, "temporary_price": 83.368},
    "2": {"name": "GO — 3 месяца", "lot_id": "73376764", "buyer_price": 250, "temporary_price": 231.595},
    "3": {"name": "GO — 6 месяцев", "lot_id": "73377060", "buyer_price": 450, "temporary_price": 416.878},
    "4": {"name": "GO — 12 месяцев", "lot_id": "72855225", "buyer_price": 950, "temporary_price": 880.085},
    "5": {"name": "GO+ — 1 месяц", "lot_id": "72853562", "buyer_price": 130, "temporary_price": 120.425},
    "6": {"name": "GO+ — 3 месяца", "lot_id": "73377249", "buyer_price": 400, "temporary_price": 370.557},
    "7": {"name": "GO+ — 6 месяцев", "lot_id": "73377384", "buyer_price": 750, "temporary_price": 694.802},
    "8": {"name": "GO+ — 12 месяцев", "lot_id": "72854289", "buyer_price": 1450, "temporary_price": 1343.293},
}

def _load_restore_prices() -> dict[str, float]:
    """Возвращает фиксированные цены, к которым нужно откатывать лоты.

    Эти цены не берутся из состояния очереди: даже если состояние было
    повреждено или в нём сохранилась временная цена, восстановление всегда
    выполняется по отдельному конфигурационному файлу.
    """
    try:
        with PRICES_FILE.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        result = {}
        for lot_id, item in raw.items():
            result[str(lot_id)] = float(item["restore_price"])
        return result
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.exception("Не удалось загрузить файл цен восстановления %s: %s", PRICES_FILE, exc)
        raise RuntimeError(f"Не удалось загрузить цены восстановления: {PRICES_FILE}") from exc


def _restore_price_for_lot(lot_id: str) -> float:
    prices = _load_restore_prices()
    key = str(lot_id)
    if key not in prices:
        raise KeyError(f"Для лота {lot_id} не задана restore_price в {PRICES_FILE}")
    return prices[key]


MAIN_MENU = """👋 Добро пожаловать, {username}, в SlavikStore⚡️

Если вы готовы приобрести подписку и хотите увидеть актуальные цены:
👉 !ПРАЙС

📖 Если у вас есть вопросы по оформлению подписки:
👉 !ИНСТРУКЦИЯ

💬 Если у вас другой вопрос:
👉 !ПРОДАВЕЦ"""

INSTRUCTION = """📖 **КАК ПРОХОДИТ ОФОРМЛЕНИЕ ПОДПИСКИ**

Всё происходит автоматически и занимает всего несколько минут:

1️⃣ Выбираете нужную подписку в !ПРАЙС.

2️⃣ Бот подготавливает для вас объявление на FunPay с актуальной стоимостью и отправляет ссылку на оплату.

3️⃣ После оплаты бот автоматически начинает оформление подписки и отправляет вам необходимые инструкции.

4️⃣ В процессе оформления бот отправит вам **подробную инструкцию по смене региона App Store (Турция)**, это потребуется для активации подписки.

5️⃣ Следуете указаниям бота — после выполнения необходимых действий подписка будет оформлена.

🤖 **Весь процесс проходит через автоматическую систему выдачи, поэтому после оплаты просто следуйте сообщениям бота.**

⚠️ Перед оплатой внимательно проверьте выбранный тариф и сумму.

📋 !МЕНЮ — вернуться в главное меню."""

SELLER = """💬 **Запрос продавцу отправлен!**

Для ответа на ваш вопрос я уже вызвал продавца в чат.

В течение нескольких минут он вам ответит.

⏳ Пожалуйста, оставайтесь в чате и ожидайте ответа."""

PRICE_MAIN = """📋 АКТУАЛЬНЫЙ ПРАЙС 📋

🔥 ТАРИФЫ GO:

1️⃣ 1 месяц — 90 ₽
2️⃣ 3 месяца — 250 ₽
3️⃣ 6 месяцев — 450 ₽
4️⃣ 12 месяцев — 950 ₽

🔥 ТАРИФЫ GO+:

5️⃣ 1 месяц — 130 ₽
6️⃣ 3 месяца — 400 ₽
7️⃣ 6 месяцев — 750 ₽
8️⃣ 12 месяцев — 1450 ₽"""

PRICE_INFO = """💬 **Выберите нужный тариф**

Напишите номер тарифа от **1 до 8**.

⚠️ **ВНИМАНИЕ!**
Это итоговая стоимость на FunPay.
Заплатите ровно столько, сколько указано выше.

📋 !МЕНЮ — вернуться в главное меню."""

PREPARED = """✨ ЗАКАЗ ПОДГОТОВЛЕН! ✨

📦 Тариф: {name}
💰 К оплате: {buyer_price} ₽

🔗 Ссылка на оплату:
{link}

⏳ У вас есть 5 минут на оплату.

⚠️ Заплатите ровно {buyer_price} ₽, указанную выше сумму.

🤖 После оплаты следуйте указаниям бота для автоматической выдачи заказа.

🔄 После истечения 5 минут цена автоматически вернётся к исходной.

🤝 С уважением, SlavikStore⚡️"""

WAITING = """⏳ Сейчас этот тариф уже оформляется для другого покупателя.

Вы добавлены в очередь.

Как только предыдущий покупатель оплатит или его время истечёт, бот автоматически подготовит объявление для вас и отправит ссылку на оплату."""

INVALID_NUMBER = "❌ Напишите номер тарифа от 1 до 8 или используйте !МЕНЮ."

_lock = asyncio.Lock()
_worker_task: asyncio.Task | None = None

# Защита от автоматической выдачи меню конкурентам:
# проверяем профиль FunPay отправителя и ищем активные SoundCloud-лоты.
# Проверка выполняется в отдельной задаче и сетевой запрос запускается
# через to_thread(), поэтому медленный запрос профиля не блокирует
# обработку сообщений других покупателей.
SOUNDCLOUD_PROFILE_CACHE_SECONDS = 10 * 60
SOUNDCLOUD_MARKERS = (
    "soundcloud",
    "sound cloud",
    "soundcloud go",
    "soundcloud go+",
    "soundcloud go plus",
)
_profile_cache: dict[int, tuple[float, bool, str | None]] = {}
_profile_user_locks: dict[str, asyncio.Lock] = {}
_profile_cache_lock = asyncio.Lock()

# Если покупатель уже имеет любой заказ в магазине, обычные сообщения
# в этом чате не должны запускать приветственное меню предоплаты.
# Команды !ПРАЙС / !ИНСТРУКЦИЯ / !МЕНЮ при этом остаются доступными.
EXISTING_SALE_CACHE_SECONDS = 10 * 60
_existing_sale_cache: dict[str, tuple[float, bool]] = {}
_existing_sale_cache_lock = asyncio.Lock()




def _profile_has_soundcloud_lot_sync(bot, author_id: int) -> tuple[bool, str | None]:
    """Синхронно проверяет публичные активные лоты профиля FunPay."""
    profile = bot.account.get_user(int(author_id))
    for lot in profile.get_lots():
        title = str(getattr(lot, "title", None) or getattr(lot, "description", None) or "")
        haystack = title.casefold().replace("ё", "е")
        if any(marker in haystack for marker in SOUNDCLOUD_MARKERS):
            return True, title
    return False, None


def _has_existing_sale_sync(bot, username: str) -> bool:
    """Проверяет, есть ли у покупателя уже заказ в продажах FunPay."""
    result = bot.account.get_sales()
    sales = result[1] if isinstance(result, tuple) else result
    username_cf = str(username).casefold()
    for order in sales or []:
        buyer = str(getattr(order, "buyer_username", "") or "").casefold()
        if buyer == username_cf:
            return True
    return False


async def _has_existing_sale(bot, username: str) -> bool:
    """Кешированно проверяет наличие любого предыдущего заказа."""
    key = str(username).casefold()
    now = time.time()
    async with _existing_sale_cache_lock:
        cached = _existing_sale_cache.get(key)
        if cached and now - cached[0] < EXISTING_SALE_CACHE_SECONDS:
            return cached[1]

    try:
        found = await asyncio.to_thread(_has_existing_sale_sync, bot, username)
    except Exception:
        logger.exception(
            "PREPAYMENT: не удалось проверить существующие заказы покупателя %s",
            username,
        )
        # При ошибке проверки не блокируем команды/обычную логику.
        return False

    async with _existing_sale_cache_lock:
        _existing_sale_cache[key] = (time.time(), found)
    return found


async def _is_soundcloud_seller(bot, username: str, author_id) -> bool:
    """Возвращает True, если в профиле пользователя найден SoundCloud-лот."""
    try:
        author_id = int(author_id)
    except (TypeError, ValueError):
        # Без ID профиля проверить лоты надёжно нельзя. В этом случае
        # не блокируем обычного покупателя.
        logger.warning(
            "🛡 PREPAYMENT: не удалось получить author_id для %s; проверка профиля пропущена",
            username,
        )
        return False

    now = time.time()
    async with _profile_cache_lock:
        cached = _profile_cache.get(author_id)
        if cached and now - cached[0] < SOUNDCLOUD_PROFILE_CACHE_SECONDS:
            return cached[1]

    try:
        found, lot_title = await asyncio.to_thread(
            _profile_has_soundcloud_lot_sync, bot, author_id
        )
    except Exception:
        # Ошибка проверки профиля не должна задерживать/ломать сообщения
        # остальных покупателей.
        logger.exception(
            "🛡 PREPAYMENT: ошибка проверки профиля FunPay %s (%s)",
            username, author_id,
        )
        return False

    async with _profile_cache_lock:
        _profile_cache[author_id] = (time.time(), found, lot_title)

    if found:
        logger.info(
            "🛡 PREPAYMENT: %s заблокирован — обнаружен SoundCloud-лот в профиле: %r",
            username, lot_title,
        )
    else:
        logger.info(
            "🛡 PREPAYMENT: профиль %s проверен — SoundCloud-лоты не найдены",
            username,
        )
    return found


def _load() -> dict:
    DATA_FILE.parent.mkdir(exist_ok=True)
    if not DATA_FILE.exists():
        return {"users": {}, "lots": {}}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("users", {})
        data.setdefault("lots", {})
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Не удалось загрузить состояние предоплатной очереди; состояние будет сброшено: %s", exc)
        return {"users": {}, "lots": {}}


def _save(data: dict) -> None:
    DATA_FILE.parent.mkdir(exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(DATA_FILE)


def _user(data: dict, username: str) -> dict | None:
    return data["users"].get(username.lower())


def _set_user(data: dict, username: str, **values) -> dict:
    key = username.lower()
    current = data["users"].setdefault(key, {"username": username, "state": "MENU"})
    current.update(values)
    return current


def _remove_from_queue(data: dict, session_id: str) -> None:
    for lot in data["lots"].values():
        lot["waiting"] = [x for x in lot.get("waiting", []) if x.get("session_id") != session_id]


def _find_session(data: dict, username: str):
    u = _user(data, username)
    if not u or not u.get("session_id"):
        return None, None
    sid = u["session_id"]
    for lot_id, lot in data["lots"].items():
        if lot.get("active", {}).get("session_id") == sid:
            return lot_id, lot["active"]
        for item in lot.get("waiting", []):
            if item.get("session_id") == sid:
                return lot_id, item
    return None, None


def _public_link(lot_id: str) -> str:
    return f"https://funpay.com/lots/offer?id={lot_id}"


def _set_lot_price(bot, lot_id: str, price: float) -> None:
    fields = bot.account.get_lot_fields(int(lot_id))
    fields.price = float(price)
    fields.renew_fields()
    bot.account.save_lot(fields)

    # Обязательная повторная проверка после сохранения.
    verify = bot.account.get_lot_fields(int(lot_id))
    actual = float(verify.price)
    if abs(actual - float(price)) > 0.001:
        raise RuntimeError(
            f"FunPay не подтвердил цену лота {lot_id}: ожидалось {price}, получено {actual}"
        )


def _get_lot_price_sync(bot, lot_id: str) -> float:
    fields = bot.account.get_lot_fields(int(lot_id))
    return float(fields.price)


async def _set_lot_price_checked(bot, lot_id: str, price: float) -> None:
    await asyncio.to_thread(_set_lot_price, bot, lot_id, price)


async def _read_lot_price(bot, lot_id: str) -> float:
    return await asyncio.to_thread(_get_lot_price_sync, bot, lot_id)


async def _call_seller(username: str, chat_id, bot) -> None:
    try:
        from tgbot.telegrambot import get_telegram_bot, get_telegram_bot_loop
        telegram_bot = get_telegram_bot()
        telegram_loop = get_telegram_bot_loop()
        asyncio.run_coroutine_threadsafe(
            telegram_bot.call_seller(username, chat_id),
            telegram_loop,
        )
    except Exception:
        logger.exception("Ошибка вызова продавца для %s", username)


def _ensure_worker(bot) -> None:
    """Гарантирует, что фоновый таймер предоплат запущен."""
    global _worker_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _worker_task is None or _worker_task.done():
        _worker_task = loop.create_task(_worker(bot))
        logger.info("🍎 Apple TopUp: фоновый таймер предоплаты запущен/перезапущен")


async def _prepare_active(bot, session: dict, lot: dict) -> None:
    _ensure_worker(bot)
    product = PRODUCTS[session["product_number"]]
    lot_id = session["lot_id"]
    # Перед изменением цены ещё раз убеждаемся, что лот доступен для редактирования.
    await _read_lot_price(bot, lot_id)
    await _set_lot_price_checked(bot, lot_id, product["temporary_price"])

    _send_prepaid_message(
        bot,
        session["chat_id"],
        PREPARED.format(
            name=product["name"],
            buyer_price=product["buyer_price"],
            link=_public_link(lot_id),
        ),
    )


async def _activate_next(bot, lot_id: str, *, reason: str) -> None:
    data = _load()
    lot = data["lots"].get(lot_id)
    if not lot or lot.get("active"):
        return

    waiting = lot.get("waiting", [])
    if not waiting:
        return

    session = waiting.pop(0)
    lot["active"] = session
    session["status"] = "ACTIVE"
    session["expires_at"] = time.time() + RESERVATION_SECONDS
    _set_user(
        data,
        session["username"],
        state="ACTIVE",
        session_id=session["session_id"],
        lot_id=lot_id,
    )
    _save(data)

    try:
        await _prepare_active(bot, session, lot)
        logger.info("Очередь: активирован %s для лота %s (%s)", session["username"], lot_id, reason)
    except Exception:
        logger.exception("Очередь: не удалось активировать %s для лота %s", session["username"], lot_id)
        # Не оставляем лот в неизвестном состоянии.
        try:
            await _set_lot_price_checked(bot, lot_id, _restore_price_for_lot(lot_id))
        except Exception:
            logger.exception("Не удалось вернуть исходную цену лота %s", lot_id)
        data = _load()
        lot = data["lots"].get(lot_id)
        if lot:
            lot["active"] = None
            _set_user(data, session["username"], state="MENU", session_id=None, lot_id=None)
            _save(data)
        await _activate_next(bot, lot_id, reason="ошибка активации")


async def _release_active(bot, lot_id: str, *, reason: str, payment_order=None, final_state: str | None = None) -> bool:
    async with _lock:
        data = _load()
        lot = data["lots"].get(lot_id)
        if not lot or not lot.get("active"):
            logger.warning("Предоплата: лот %s уже не имеет активной заявки", lot_id)
            return False
        active = lot["active"]

        if payment_order is not None:
            if str(payment_order.buyer_username).lower() != str(active["username"]).lower():
                return False

        original_price = _restore_price_for_lot(lot_id)
        restore_error = None
        restored = False
        for attempt in range(1, 4):
            try:
                logger.info(
                    "Предоплата: восстанавливаю цену лота %s -> %s (попытка %s/3, причина=%s)",
                    lot_id, original_price, attempt, reason,
                )
                await _set_lot_price_checked(bot, lot_id, original_price)
                restored = True
                break
            except Exception as exc:
                restore_error = exc
                logger.exception(
                    "Предоплата: ошибка восстановления цены лота %s, попытка %s/3",
                    lot_id, attempt,
                )
                if attempt < 3:
                    await asyncio.sleep(2)

        if not restored:
            logger.error(
                "Предоплата: НЕ УДАЛОСЬ восстановить цену лота %s после 3 попыток: %s",
                lot_id, restore_error,
            )
            return False

        _set_user(
            data, active["username"],
            state=final_state or ("PAID" if payment_order else "MENU"),
            session_id=None,
            lot_id=None,
        )
        lot["active"] = None
        _save(data)

        await _activate_next(bot, lot_id, reason=reason)
        return True


def _product_for_lot(lot_id: str):
    for product in PRODUCTS.values():
        if product["lot_id"] == str(lot_id):
            return product
    return None


def _find_paid_sale(bot, username: str, lot_id: str):
    try:
        result = bot.account.get_sales()
        sales = result[1] if isinstance(result, tuple) else result
        for order in sales or []:
            if str(getattr(order, "buyer_username", "")).lower() != username.lower():
                continue
            if getattr(order, "status", None) != OrderStatuses.PAID:
                continue
            # У оплаченного заказа offer_id доступен в полном объекте заказа.
            try:
                full = bot.account.get_order(order.id)
                offer_id = full.get_field_value("offer_id")
                if isinstance(offer_id, dict):
                    offer_id = offer_id.get("ru") or offer_id.get("en")
                if str(offer_id) == str(lot_id):
                    return full
            except Exception:
                continue
    except Exception:
        logger.exception("Не удалось проверить продажи на границе таймера")
    return None


async def handle_refund(bot, order) -> None:
    """Освобождает предоплатную заявку при возврате уже оплаченного заказа."""
    username = getattr(order, "buyer_username", None)
    if not username:
        return
    lot_id = None
    try:
        lot_id = order.get_field_value("offer_id")
        if isinstance(lot_id, dict):
            lot_id = lot_id.get("ru") or lot_id.get("en")
    except Exception:
        return
    if not lot_id:
        return
    product = _product_for_lot(str(lot_id))
    if not product:
        return
    released = await _release_active(bot, str(lot_id), reason="возврат", payment_order=order, final_state="MENU")
    if released:
        logger.info("🔄 Предоплата: заявка %s освобождена после возврата заказа %s", username, getattr(order, "id", "?"))


async def handle_payment(bot, order, lot_id_override: str | None = None) -> None:
    """Освобождает оплаченный лот и сразу переключает его на следующего покупателя.

    lot_id_override используется, когда FunPay не передаёт offer_id в объекте
    заказа, но Apple TopUp уже надёжно определил лот по названию заказа.
    """
    username = getattr(order, "buyer_username", None)
    if not username:
        return
    lot_id = str(lot_id_override).strip() if lot_id_override else None
    if not lot_id:
        try:
            lot_id = order.get_field_value("offer_id")
            if isinstance(lot_id, dict):
                lot_id = lot_id.get("ru") or lot_id.get("en")
        except Exception:
            return
    if not lot_id:
        logger.warning(
            "Предоплата: не удалось определить lot_id для оплаченного заказа %s",
            getattr(order, "id", "?")
        )
        return
    product = _product_for_lot(str(lot_id))
    if not product:
        return
    await _release_active(bot, str(lot_id), reason="оплата", payment_order=order)


async def _expire_once(bot) -> None:
    now = time.time()
    data = _load()
    lots = data.get("lots", {})
    for lot_id in list(lots):
        data = _load()
        lot = data["lots"].get(lot_id)
        if not lot or not lot.get("active"):
            continue
        active = lot["active"]
        expires_at = float(active.get("expires_at", 0) or 0)
        if expires_at <= 0 or expires_at > now:
            continue

        logger.info(
            "⏰ Предоплата: истёк таймер для %s, лот %s (expires_at=%s, now=%s)",
            active["username"], lot_id, expires_at, now,
        )

        paid = await asyncio.to_thread(
            _find_paid_sale, bot, active["username"], lot_id
        )
        if paid is not None:
            logger.info(
                "💳 Предоплата: найден оплаченный заказ %s на границе таймера для %s",
                getattr(paid, "id", "?"), active["username"],
            )
            await _release_active(
                bot, lot_id,
                reason="оплата на границе таймера",
                payment_order=paid,
            )
            continue

        released = await _release_active(
            bot, lot_id, reason="истечение времени"
        )

        if released:
            _send_prepaid_message(
                bot,
                active["chat_id"],
                "❌ ВРЕМЯ ОПЛАТЫ ИСТЕКЛО\n\n"
                "Заказ не был оплачен в течение 5 минут.\n\n"
                "💰 Цена объявления восстановлена до исходной.\n\n"
                "📋 !ПРАЙС — выбрать тариф и оформить новый заказ.",
            )
        else:
            _send_prepaid_message(
                bot,
                active["chat_id"],
                "❌ ВРЕМЯ ОПЛАТЫ ИСТЕКЛО\n\n"
                "Заказ не был оплачен в течение 5 минут.\n\n"
                "⚠️ Бот пока не смог восстановить исходную цену объявления.\n"
                "Он автоматически повторит попытку. Пожалуйста, не оплачивайте эту заявку.",
            )


async def _worker(bot) -> None:
    logger.info("🍎 Предоплатный таймер: worker активен")
    while True:
        try:
            await _expire_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка фонового обработчика предоплатной очереди")
        await asyncio.sleep(POLL_SECONDS)


async def on_funpay_bot_init(bot) -> None:
    _ensure_worker(bot)
    logger.info("🍎 Apple TopUp: предоплатная очередь и таймер инициализированы")


def _send_prepaid_message(bot, chat_id, text: str) -> bool:
    """Отправляет предоплатные сообщения без глобального watermark."""
    try:
        result = bot.send_message(chat_id, text, exclude_watermark=True)
        if result is None:
            logger.error("PREPAYMENT DEBUG: сообщение не отправлено в чат %s", chat_id)
            return False
        return True
    except Exception:
        logger.exception("PREPAYMENT DEBUG: ошибка отправки сообщения в чат %s", chat_id)
        return False


async def _show_menu(bot, username: str, chat_id) -> None:
    _send_prepaid_message(bot, chat_id, MAIN_MENU.format(username=username))


async def _show_price(bot, username: str, chat_id) -> None:
    data = _load()
    _set_user(data, username, state="BROWSING", session_id=None, lot_id=None)
    _save(data)
    logger.info("PREPAYMENT DEBUG: отправляю PRICE пользователю %s", username)
    _send_prepaid_message(bot, chat_id, PRICE_MAIN)
    _send_prepaid_message(bot, chat_id, PRICE_INFO)


async def _show_instruction(bot, username: str, chat_id) -> None:
    data = _load()
    _set_user(data, username, state="MENU", session_id=None, lot_id=None)
    _save(data)
    logger.info("PREPAYMENT DEBUG: отправляю INSTRUCTION пользователю %s", username)
    _send_prepaid_message(bot, chat_id, INSTRUCTION)


async def _seller(bot, username: str, chat_id) -> None:
    data = _load()
    _set_user(data, username, state="WAITING_SELLER", session_id=None, lot_id=None)
    _save(data)
    _send_prepaid_message(bot, chat_id, SELLER)
    await _call_seller(username, chat_id, bot)


async def _select_product(bot, username: str, chat_id, number: str) -> None:
    product = PRODUCTS[number]
    async with _lock:
        data = _load()
        existing_lot_id, existing = _find_session(data, username)
        if existing:
            _send_prepaid_message(bot, chat_id, "⏳ У вас уже есть активная заявка. Дождитесь её завершения или оплаты.")
            return

        lot_id = product["lot_id"]
        lot = data["lots"].setdefault(lot_id, {"active": None, "waiting": []})

        session = {
            "session_id": uuid.uuid4().hex,
            "username": username,
            "chat_id": str(chat_id),
            "product_number": number,
            "lot_id": lot_id,
            "created_at": time.time(),
            "expires_at": None,
            "status": "WAITING",
        }

        if lot.get("active"):
            lot["waiting"].append(session)
            _set_user(data, username, state="QUEUED", session_id=session["session_id"], lot_id=lot_id)
            _save(data)
            _send_prepaid_message(bot, chat_id, WAITING)
            return

        # Ставим временную цену только после проверок выше.
        lot["active"] = session
        session["status"] = "ACTIVE"
        session["expires_at"] = time.time() + RESERVATION_SECONDS
        _set_user(data, username, state="ACTIVE", session_id=session["session_id"], lot_id=lot_id)
        _save(data)

        try:
            await _prepare_active(bot, session, lot)
        except Exception:
            logger.exception("Не удалось подготовить лот %s для %s", lot_id, username)
            try:
                await _set_lot_price_checked(bot, lot_id, _restore_price_for_lot(lot_id))
            except Exception:
                logger.exception("Не удалось восстановить цену лота %s", lot_id)
            data = _load()
            lot = data["lots"].get(lot_id)
            if lot:
                lot["active"] = None
                _set_user(data, username, state="MENU", session_id=None, lot_id=None)
                _save(data)
            _send_prepaid_message(bot, chat_id, "❌ Не удалось подготовить лот. Пожалуйста, попробуйте ещё раз позже.")


async def _handle_message_checked(bot, event) -> None:
    """Выполняет предоплатную обработку после проверки профиля."""
    message = getattr(event, "message", None)
    if not message or not getattr(message, "author", None):
        return

    username = message.author
    author_id = getattr(message, "author_id", None)

    # Проверяем профиль ДО любого автоматического сообщения.
    # Если найден SoundCloud-лот, задача завершается молча.
    if await _is_soundcloud_seller(bot, username, author_id):
        return

    text = (message.text or "").strip()
    lower = text.lower()
    logger.info("PREPAYMENT DEBUG: получено сообщение от %s: %r", username, text)

    # После оплаты управление остаётся за существующим Apple TopUp flow.
    if storage.find_active_order(username):
        return

    # Сначала добиваем просроченную заявку этого пользователя, если worker не успел
    # обработать её до нового сообщения. Это не отменяет проверку оплаты на границе таймера.
    data_before = _load()
    _, stale_session = _find_session(data_before, username)
    if stale_session and stale_session.get("status") == "ACTIVE":
        try:
            expires_at = float(stale_session.get("expires_at", 0) or 0)
            if expires_at and expires_at <= time.time():
                await _expire_once(bot)
        except Exception:
            logger.exception("PREPAYMENT DEBUG: не удалось обработать просроченную заявку %s", username)

    data = _load()
    user = _user(data, username)
    state = user.get("state") if user else None

    # ВАЖНО: обычное сообщение покупателя, у которого уже есть/был
    # любой FunPay-заказ, не должно запускать приветственное меню.
    # Иначе покупатель другой категории после оплаты пишет продавцу,
    # а предоплата ошибочно отвечает своим стартовым сообщением.
    # Явные команды ниже по-прежнему обрабатываются.
    has_previous_sale = await _has_existing_sale(bot, username) if user is None else False

    # Активную/очередную заявку не сбрасываем командами навигации:
    # иначе можно оставить слот занятым без покупателя.
    existing_lot_id, existing_session = _find_session(data, username)
    if existing_session and state in ("ACTIVE", "QUEUED") and lower in ("!меню", "!прайс", "!инструкция", "!продавец", "!seller", "!price", "!instruction"):
        _send_prepaid_message(bot, message.chat_id, "⏳ У вас уже есть активная заявка. Дождитесь оплаты или окончания времени ожидания.")
        return True

    if lower == "!меню":
        await _show_menu(bot, username, message.chat_id)
        return True
    if lower in ("!прайс", "!price"):
        logger.info("PREPAYMENT DEBUG: команда !ПРАЙС от %s", username)
        await _show_price(bot, username, message.chat_id)
        return True
    if lower in ("!инструкция", "!instruction"):
        logger.info("PREPAYMENT DEBUG: команда !ИНСТРУКЦИЯ от %s", username)
        await _show_instruction(bot, username, message.chat_id)
        return True
    if lower in ("!продавец", "!seller"):
        await _seller(bot, username, message.chat_id)
        return True

    if state == "WAITING_SELLER":
        return True

    if state in ("BROWSING", "MENU") and lower in PRODUCTS:
        await _select_product(bot, username, message.chat_id, lower)
        return True

    if state in ("BROWSING",):
        _send_prepaid_message(bot, message.chat_id, INVALID_NUMBER)
        return True

    # Если пользователь уже имеет запись предоплаты, стартовое сообщение
    # повторно никогда не отправляем на обычные вопросы/сообщения.
    if user is not None:
        logger.debug(
            "PREPAYMENT: обычное сообщение от %s без команды — стартовое меню не повторяем (state=%s)",
            username, state,
        )
        return True

    # Для покупателя с уже существующим заказом в другой категории также
    # ничего автоматически не отправляем. Это предотвращает вмешательство
    # предоплатного сценария в чужой заказ.
    if has_previous_sale:
        logger.info(
            "PREPAYMENT: %s уже имеет заказ FunPay — стартовое меню не отправляем",
            username,
        )
        return True

    # Только действительно новый покупатель получает стартовое сообщение.
    await _show_menu(bot, username, message.chat_id)


async def handle_message(bot, event) -> bool:
    """Поглощает NEW_MESSAGE и запускает предоплату без блокировки Runner."""
    _ensure_worker(bot)
    message = getattr(event, "message", None)
    if not message or not getattr(message, "author", None):
        return False

    username = str(message.author)

    # Уже оплаченный/активный Apple TopUp должен идти по старому сценарию.
    if storage.find_active_order(username):
        return False

    # Не await-им сетевую проверку профиля здесь. Runner FunPay вызывает
    # NEW_MESSAGE последовательно, поэтому ожидание get_user() здесь могло бы
    # задержать обработку сообщений всех остальных покупателей.
    # Каждому покупателю даём собственную очередь задач: сообщения одного
    # покупателя сохраняют порядок, а разные покупатели обрабатываются параллельно.
    key = username.casefold()
    user_lock = _profile_user_locks.get(key)
    if user_lock is None:
        user_lock = asyncio.Lock()
        _profile_user_locks[key] = user_lock

    async def _run_serialized() -> None:
        async with user_lock:
            await _handle_message_checked(bot, event)

    task = asyncio.create_task(_run_serialized())

    def _cleanup(done_task: asyncio.Task, user_key: str = key) -> None:
        try:
            done_task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(
                "PREPAYMENT: ошибка фоновой обработки сообщения от %s",
                username,
            )
        # Не удаляем lock, если за время выполнения уже появилась новая задача
        # для того же пользователя. Оставляем лёгкий lock-кэш — пользователей
        # обычно немного, а это исключает гонки между их командами.

    task.add_done_callback(_cleanup)
    return True
