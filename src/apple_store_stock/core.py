from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, TypeVar
from urllib.parse import urlencode, urlparse, urlunparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from playwright_stealth import Stealth

HK_PRIME_URL = "https://www.apple.com/hk-zh/shop/buy-mac/macbook-pro"
HK_FULFILLMENT_URL = "https://www.apple.com/hk-zh/shop/fulfillment-messages"
APPLE_STORE_LIST_MO = "https://www.apple.com/mo/retail/storelist/"
MAX_QUERY_LENGTH = 8_192

PRESETS = (
    {
        "sku": "MJ3D4ZP/A",
        "label": "14 英寸 M5 · 32GB · 1TB · 太空黑色",
    },
    {
        "sku": "MJ3E4ZP/A",
        "label": "14 英寸 M5 · 32GB · 1TB · 银色",
    },
)

MACAU_STORES = (
    {
        "store_number": "R697",
        "store_name": "路氹金光大道",
        "address": "路氹城伦敦人购物中心",
        "phone": "87917000",
        "url": "https://www.apple.com/mo/retail/cotaistrip/",
    },
    {
        "store_number": "R672",
        "store_name": "澳门银河",
        "address": "路氹城澳门银河™时尚汇",
        "phone": "87919000",
        "url": "https://www.apple.com/mo/retail/galaxymacau/",
    },
)

_SKU_RE = re.compile(r"^[A-Z0-9]{5,10}[A-Z]{2}/A$")
_HK_BUY_PREFIXES = (
    "/hk-zh/shop/buy-",
    "/hk/shop/buy-",
    "/hk/en/shop/buy-",
)
_SHIELD_COOKIE_NAMES = {"shld_bt_ck", "shld_bt_m", "sh_spksy"}


class StockError(Exception):
    status_code = 500
    code = "internal_error"

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": str(self)}


class InputError(StockError):
    status_code = 400
    code = "invalid_input"


class AppleResponseError(StockError):
    status_code = 502
    code = "apple_response_error"


class BrowserUnavailableError(StockError):
    status_code = 503
    code = "browser_unavailable"


class RetryableAppleError(Exception):
    """Apple 的临时拦截、非 JSON 响应或浏览器网络失败。"""


@dataclass(frozen=True, slots=True)
class ProductQuery:
    sku: str | None = None
    url: str | None = None


class _JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside_json_ld = False
        self._parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = {key.lower(): value for key, value in attrs}
        if (attributes.get("type") or "").lower() == "application/ld+json":
            self._inside_json_ld = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._inside_json_ld:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._inside_json_ld:
            self.scripts.append("".join(self._parts))
            self._inside_json_ld = False
            self._parts = []


def normalize_sku(value: str) -> str:
    sku = re.sub(r"\s+", "", value).upper()
    if not _SKU_RE.fullmatch(sku):
        raise InputError("SKU 格式不正确，应类似 MJ3D4ZP/A，并包含末尾的 /A。")
    return sku


def validate_product_url(value: str) -> str:
    if len(value) > MAX_QUERY_LENGTH:
        raise InputError("商品网址过长。")

    parsed = urlparse(value.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise InputError("商品网址端口无效。") from exc

    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "www.apple.com"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        raise InputError("只接受 https://www.apple.com 的香港商品网址。")

    if not any(parsed.path.startswith(prefix) for prefix in _HK_BUY_PREFIXES):
        raise InputError("网址必须是 Apple 香港商店的购买页面。")

    return urlunparse(parsed._replace(fragment=""))


def parse_product_query(value: str) -> ProductQuery:
    query = value.strip()
    if not query:
        raise InputError("请输入 SKU 或 Apple 香港商品网址。")
    if len(query) > MAX_QUERY_LENGTH:
        raise InputError("查询内容过长。")
    if query.lower().startswith(("http://", "https://")):
        return ProductQuery(url=validate_product_url(query))
    return ProductQuery(sku=normalize_sku(query))


def _collect_json_ld_skus(value: Any, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "sku" and isinstance(child, str):
                try:
                    found.add(normalize_sku(child))
                except InputError:
                    pass
            else:
                _collect_json_ld_skus(child, found)
    elif isinstance(value, list):
        for child in value:
            _collect_json_ld_skus(child, found)


def extract_product_sku(html: str) -> str:
    parser = _JsonLdParser()
    parser.feed(html)
    found: set[str] = set()
    for script in parser.scripts:
        try:
            data = json.loads(script)
        except (json.JSONDecodeError, TypeError):
            continue
        _collect_json_ld_skus(data, found)

    if not found:
        raise InputError(
            "这个页面没有唯一的商品 SKU。请先在 Apple 页面完成全部配置，再复制最终网址。"
        )
    if len(found) != 1:
        raise InputError(
            "这个页面包含多个商品 SKU，不是精确配置页。请复制最终配置的网址。"
        )
    return found.pop()


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _address_text(address: dict[str, Any]) -> str:
    two_lines = address.get("twoLineAddress")
    if isinstance(two_lines, str) and two_lines.strip():
        return two_lines.strip()
    if isinstance(two_lines, list):
        text = "\n".join(_string(part) for part in two_lines if _string(part))
        if text:
            return text

    fields = (
        "companyName",
        "street",
        "street2",
        "street3",
        "city",
        "postalCode",
    )
    return "\n".join(
        _string(address.get(field)) for field in fields if _string(address.get(field))
    )


def parse_stock_payload(
    payload: Any, sku: str, purchase_url: str | None = None
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AppleResponseError("Apple 库存接口返回了无法识别的数据。")

    head_status = (payload.get("head") or {}).get("status")
    if head_status not in (None, 200, "200"):
        raise AppleResponseError(f"Apple 库存接口返回状态 {head_status}。")

    try:
        pickup = payload["body"]["content"]["pickupMessage"]
    except (KeyError, TypeError) as exc:
        raise AppleResponseError(
            "Apple 响应缺少 pickupMessage，接口结构可能已变化。"
        ) from exc

    if not isinstance(pickup, dict):
        raise AppleResponseError("Apple 响应中的 pickupMessage 格式无效。")
    stores_data = pickup.get("stores")
    if not isinstance(stores_data, list) or not stores_data:
        raise AppleResponseError(
            "Apple 没有返回香港门店；SKU 可能无效、地区不匹配或接口已变化。"
        )

    stores: list[dict[str, Any]] = []
    product_name = ""
    for store in stores_data:
        if not isinstance(store, dict):
            raise AppleResponseError("Apple 返回了格式无效的门店记录。")
        availability = (store.get("partsAvailability") or {}).get(sku)
        if not isinstance(availability, dict):
            raise AppleResponseError(
                f"门店 {store.get('storeName') or '未知'} 的响应中缺少 SKU {sku}。"
            )

        message_types = availability.get("messageTypes") or {}
        regular = message_types.get("regular") or {}
        if not isinstance(regular, dict):
            regular = {}

        if not product_name:
            product_name = _string(regular.get("storePickupProductTitle"))

        raw_status = _string(availability.get("pickupDisplay")).lower()
        status = raw_status or "unknown"
        quote = (
            _string(availability.get("pickupSearchQuote"))
            or _string(regular.get("storePickupQuote"))
            or _string(regular.get("storePickupSearchQuote"))
            or "Apple 未提供状态说明"
        )
        retail = store.get("retailStore") or {}
        if not isinstance(retail, dict):
            retail = {}
        address = retail.get("address") or {}
        if not isinstance(address, dict):
            address = {}

        stores.append(
            {
                "store_number": _string(store.get("storeNumber")),
                "store_name": _string(store.get("storeName")) or "未知门店",
                "status": status,
                "available": status == "available",
                "quote": quote,
                "address": _address_text(address),
                "phone": _string(address.get("daytimePhone")),
                "distance": _string(retail.get("distanceWithUnit"))
                or _string(store.get("distanceWithUnit")),
            }
        )

    result: dict[str, Any] = {
        "region": "hk",
        "realtime_supported": True,
        "sku": sku,
        "product_name": product_name or sku,
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "available_count": sum(store["available"] for store in stores),
        "stores": stores,
    }
    if purchase_url:
        result["purchase_url"] = purchase_url
    return result


def macau_response() -> dict[str, Any]:
    return {
        "region": "mo",
        "realtime_supported": False,
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "message": (
            "Apple 澳门没有在线购买及门店提取库存接口，无法可靠查询实时库存。"
            "请致电门店确认，页面不会把未知状态显示成无货。"
        ),
        "source_url": APPLE_STORE_LIST_MO,
        "stores": [dict(store) for store in MACAU_STORES],
    }


T = TypeVar("T")


def run_with_single_retry(operation: Callable[[], T], reset: Callable[[], None]) -> T:
    try:
        return operation()
    except RetryableAppleError:
        reset()
    try:
        return operation()
    except RetryableAppleError as exc:
        raise AppleResponseError(
            "Apple 连续两次拒绝或中断库存查询，请稍后重试。"
        ) from exc


class AppleStockClient:
    def __init__(self, chrome_channel: str = "chrome") -> None:
        self.chrome_channel = chrome_channel
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def close(self) -> None:
        context, browser, playwright = (
            self._context,
            self._browser,
            self._playwright,
        )
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        for resource, method in (
            (context, "close"),
            (browser, "close"),
            (playwright, "stop"),
        ):
            if resource is None:
                continue
            try:
                getattr(resource, method)()
            except Exception:
                # Playwright 驱动可能已被 Control+C 关闭；清理必须继续且保持幂等。
                pass

    def _restart(self) -> None:
        self.close()

    def _ensure_runtime(self) -> Page:
        if (
            self._page is not None
            and self._browser is not None
            and self._browser.is_connected()
        ):
            return self._page

        self.close()
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                channel=self.chrome_channel,
                headless=True,
            )
            self._context = self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.4 Safari/605.1.15"
                ),
                locale="zh-HK",
                viewport={"width": 1280, "height": 1200},
            )
            Stealth().apply_stealth_sync(self._context)
            self._page = self._context.new_page()
        except PlaywrightError as exc:
            self.close()
            raise BrowserUnavailableError(
                "无法启动本机 Google Chrome，请确认 Chrome 已安装且可以正常打开。"
            ) from exc
        return self._page

    def _has_shield_cookie(self) -> bool:
        if self._context is None:
            return False
        try:
            names = {
                cookie["name"]
                for cookie in self._context.cookies("https://www.apple.com")
            }
        except PlaywrightError:
            return False
        return _SHIELD_COOKIE_NAMES.issubset(names)

    def _wait_for_shield(self, timeout_seconds: float = 15.0) -> None:
        page = self._ensure_runtime()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._has_shield_cookie():
                return
            page.wait_for_timeout(250)

    def _navigate(self, url: str, product_url: bool = False) -> Page:
        page = self._ensure_runtime()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError as exc:
            raise RetryableAppleError("Apple 页面加载超时。") from exc
        except PlaywrightError as exc:
            raise RetryableAppleError("Apple 页面加载失败。") from exc

        status = response.status if response else 0
        if status == 404 and product_url:
            raise InputError("Apple 商品网址不存在或已经失效。")
        if status >= 400 or status == 0:
            raise RetryableAppleError(f"Apple 页面返回 HTTP {status}。")

        if product_url:
            validate_product_url(page.url)
        self._wait_for_shield()
        return page

    def _ensure_shield(self) -> None:
        self._ensure_runtime()
        if not self._has_shield_cookie():
            self._navigate(HK_PRIME_URL)

    def _sku_from_url(self, url: str) -> str:
        page = self._navigate(url, product_url=True)
        return extract_product_sku(page.content())

    def _fetch_payload(self, sku: str) -> dict[str, Any]:
        page = self._ensure_runtime()
        query_url = f"{HK_FULFILLMENT_URL}?{
            urlencode(
                {
                    'fae': 'true',
                    'pl': 'true',
                    'parts.0': sku,
                    'location': 'Hong Kong',
                    'searchNearby': 'true',
                }
            )
        }"
        try:
            result = page.evaluate(
                """
                async (url) => {
                    const controller = new AbortController();
                    const timeout = setTimeout(() => controller.abort(), 30000);
                    try {
                        const response = await fetch(url, {
                            credentials: "include",
                            signal: controller.signal,
                            headers: {
                                "Accept": "application/json, */*",
                                "X-Requested-With": "XMLHttpRequest"
                            }
                        });
                        return {
                            status: response.status,
                            contentType: response.headers.get("content-type") || "",
                            text: await response.text()
                        };
                    } finally {
                        clearTimeout(timeout);
                    }
                }
                """,
                query_url,
            )
        except PlaywrightError as exc:
            raise RetryableAppleError("Apple 库存请求失败。") from exc

        if not isinstance(result, dict):
            raise RetryableAppleError("Apple 库存接口没有返回有效结果。")
        status = result.get("status")
        content_type = _string(result.get("contentType")).lower()
        body = result.get("text")
        if status != 200 or "json" not in content_type or not isinstance(body, str):
            raise RetryableAppleError(
                f"Apple 库存接口返回 HTTP {status} 或非 JSON 内容。"
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RetryableAppleError("Apple 库存接口返回了无效 JSON。") from exc
        if not isinstance(payload, dict):
            raise RetryableAppleError("Apple 库存接口返回了无效对象。")
        return payload

    def query_stock(self, query: str) -> dict[str, Any]:
        product_query = parse_product_query(query)
        resolved_sku = product_query.sku

        def operation() -> dict[str, Any]:
            nonlocal resolved_sku
            if resolved_sku is None:
                assert product_query.url is not None
                resolved_sku = self._sku_from_url(product_query.url)
            else:
                self._ensure_shield()
            payload = self._fetch_payload(resolved_sku)
            return parse_stock_payload(payload, resolved_sku, product_query.url)

        return run_with_single_retry(operation, self._restart)
