import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp

from config import load_config
from database import log_source_error, upsert_payment_method

config = load_config()


class BinanceAPIError(Exception):
    pass


@dataclass
class P2PAd:
    price: float
    min_single_trans_amount: float
    max_single_trans_amount: float
    available_amount: float
    merchant_name: str
    payment_types: list[str]


@dataclass
class P2PResult:
    ads: list[P2PAd]
    total: int
    checked: int


class BinanceP2PClient:
    def __init__(self):
        self.base_url = config.binance_base_url.rstrip("/")
        self.timeout = config.binance_timeout
        self.max_retries = config.binance_max_retries
        self.max_pages = config.binance_max_pages

    async def fetch_page(
        self,
        session: aiohttp.ClientSession,
        page: int,
        pay_types: list[str],
    ) -> tuple[list[P2PAd], int]:
        url = f"{self.base_url}/bapi/c2c/v2/friendly/c2c/adv/search"

        payload: Dict[str, Any] = {
            "asset": "USDT",
            "fiat": "UAH",
            "merchantCheck": False,
            "page": page,
            "rows": 20,
            "tradeType": "SELL",  # Ми продаємо USDT за UAH (знаходимо покупців)
            "payTypes": pay_types,
        }

        for attempt in range(1, self.max_retries + 1):
            try:
                async with session.post(
                    url, json=payload, timeout=self.timeout
                ) as resp:
                    if resp.status != 200:
                        txt = await resp.text()
                        raise BinanceAPIError(f"HTTP {resp.status}: {txt[:200]}")
                    data = await resp.json()

                    if "data" not in data:
                        raise BinanceAPIError("Несподівана відповідь Binance P2P")

                    ads_raw = data.get("data", [])
                    all_ads: list[P2PAd] = []
                    for item in ads_raw:
                        adv = item.get("adv") or {}
                        advertiser = item.get("advertiser") or {}
                        price_str = adv.get("price")
                        min_amount = adv.get("minSingleTransAmount")
                        max_amount = adv.get("maxSingleTransAmount")
                        available = adv.get("tradableQuantity") or adv.get(
                            "availableAmount"
                        )

                        try:
                            price = float(price_str)
                            min_uah = float(min_amount)
                            max_uah = float(max_amount)
                            avail = float(available)
                        except (TypeError, ValueError):
                            continue

                        trade_methods = adv.get("tradeMethods") or []
                        pay_types_found: list[str] = []
                        for tm in trade_methods:
                            pay_type = tm.get("payType")
                            if pay_type:
                                pay_types_found.append(pay_type)
                                upsert_payment_method(
                                    binance_pay_type=pay_type,
                                    display_name=tm.get("tradeMethodName") or pay_type,
                                )

                        merchant_name = (
                            advertiser.get("nickName")
                            or advertiser.get("userName")
                            or "—"
                        )
                        all_ads.append(
                            P2PAd(
                                price=price,
                                min_single_trans_amount=min_uah,
                                max_single_trans_amount=max_uah,
                                available_amount=avail,
                                merchant_name=merchant_name,
                                payment_types=pay_types_found,
                            )
                        )

                    total = data.get("total") or data.get("totalCount") or len(
                        ads_raw
                    )
                    return all_ads, int(total)
            except (asyncio.TimeoutError, aiohttp.ClientError, BinanceAPIError) as e:
                if attempt == self.max_retries:
                    msg = f"Помилка Binance P2P (спроба {attempt}): {e}"
                    log_source_error("binance_p2p", msg)
                    raise
                await asyncio.sleep(2**attempt)

        raise BinanceAPIError("Невідома помилка Binance P2P")

    async def fetch_ads(
        self,
        pay_types: list[str],
    ) -> P2PResult:
        ads: list[P2PAd] = []
        total = 0
        checked = 0

        async with aiohttp.ClientSession() as session:
            page = 1
            while page <= self.max_pages:
                page_ads, total_from_api = await self.fetch_page(
                    session=session,
                    page=page,
                    pay_types=pay_types,
                )
                if page == 1:
                    total = total_from_api
                ads.extend(page_ads)
                checked += len(page_ads)

                if len(ads) >= total_from_api:
                    break
                page += 1

        return P2PResult(ads=ads, total=total, checked=checked)
