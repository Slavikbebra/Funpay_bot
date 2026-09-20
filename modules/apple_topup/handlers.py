from .api import FazerCardsAPI
import time
import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

from FunPayAPI.common.enums import OrderStatuses

from tgbot.telegrambot import (
    get_telegram_bot,
    get_telegram_bot_loop,
)

from . import config
from . import messages
from . import storage
from . import prepayment
from . import products


logger = logging.getLogger("modules.apple_topup")


# ============================================================
# ИЗОБРАЖЕНИЕ С ИНСТРУКЦИЕЙ ПО СМЕНЕ РЕГИОНА
# ============================================================

INSTRUCTION_IMAGE_PATH = Path(__file__).with_name("instruction.jpg")
_instruction_image_id = None


def get_instruction_image_id(bot):
    """
    Загружает instruction.jpg в FunPay и возвращает image_id.
    После первой загрузки в рамках текущего запуска используется
    сохранённый image_id, чтобы не загружать картинку повторно.
    """
    global _instruction_image_id

    if _instruction_image_id:
        return _instruction_image_id

    if not INSTRUCTION_IMAGE_PATH.exists():
        raise FileNotFoundError(
            f"Не найдена картинка инструкции: {INSTRUCTION_IMAGE_PATH}"
        )

    image_id = bot.account.upload_image(str(INSTRUCTION_IMAGE_PATH))

    if not image_id:
        raise Exception("FunPay не вернул image_id после загрузки инструкции")

    _instruction_image_id = image_id

    logger.info(
        "🍎 Apple TopUp: инструкция загружена в FunPay, image_id=%s",
        image_id
    )

    return image_id
def get_order_product(order_id):
    """Возвращает настройки товара, сохранённые для заказа."""
    order = storage.get_order(order_id)
    if not order:
        return None
    items = order.get("product_items") or []
    if not items and order.get("fazercards_card_id"):
        items = [{
            "card_id": order.get("fazercards_card_id"),
            "quantity": int(order.get("quantity", 0) or 0),
        }]
    lot_id = order.get("lot_id")
    after_input_message = order.get("after_input_message")

    # Для старых заказов, созданных до сохранения after_input_message,
    # берём актуальный текст напрямую из products.py.
    if not after_input_message and lot_id is not None:
        saved_product = products.get_product(lot_id)
        if saved_product:
            after_input_message = saved_product.get("after_input_message")

    return {
        "lot_id": lot_id,
        "name": order.get("product_name"),
        "fazercards_category_id": order.get("fazercards_category_id"),
        "fazercards_card_id": order.get("fazercards_card_id"),
        "quantity": sum(int(item.get("quantity", 0)) for item in items),
        "items": items,
        "after_input_message": after_input_message,
    }


def get_fazercards_offer(product, card_id=None):
    """Проверяет наличие нужного предложения FazerCards."""
    api = FazerCardsAPI(config.FAZERCARDS_API_KEY)
    card_id = card_id or product.get("fazercards_card_id")
    data = api.get_giftcard_cards(product["fazercards_category_id"])
    for offer in data.get("offers", []):
        if str(offer.get("card_id")) == str(card_id):
            return offer
    return None


def buy_fazercards_codes(order_id):
    """Покупает все номиналы, настроенные для конкретного заказа."""
    product = get_order_product(order_id)
    if not product or not product.get("items"):
        raise Exception(f"Для заказа {order_id} отсутствуют настройки товара")

    api = FazerCardsAPI(config.FAZERCARDS_API_KEY)
    order = storage.get_order(order_id) or {}
    saved_ids = order.get("fazercards_order_ids") or []
    if not saved_ids and order.get("fazercards_order_id"):
        saved_ids = [order["fazercards_order_id"]]

    all_codes = []
    all_order_ids = list(saved_ids)

    for index, item in enumerate(product["items"]):
        card_id = str(item["card_id"])
        quantity = int(item["quantity"])
        if quantity <= 0:
            continue

        existing_id = all_order_ids[index] if index < len(all_order_ids) else None
        if existing_id:
            codes = get_existing_fazercards_codes(existing_id)
            all_codes.extend([str(code) for code in codes])
            continue

        idempotency_key = f"apple-topup-{order_id}-{index}"
        logger.info(
            "🍎 Apple TopUp: покупаем %s x %s для заказа %s (лот %s)",
            card_id, quantity, order_id, product["lot_id"]
        )
        result = api.create_giftcard_order(
            category_id=product["fazercards_category_id"],
            card_id=card_id,
            quantity=quantity,
            idempotency_key=idempotency_key
        )
        fazer_order = result.get("order", {})
        fazer_order_id = fazer_order.get("id")
        if not fazer_order_id:
            raise Exception(f"FazerCards не вернул ID заказа: {result}")

        all_order_ids.append(fazer_order_id)
        storage.update_order(
            order_id,
            fazercards_order_ids=all_order_ids,
            fazercards_order_id=all_order_ids[0],
            purchase_started=True
        )

        codes = fazer_order.get("cards")
        if not codes:
            for attempt in range(1, 37):
                time.sleep(5)
                status_result = api.get_order(fazer_order_id)
                fazer_order = status_result.get("order", {})
                status = str(fazer_order.get("status", "")).lower()
                logger.info(
                    "🍎 Apple TopUp: статус FazerCards заказа %s (проверка %s/36): %s",
                    fazer_order_id, attempt, status
                )
                if status == "completed":
                    codes = fazer_order.get("cards")
                    if not codes and isinstance(fazer_order.get("payload"), dict):
                        codes = fazer_order["payload"].get("cards")
                    break
                if status in ("failed", "refund", "refunded"):
                    raise Exception(f"FazerCards заказ завершился со статусом: {status}")
        if not codes:
            raise Exception(f"FazerCards: не получены коды для {card_id}")
        if len(codes) != quantity:
            raise Exception(f"FazerCards вернул {len(codes)} кодов для {card_id}, ожидалось {quantity}")
        all_codes.extend([str(code) for code in codes])

    if len(all_codes) != product["quantity"]:
        raise Exception(f"FazerCards вернул {len(all_codes)} кодов, ожидалось {product['quantity']}")
    return all_codes

def get_existing_fazercards_codes(fazercards_order_id):
    """
    Получает коды уже существующего заказа FazerCards.
    Новую покупку НЕ создаёт.
    """

    api = FazerCardsAPI(
        config.FAZERCARDS_API_KEY
    )

    logger.info(
        "🍎 Apple TopUp: проверяем существующий FazerCards заказ %s",
        fazercards_order_id
    )

    max_attempts = 36
    poll_interval = 5

    for attempt in range(1, max_attempts + 1):

        status_result = api.get_order(
            fazercards_order_id
        )

        logger.info(
            "🍎 Apple TopUp: существующий заказ %s "
            "(проверка %s/%s): %s",
            fazercards_order_id,
            attempt,
            max_attempts,
            status_result
        )

        fazer_order = status_result.get(
            "order",
            {}
        )

        status = str(
            fazer_order.get("status", "")
        ).lower()

        codes = fazer_order.get("cards")

        if not codes:
            payload = fazer_order.get(
                "payload",
                {}
            )

            if isinstance(payload, dict):
                codes = payload.get("cards")

        if status == "completed":

            if not codes:
                raise Exception(
                    "FazerCards сообщил completed, "
                    "но коды не найдены"
                )

            return codes

        if status in (
            "failed",
            "refund",
            "refunded"
        ):
            raise Exception(
                f"FazerCards заказ завершился "
                f"со статусом: {status}"
            )

        time.sleep(poll_interval)

    raise Exception(
        "FazerCards: превышено время ожидания "
        "получения существующего заказа"
    )


fazercards = FazerCardsAPI(
    config.FAZERCARDS_API_KEY
)



def _normalize_lot_title(value):
    """Нормализует название лота для безопасного сопоставления."""
    if value is None:
        return ""
    value = str(value).replace("\u200b", "").replace("\ufeff", "")
    value = re.sub(r"\s+", " ", value).strip().casefold()
    return value


def _resolve_lot_id_from_order(bot, order):
    """Определяет ID Apple TopUp лота.

    Сначала используется настоящий offer_id. Если FunPay не отдаёт его в
    объекте заказа, берём summary/title и сопоставляем с собственными лотами
    той же подкатегории. Ограничиваем результат только лотами из products.py.
    """
    # 1. Настоящий offer_id — приоритетный и самый безопасный путь.
    try:
        lot_id = order.get_field_value("offer_id")
    except Exception:
        lot_id = None

    if isinstance(lot_id, dict):
        lot_id = lot_id.get("ru") or lot_id.get("en")

    if lot_id is not None and str(lot_id).strip():
        lot_id = str(lot_id).strip()
        if products.get_product(lot_id):
            return lot_id

    # 2. В текущем API название может находиться в summary.
    order_title = ""
    for getter in ("summary", "desc", "payment_msg"):
        try:
            value = order.get_field_value(getter)
        except Exception:
            value = None
        if isinstance(value, dict):
            value = value.get("ru") or value.get("en")
        if value:
            order_title = str(value)
            break

    if not order_title:
        try:
            order_title = order.get_field_value_any("summary") or ""
        except Exception:
            pass
    if not order_title:
        order_title = getattr(order, "title", "") or ""

    normalized_order_title = _normalize_lot_title(order_title)
    if not normalized_order_title:
        logger.error(
            "❌ Apple TopUp: не удалось получить название лота для заказа %s",
            order.id,
        )
        return None

    # 3. Сопоставляем только с нашими лотами из products.py.
    subcategory = getattr(order, "subcategory", None)
    if not subcategory:
        logger.error(
            "❌ Apple TopUp: у заказа %s отсутствует подкатегория; "
            "offer_id и title недоступны для определения лота",
            order.id,
        )
        return None

    try:
        own_lots = bot.account.get_my_subcategory_lots(subcategory.id)
    except Exception:
        logger.exception(
            "❌ Apple TopUp: не удалось получить собственные лоты "
            "подкатегории %s для заказа %s",
            getattr(subcategory, "id", "?"),
            order.id,
        )
        return None

    exact = []
    partial = []
    for lot in own_lots:
        lot_id = str(getattr(lot, "id", ""))
        if not products.get_product(lot_id):
            continue
        lot_title = getattr(lot, "title", None) or getattr(lot, "description", None)
        normalized_lot_title = _normalize_lot_title(lot_title)
        if not normalized_lot_title:
            continue
        if normalized_lot_title == normalized_order_title:
            exact.append(lot_id)
        elif normalized_order_title in normalized_lot_title or normalized_lot_title in normalized_order_title:
            partial.append(lot_id)

    if len(exact) == 1:
        logger.info(
            "🍎 Apple TopUp: offer_id не был передан FunPay; "
            "лот определён по точному совпадению названия: %s",
            exact[0],
        )
        return exact[0]
    if len(exact) > 1:
        logger.error(
            "❌ Apple TopUp: несколько Apple TopUp лотов с одинаковым названием "
            "для заказа %s: %s",
            order.id, exact,
        )
        return None

    if len(partial) == 1:
        logger.info(
            "🍎 Apple TopUp: offer_id не был передан FunPay; "
            "лот определён по частичному совпадению названия: %s",
            partial[0],
        )
        return partial[0]
    if len(partial) > 1:
        logger.error(
            "❌ Apple TopUp: неоднозначное частичное совпадение лота "
            "для заказа %s: %s; заказ не обрабатываем",
            order.id, partial,
        )
        return None

    logger.error(
        "❌ Apple TopUp: не найден Apple TopUp лот по названию заказа %s: %r",
        order.id, order_title,
    )
    return None

def extract_order_id(text: str):
    """
    Извлекает ID заказа из системного сообщения FunPay.

    Например:
    Покупатель barabanjj22 оплатил заказ #SL8MUHC5.
    """
    if not text:
        return None

    match = re.search(
        r"заказ\s*#([A-Za-z0-9]+)",
        text,
        re.IGNORECASE
    )

    if not match:
        return None

    return match.group(1)


async def start_paid_order(bot, order):
    """Запускает Apple TopUp-сценарий после обнаружения оплаты."""

    if not config.ENABLED:
        return

    if not order:
        return

    order_id = str(order.id)

    try:
        # ---------------------------------------------------------
        # 1. Определяем товар строго по реальному offer_id заказа.
        # ---------------------------------------------------------
        lot_id = _resolve_lot_id_from_order(bot, order)
        if not lot_id:
            logger.error(
                "❌ Apple TopUp: лот для заказа %s не определён. "
                "Заказ не обрабатываем.",
                order_id,
            )
            return

        # FunPay может не прислать отдельный ORDER_STATUS_CHANGED(PAID).
        # Поэтому освобождаем предоплатную заявку прямо здесь, после
        # надёжного определения lot_id из оплаченного заказа.
        try:
            await prepayment.handle_payment(bot, order, lot_id_override=lot_id)
            logger.info(
                "💰 Apple TopUp: предоплатная заявка освобождена после оплаты заказа %s; "
                "лот %s возвращён к restore_price.",
                order_id, lot_id,
            )
        except Exception:
            logger.exception(
                "❌ Apple TopUp: не удалось освободить предоплатную заявку после оплаты "
                "заказа %s (лот %s).",
                order_id, lot_id,
            )

        product = products.get_product(lot_id)

        if not product:
            logger.error(
                "❌ Apple TopUp: лот %s не настроен, заказ %s не обрабатываем.",
                lot_id,
                order_id
            )
            return

        primary_card_id = product.get("fazercards_card_id")
        card_summary = (
            primary_card_id
            if primary_card_id
            else ", ".join(
                f"{item.get('card_id')} x {int(item.get('quantity', 0) or 0)}"
                for item in product.get("items", [])
            )
        )

        logger.info(
            "🍎 Apple TopUp: выбран лот %s -> %s, %s (всего %s кодов)",
            lot_id,
            product["name"],
            card_summary,
            product["quantity"]
        )

        # ---------------------------------------------------------
        # 2. Фиксируем оплаченный заказ ДО любых внешних API-вызовов.
        # Это не даёт временной ошибке FazerCards потерять заказ.
        # ---------------------------------------------------------
        buyer_username = getattr(order, "buyer_username", None)
        if not buyer_username:
            buyer_username = getattr(order, "buyer", "")

        chat_id = getattr(order, "chat_id", None)
        if chat_id is None:
            logger.error(
                "❌ Apple TopUp: у заказа %s отсутствует chat_id. "
                "Заказ не обрабатываем.",
                order_id
            )
            return

        logger.info(
            "🍎 Apple TopUp: сценарий запущен для заказа %s (%s)",
            order_id,
            buyer_username
        )

        existing = storage.get_order(order_id)
        if existing:
            logger.info(
                "🍎 Apple TopUp: заказ %s уже существует, "
                "повторный запуск сценария пропускаем (state=%s)",
                order_id,
                existing.get("state")
            )
            return

        storage.create_order(
            order_id=order_id,
            buyer_username=buyer_username,
            chat_id=chat_id,
            lot_id=lot_id,
            product_name=product["name"],
            fazercards_category_id=product["fazercards_category_id"],
            fazercards_card_id=product.get("fazercards_card_id"),
            quantity=product["quantity"],
            product_items=product["items"],
            after_input_message=product.get("after_input_message")
        )

        # ---------------------------------------------------------
        # 3. Отправляем приветствие. Покупка FazerCards начинается
        # только после !сменил, когда пользователь подтвердил регион.
        # ---------------------------------------------------------
        bot.send_message(chat_id, messages.WELCOME)

        logger.info(
            "🍎 Apple TopUp: приветственное сообщение отправлено "
            "для заказа %s",
            order_id
        )

    except Exception:
        logger.exception(
            "❌ Apple TopUp: ошибка запуска заказа %s",
            order_id
        )

        try:
            existing = storage.get_order(order_id)
            if existing and existing.get("state") not in ("COMPLETED", "REFUNDED"):
                storage.update_order(order_id, state="ERROR")
                bot.send_message(existing.get("chat_id"), messages.ERROR)
        except Exception:
            logger.exception(
                "❌ Apple TopUp: не удалось перевести заказ %s в ERROR после сбоя запуска",
                order_id
            )

async def on_order_status_changed(bot, event):
    """
    Оставляем обработчик статусов для будущего использования.

    В текущем FunPay API PAID может не приходить как
    ORDER_STATUS_CHANGED, поэтому основной запуск происходит
    через системное сообщение в on_new_message().
    """

    order = event.order

    if not order:
        return

    logger.info(
        "🔄 Apple TopUp STATUS DEBUG: order=%s status=%r",
        order.id,
        order.status
    )

    # Предоплатная очередь должна освобождать лот не только через системное
    # сообщение об оплате, но и через официальное изменение статуса заказа.
    # Это особенно важно при ручном возврате средств.
    if order.status == OrderStatuses.PAID:
        try:
            await prepayment.handle_payment(bot, order)
        except Exception:
            logger.exception("Apple TopUp: ошибка освобождения предоплаты после PAID для %s", order.id)

    elif order.status == OrderStatuses.REFUNDED:
        try:
            await prepayment.handle_refund(bot, order)
        except Exception:
            logger.exception("Apple TopUp: ошибка освобождения предоплаты после REFUNDED для %s", order.id)

    if order.status == OrderStatuses.CLOSED:
        existing = storage.get_order(order.id)

        if existing:
            logger.info(
                "🍎 Apple TopUp: заказ %s получил статус CLOSED в FunPay; "
                "локальное состояние Apple TopUp=%s, last_command=%s.",
                order.id, existing.get("state"), existing.get("last_command")
            )

    elif order.status == OrderStatuses.REFUNDED:
        existing = storage.get_order(order.id)

        if existing:
            storage.update_order(
                order.id,
                state="REFUNDED"
            )

            logger.info(
                "🔄 Apple TopUp REFUND DEBUG: order=%s -> state=REFUNDED",
                order.id
            )


async def recover_existing_order(bot, order_id):
    """
    Восстанавливает уже созданный заказ FazerCards.
    Новую покупку НЕ создаёт.
    """

    order = storage.get_order(order_id)

    if not order:
        logger.error(
            "Apple TopUp: заказ %s не найден в storage",
            order_id
        )
        return

    fazercards_order_id = order.get(
        "fazercards_order_id"
    )

    if not fazercards_order_id:
        logger.error(
            "Apple TopUp: у заказа %s нет FazerCards order_id",
            order_id
        )
        return

    chat_id = order["chat_id"]

    try:
        codes = await asyncio.to_thread(
            get_existing_fazercards_codes,
            fazercards_order_id
        )

        if not codes:
            raise Exception(
                "Не удалось получить коды"
            )

        current_order = storage.get_order(order_id)

        if current_order and current_order.get("codes"):
            logger.warning(
                "Apple TopUp: коды для заказа %s уже сохранены; "
                "ждём команду !ввел",
                order_id
            )
            return

        codes = [
            str(code)
            for code in codes
        ]

        expected_quantity = int(
            get_order_product(order_id)["quantity"]
        )

        if len(codes) != expected_quantity:
            raise Exception(
                f"Получено {len(codes)} кодов, "
                f"ожидалось {expected_quantity}"
            )

        storage.update_order(
            order_id,
            state="CODES_SENT",
            codes=codes,
            codes_sent_at=datetime.now().isoformat()
        )

        codes_text = "\n".join(
            f"{i + 1}. {code}"
            for i, code in enumerate(codes)
        )

        bot.send_message(
            chat_id,
            messages.CODES.format(
                codes=codes_text
            )
        )

        logger.info(
            "🍎 Apple TopUp: восстановлено и отправлено "
            "%s кодов для заказа %s",
            len(codes),
            order_id
        )

        logger.info(
            "🍎 Apple TopUp: коды восстановлены для заказа %s; "
            "ждём команду !ввел",
            order_id
        )

    except Exception:
        logger.exception(
            "❌ Apple TopUp: ошибка восстановления заказа %s",
            order_id
        )


async def on_new_message(bot, event):
    """
    Обрабатывает:
    1. системное сообщение FunPay об оплате;
    2. команды покупателя Apple TopUp.
    """

    message = event.message

    if not config.ENABLED:
        return

    if not message:
        return

    # Apple TopUp is the owner of NEW_MESSAGE while this module is enabled.
    # The core FunPay handler must not process the same message afterwards,
    # otherwise it can send duplicate/legacy prepayment messages.
    setattr(event, "_apple_topup_handled", True)

    text = (message.text or "").strip()

    # ============================================================
    # 1. СИСТЕМНОЕ СООБЩЕНИЕ FUNPAY ОБ ОПЛАТЕ
    # ============================================================

    if (
        message.author
        and message.author.lower() == "funpay"
    ):
        order_id = extract_order_id(text)

        if order_id:
            logger.info(
                "🍎 Apple TopUp DEBUG: найден ID заказа %s в системном сообщении",
                order_id
            )

            try:
                order = bot.account.get_order(order_id)
            except Exception:
                logger.exception(
                    "Apple TopUp: не удалось получить заказ %s",
                    order_id
                )
                return

            await start_paid_order(bot, order)

        return

    # ============================================================
    # 2. ОБЫЧНОЕ СООБЩЕНИЕ ПОКУПАТЕЛЯ
    # ============================================================

    username = message.author

    if not username:
        return

    # Сообщения самого продавца бот видит как NEW_MESSAGE тоже.
    # Они никогда не должны обрабатываться как команды покупателя.
    try:
        bot_username = getattr(getattr(bot, "account", None), "username", None)
    except Exception:
        bot_username = None
    if bot_username and str(username).lstrip("@").casefold() == str(bot_username).lstrip("@").casefold():
        logger.debug(
            "🍎 Apple TopUp DEBUG: сообщение собственного аккаунта пропущено: author=%r",
            username,
        )
        return

    incoming_chat_id = getattr(message, "chat_id", None)
    # Предоплатный сценарий должен перехватывать свои команды
    # до старой логики Apple TopUp.
    if await prepayment.handle_message(bot, event):
        return

    order = storage.find_active_order(username, incoming_chat_id)

    # В разных местах FunPay один и тот же чат может иметь разные chat_id.
    # Не меняем storage и не ломаем старую схему: если точное совпадение не
    # найдено, второй поиск выполняем только по стабильному username покупателя.
    if not order:
        order = storage.find_active_order(username)
        if order:
            logger.warning(
                "🍎 Apple TopUp DEBUG: заказ найден по username fallback: "
                "order=%s buyer=%r saved_chat_id=%r incoming_chat_id=%r state=%s",
                order.get("order_id"), order.get("buyer_username"),
                order.get("chat_id"), incoming_chat_id, order.get("state"),
            )

    if not order:
        text_lower = (
            (text or "")
            .replace("\u200b", "")
            .replace("\ufeff", "")
            .strip()
            .casefold()
        )

        # ========================================================
        # ПРЕДПРОДАЖНОЕ МЕНЮ
        # ========================================================

        # !МЕНЮ — главное меню для покупателя без оплаченного заказа.
        if text_lower == "!меню":
            if incoming_chat_id is not None:
                bot.send_message(
                    incoming_chat_id,
                    messages.MAIN_MENU.format(username=username),
                )
            return

        # !ПРАЙС — актуальные цены.
        if text_lower == "!прайс":
            if incoming_chat_id is not None:
                bot.send_message(
                    incoming_chat_id,
                    messages.PRICE,
                )
            return

        # Любое первое обычное сообщение нового покупателя
        # открывает главное меню.
        if incoming_chat_id is not None:
            bot.send_message(
                incoming_chat_id,
                messages.MAIN_MENU.format(username=username),
            )
        return

    text_lower = (
        (text or "")
        .replace("\u200b", "")
        .replace("\ufeff", "")
        .strip()
        .casefold()
    )

    order_id = order["order_id"]
    # Для ответа используем chat_id входящего сообщения, если он есть.
    # Это гарантирует ответ именно в текущий чат даже при разных форматах ID.
    chat_id = incoming_chat_id if incoming_chat_id is not None else order.get("chat_id")
    state = order["state"]

    logger.info(
        "🍎 Apple TopUp DEBUG: команда покупателя: author=%r command=%r "
        "order=%s state=%s incoming_chat_id=%r saved_chat_id=%r",
        username, text_lower, order_id, state, incoming_chat_id, order.get("chat_id"),
    )

    # ============================================================
    # !ВВЕЛ
    # ============================================================

    if text_lower == "!ввел":

        if state != "CODES_SENT":
            return

        current_order = storage.get_order(order_id)
        product = get_order_product(order_id)

        if not current_order or not product:
            return

        # Сообщение после !ввел берём из настроек конкретного лота.
        after_input_message = current_order.get("after_input_message")

        if not after_input_message:
            after_input_message = messages.AFTER_INPUT_DEFAULT

        storage.update_order(
            order_id,
            state="WAITING_CONFIRMATION",
            last_command="!ввел"
        )

        bot.send_message(
            chat_id,
            after_input_message
        )

        logger.info(
            "🍎 Apple TopUp: покупатель ввёл коды по заказу %s; "
            "отправлена инструкция для лота %s",
            order_id,
            product.get("lot_id")
        )

        return


    # ============================================================
    # !ОФОРМИЛ
    # ============================================================

    if text_lower == "!оформил":

        if state != "WAITING_CONFIRMATION":
            return

        storage.update_order(
            order_id,
            state="COMPLETED",
            last_command="!оформил"
        )

        bot.send_message(
            chat_id,
            messages.FINAL
        )

        logger.info(
            "🍎 Apple TopUp: покупатель подтвердил оформление заказа %s; "
            "локальное состояние COMPLETED, ожидаем статус CLOSED от FunPay.",
            order_id
        )

        return


    # ============================================================
    # ВЫЗОВ ПРОДАВЦА
    # ============================================================

    if text_lower in ("!продавец", "!seller"):

        bot.send_message(
            chat_id,
            messages.SELLER
        )

        try:
            telegram_bot = get_telegram_bot()
            telegram_loop = get_telegram_bot_loop()

            asyncio.run_coroutine_threadsafe(
                telegram_bot.call_seller(
                    username,
                    chat_id
                ),
                telegram_loop
            )

        except Exception:
            logger.exception(
                "Apple TopUp: ошибка вызова продавца"
            )

        return

    # ============================================================
    # !МОГУ
    # ============================================================

    if text_lower == "!могу":

        if state != "WAITING_REGION":
            return

        # Сначала отправляем текст.
        bot.send_message(
            chat_id,
            messages.CAN_CHANGE_REGION
        )

        # Затем отдельным сообщением отправляем инструкцию.
        try:
            image_id = await asyncio.to_thread(
                get_instruction_image_id,
                bot
            )

            bot.send_message(
                chat_id,
                image_id=image_id
            )

        except Exception:
            logger.exception(
                "❌ Apple TopUp: не удалось отправить "
                "картинку-инструкцию для заказа %s",
                order_id
            )

            bot.send_message(
                chat_id,
                "Не удалось автоматически отправить изображение с инструкцией.\n"
                "Пожалуйста, обратитесь к продавцу:\n"
                "!продавец"
            )
            return

        storage.update_order(
            order_id,
            state="WAITING_REGION_CHANGED",
            last_command="!могу"
        )

        logger.info(
            "🍎 Apple TopUp: текст и инструкция отправлены для заказа %s",
            order_id
        )

        return

    # ============================================================
    # !НЕ МОГУ
    # ============================================================

    if text_lower == "!не могу":

        if state != "WAITING_REGION":
            return

        storage.update_order(
            order_id,
            state="SELLER_REQUESTED",
            last_command="!не могу"
        )

        bot.send_message(
            chat_id,
            messages.CANNOT_CHANGE_REGION
        )

        try:
            telegram_bot = get_telegram_bot()
            telegram_loop = get_telegram_bot_loop()

            asyncio.run_coroutine_threadsafe(
                telegram_bot.call_seller(
                    username,
                    chat_id
                ),
                telegram_loop
            )

        except Exception:
            logger.exception(
                "Apple TopUp: ошибка вызова продавца"
            )

        return

        # ============================================================
    # !СМЕНИЛ
    # ============================================================

    if text_lower == "!сменил":

        current_order = storage.get_order(order_id)

        if not current_order:
            return

        # Если коды уже были отправлены, повторный !сменил
        # не запускает покупку повторно, а даёт понятный ответ покупателю.
        if current_order.get("codes"):
            logger.warning(
                "Apple TopUp: покупатель повторно отправил !сменил для заказа %s; "
                "повторную покупку не запускаем",
                order_id
            )
            bot.send_message(
                chat_id,
                "Коды уже были отправлены выше. Повторно получать их не нужно.\n\n"
                "Если возникли проблемы — напишите:\n!продавец"
            )
            return

        # Если покупка уже запущена, но коды ещё не готовы,
        # также ничего не покупаем повторно.
        if current_order.get("purchase_started") or current_order.get(
            "fazercards_order_id"
        ):
            logger.warning(
                "Apple TopUp: покупатель повторно отправил !сменил для заказа %s; "
                "покупка уже запущена (%s)",
                order_id,
                current_order.get("fazercards_order_id")
            )
            bot.send_message(
                chat_id,
                "Покупка кодов уже выполняется. Пожалуйста, немного подождите.\n\n"
                "Если возникли проблемы — напишите:\n!продавец"
            )
            return

        if state != "WAITING_REGION_CHANGED":
            return

        # Сначала фиксируем переход в storage.
        # Если процесс упадёт сразу после получения команды,
        # при следующем запуске заказ останется в PREPARING_CODES
        # и recovery сможет продолжить сценарий.
        storage.update_order(
            order_id,
            state="PREPARING_CODES",
            purchase_started=True,
            last_command="!сменил"
        )

        bot.send_message(
            chat_id,
            messages.REGION_CHANGED
        )

        # ----------------------------------------------------
        # ПРОВЕРЯЕМ ТОВАР ПЕРЕД ПОКУПКОЙ
        # ----------------------------------------------------

        try:
            product = get_order_product(order_id)
            if not product:
                raise Exception(f"Для заказа {order_id} не настроен товар")

            for item in product["items"]:
                card_id = str(item["card_id"])
                quantity = int(item["quantity"])
                offer = get_fazercards_offer(product, card_id)
                if not offer:
                    raise Exception(f"Предложение {card_id} не найдено")

                stock = int(offer.get("stock", 0))
                min_quantity = int(offer.get("min_order_quantity", 1))
                max_quantity = int(offer.get("max_order_quantity", stock))

                if quantity < min_quantity:
                    raise Exception(f"Количество {quantity} для {card_id} меньше минимального {min_quantity}")
                if quantity > max_quantity:
                    raise Exception(f"Количество {quantity} для {card_id} больше максимального {max_quantity}")
                if quantity > stock:
                    raise Exception(f"Недостаточно товара {card_id}: нужно {quantity}, в наличии {stock}")

                logger.info(
                    "🍎 Apple TopUp: товар подтверждён: card_id=%s, quantity=%s, stock=%s",
                    card_id, quantity, stock
                )
        except Exception:
            logger.exception(
                "❌ Apple TopUp: ошибка проверки товара "
                "для заказа %s",
                order_id
            )

            storage.update_order(
                order_id,
                state="ERROR"
            )

            bot.send_message(
                chat_id,
                messages.ERROR
            )

            return

        # ----------------------------------------------------
        # ПОКУПКА КАРТ
        # ----------------------------------------------------

        try:
            # purchase_started уже фиксируется до любых внешних действий.
            # Здесь оставляем только логирование перед запросом к FazerCards.
            logger.info(
                "🍎 Apple TopUp: покупаем товары %s для заказа %s",
                get_order_product(order_id)["items"],
                order_id
            )

            codes = await asyncio.to_thread(
                buy_fazercards_codes,
                order_id
            )

            if not codes:
                raise Exception(
                    "FazerCards вернул пустой список кодов"
                )

            codes = [
                str(code)
                for code in codes
            ]

            expected_quantity = int(
                get_order_product(order_id)["quantity"]
            )

            if len(codes) != expected_quantity:
                raise Exception(
                    f"FazerCards вернул {len(codes)} кодов, "
                    f"ожидалось {expected_quantity}"
                )

            # ------------------------------------------------
            # СОХРАНЯЕМ КОДЫ
            # ------------------------------------------------

            current_order = storage.get_order(order_id)

            if current_order and current_order.get("codes"):
                logger.warning(
                    "Apple TopUp: коды для заказа %s уже сохранены, "
                    "повторную отправку пропускаем",
                    order_id
                )
                return

            storage.update_order(
                order_id,
                state="CODES_SENT",
                codes=codes,
                codes_sent_at=datetime.now().isoformat()
            )

            codes_text = "\n".join(
                f"{i + 1}. {code}"
                for i, code in enumerate(codes)
            )

            # ------------------------------------------------
            # ОТПРАВЛЯЕМ КОДЫ ПОКУПАТЕЛЮ
            # ------------------------------------------------

            bot.send_message(
                chat_id,
                messages.CODES.format(
                    codes=codes_text
                )
            )

            logger.info(
                "🍎 Apple TopUp: %s кодов отправлено "
                "покупателю для заказа %s",
                len(codes),
                order_id
            )

        except Exception:
            logger.exception(
                "❌ Apple TopUp: ошибка покупки/получения "
                "кодов для заказа %s",
                order_id
            )

            storage.update_order(
                order_id,
                state="ERROR"
            )

            bot.send_message(
                chat_id,
                messages.ERROR
            )

        return


async def recover_preparing_order(bot, order_id):
    """
    Восстанавливает заказ, который находился в PREPARING_CODES
    в момент перезапуска.

    Если FazerCards order_id уже сохранён — только проверяем
    существующий заказ. Если его ещё нет, повторяем POST с тем
    же Idempotency-Key. FazerCards вернёт уже созданный заказ,
    поэтому новая покупка не создаётся.
    """
    order = storage.get_order(order_id)
    if not order:
        return

    if order.get("codes"):
        logger.info(
            "🍎 Apple TopUp: заказ %s уже содержит коды; ждём !ввел",
            order_id
        )
        return

    # Если есть хотя бы один сохранённый FazerCards order_id,
    # buy_fazercards_codes продолжит все позиции по тому же
    # idempotency-key и не создаст повторные покупки.
    if order.get("fazercards_order_ids") or order.get("fazercards_order_id"):
        try:
            codes = await asyncio.to_thread(buy_fazercards_codes, order_id)
            if not codes:
                raise Exception("FazerCards вернул пустой список кодов")
            codes = [str(code) for code in codes]
            expected_quantity = int(get_order_product(order_id)["quantity"])
            if len(codes) != expected_quantity:
                raise Exception(f"FazerCards вернул {len(codes)} кодов, ожидалось {expected_quantity}")
            current_order = storage.get_order(order_id)
            if current_order and current_order.get("codes"):
                        return
            storage.update_order(order_id, state="CODES_SENT", codes=codes, codes_sent_at=datetime.now().isoformat())
            codes_text = "\n".join(f"{i + 1}. {code}" for i, code in enumerate(codes))
            bot.send_message(order["chat_id"], messages.CODES.format(codes=codes_text))
            return
        except Exception:
            logger.exception("❌ Apple TopUp: ошибка продолжения FazerCards-заказа %s", order_id)
            return

    try:
        logger.warning(
            "🍎 Apple TopUp: заказ %s был прерван в PREPARING_CODES; "
            "возобновляем через idempotency-key",
            order_id
        )

        codes = await asyncio.to_thread(
            buy_fazercards_codes,
            order_id
        )

        if not codes:
            raise Exception("FazerCards вернул пустой список кодов")

        codes = [str(code) for code in codes]
        expected_quantity = int(get_order_product(order_id)["quantity"])

        if len(codes) != expected_quantity:
            raise Exception(
                f"FazerCards вернул {len(codes)} кодов, "
                f"ожидалось {expected_quantity}"
            )

        current_order = storage.get_order(order_id)
        if current_order and current_order.get("codes"):
            logger.warning(
                "Apple TopUp: коды для заказа %s уже сохранены; "
                "повторную отправку не выполняем",
                order_id
            )
            return

        storage.update_order(
            order_id,
            state="CODES_SENT",
            codes=codes,
            codes_sent_at=datetime.now().isoformat()
        )

        chat_id = order["chat_id"]
        codes_text = "\n".join(
            f"{i + 1}. {code}"
            for i, code in enumerate(codes)
        )

        bot.send_message(
            chat_id,
            messages.CODES.format(codes=codes_text)
        )

        logger.info(
            "🍎 Apple TopUp: после перезапуска отправлено %s кодов "
            "для заказа %s",
            len(codes),
            order_id
        )


    except Exception:
        logger.exception(
            "❌ Apple TopUp: ошибка авт восстановление заказа %s",
            order_id
        )


async def recover_active_orders(bot):
    """
    Автоматически восстанавливает незавершённые Apple TopUp-заказы
    после запуска FunPayBot.
    """
    if not config.ENABLED:
        return

    data = storage.load()
    if not data:
        logger.info("🍎 Apple TopUp: незавершённых заказов для восстановления нет")
        return

    active_states = {
        "PREPARING_CODES",
        "CODES_SENT",
    }

    orders = [
        order for order in data.values()
        if order.get("state") in active_states
    ]

    if not orders:
        logger.info(
            "🍎 Apple TopUp: незавершённых заказов для восстановления нет"
        )
        return

    logger.info(
        "🍎 Apple TopUp: найдено %s заказов для авт восстановление",
        len(orders)
    )

    for order in orders:
        order_id = str(order.get("order_id"))
        state = order.get("state")

        try:
            if state == "CODES_SENT":
                if order.get("codes"):
                    logger.info(
                        "🍎 Apple TopUp: заказ %s восстановлен в CODES_SENT; "
                        "ждём команду !ввел",
                        order_id
                    )
                else:
                    logger.warning(
                        "Apple TopUp: CODES_SENT без кодов для заказа %s; "
                        "пропускаем автоматическую отправку",
                        order_id
                    )
                continue

            if state == "PREPARING_CODES":
                await recover_preparing_order(bot, order_id)

        except Exception:
            logger.exception(
                "❌ Apple TopUp: ошибка авт восстановления заказа %s",
                order_id
            )


async def on_funpay_bot_init(bot):
    """Запускает восстановление заказов после полной инициализации FunPayBot."""
    # Даём FunPayBot завершить инициализацию и запустить основной runner.
    await asyncio.sleep(1)
    await recover_active_orders(bot)
