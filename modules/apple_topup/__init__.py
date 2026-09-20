from FunPayAPI.updater.events import EventTypes

from .meta import (
    PREFIX,
    VERSION,
    NAME,
    DESCRIPTION,
    AUTHORS,
    LINKS,
)

from .handlers import (
    on_order_status_changed,
    on_new_message,
    on_funpay_bot_init,
)


BOT_EVENT_HANDLERS = {
    "ON_FUNPAY_BOT_INIT": [
        on_funpay_bot_init,
    ],
}

FUNPAY_EVENT_HANDLERS = {
    EventTypes.ORDER_STATUS_CHANGED: [
        on_order_status_changed,
    ],
    EventTypes.NEW_MESSAGE: [
        on_new_message,
    ],
}

TELEGRAM_BOT_ROUTERS = []