"""Small synchronous client for the official FazerCards REST API v2."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import requests

from . import config


class FazerCardsAPIError(RuntimeError):
    """Raised when FazerCards returns an HTTP or API-level error."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class FazerCardsAPI:
    """Minimal FazerCards client used by the Apple TopUp module."""

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = (base_url or config.FAZERCARDS_BASE_URL).rstrip("/")
        self.timeout = float(
            config.FAZERCARDS_TIMEOUT if timeout is None else timeout
        )
        self.retries = max(
            0,
            int(config.FAZERCARDS_RETRIES if retries is None else retries),
        )
        self.session = requests.Session()

    def _headers(self, idempotency_key: Optional[str] = None) -> Dict[str, str]:
        if not self.api_key or self.api_key == "PASTE_YOUR_FAZERCARDS_API_KEY_HERE":
            raise FazerCardsAPIError(
                "Не указан FAZERCARDS_API_KEY. Открой modules/apple_topup/config.py "
                "и вставь API-ключ FazerCards."
            )

        headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "FunPay-Universal-AppleTopUp/1.0",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)
        return headers

    @staticmethod
    def _retry_after(response: requests.Response) -> float:
        value = response.headers.get("Retry-After")
        if not value:
            return 1.0
        try:
            return max(0.0, min(float(value), 30.0))
        except (TypeError, ValueError):
            return 1.0

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        attempts = self.retries + 1

        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=self._headers(idempotency_key),
                    params=params,
                    json=json,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt >= attempts:
                    raise FazerCardsAPIError(
                        f"Ошибка соединения с FazerCards: {exc}"
                    ) from exc
                time.sleep(min(2 ** (attempt - 1), 5))
                continue

            # Временные ошибки можно повторить. Для POST это безопасно,
            # потому что Apple TopUp всегда передаёт Idempotency-Key.
            if response.status_code == 429 and attempt < attempts:
                time.sleep(self._retry_after(response))
                continue

            if 500 <= response.status_code < 600 and attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 5))
                continue

            try:
                payload = response.json()
            except ValueError:
                payload = None

            if not response.ok:
                details = payload if payload is not None else response.text[:500]
                raise FazerCardsAPIError(
                    f"FazerCards HTTP {response.status_code}: {details}",
                    status_code=response.status_code,
                    payload=payload,
                )

            if not isinstance(payload, dict):
                raise FazerCardsAPIError(
                    f"FazerCards вернул неожиданный ответ: {payload!r}",
                    status_code=response.status_code,
                    payload=payload,
                )

            if payload.get("ok") is False:
                error = payload.get("error") or "неизвестная ошибка API"
                code = payload.get("code")
                suffix = f" [{code}]" if code else ""
                raise FazerCardsAPIError(
                    f"FazerCards API: {error}{suffix}",
                    status_code=response.status_code,
                    payload=payload,
                )

            return payload

        raise FazerCardsAPIError("Не удалось получить ответ от FazerCards.")

    def get_me(self) -> Dict[str, Any]:
        """Проверить API-ключ и получить профиль реселлера."""
        return self._request("GET", "/me")

    def get_balance(self) -> Dict[str, Any]:
        """Получить текущий баланс FazerCards."""
        return self._request("GET", "/balance")

    def get_giftcard_cards(self, category_id: str) -> Dict[str, Any]:
        """Получить предложения подарочных карт выбранной категории."""
        if not category_id:
            raise FazerCardsAPIError("Не указан category_id для gift cards.")

        return self._request(
            "GET",
            "/giftcards/cards",
            params={"category_id": str(category_id)},
        )

    def create_giftcard_order(
        self,
        *,
        category_id: str,
        card_id: str,
        quantity: int,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Создать заказ на подарочные карты."""
        if not category_id:
            raise FazerCardsAPIError("Не указан category_id для заказа gift card.")
        if not card_id:
            raise FazerCardsAPIError("Не указан card_id для заказа gift card.")

        try:
            quantity = int(quantity)
        except (TypeError, ValueError) as exc:
            raise FazerCardsAPIError("Количество gift cards должно быть целым числом.") from exc

        if not 1 <= quantity <= 100:
            raise FazerCardsAPIError(
                f"Количество gift cards должно быть от 1 до 100, получено: {quantity}."
            )

        return self._request(
            "POST",
            "/giftcards/order",
            json={
                "category_id": str(category_id),
                "card_id": str(card_id),
                "quantity": quantity,
            },
            idempotency_key=idempotency_key,
        )

    def get_order(self, order_id: str) -> Dict[str, Any]:
        """Получить актуальное состояние ранее созданного FazerCards-заказа."""
        if not order_id:
            raise FazerCardsAPIError("Не указан ID заказа FazerCards.")

        return self._request(
            "GET",
            f"/orders/{str(order_id).strip()}",
        )
