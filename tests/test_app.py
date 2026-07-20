from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from apple_store_stock.app import build_stock_response
from apple_store_stock.core import (
    AppleResponseError,
    AppleStockClient,
    InputError,
    RetryableAppleError,
    extract_product_sku,
    macau_response,
    normalize_sku,
    parse_product_query,
    parse_stock_payload,
    run_with_single_retry,
    validate_product_url,
)


def stock_payload(*statuses: str) -> dict:
    stores = []
    for index, status in enumerate(statuses, start=1):
        stores.append(
            {
                "storeNumber": f"R{index:03d}",
                "storeName": f"Store {index}",
                "partsAvailability": {
                    "MJ3D4ZP/A": {
                        "pickupDisplay": status,
                        "pickupSearchQuote": f"状态 {status}",
                        "messageTypes": {
                            "regular": {
                                "storePickupProductTitle": "14-inch MacBook Pro"
                            }
                        },
                    }
                },
                "retailStore": {
                    "distanceWithUnit": f"{index} km",
                    "address": {
                        "twoLineAddress": f"第 {index} 条地址\n香港",
                        "daytimePhone": f"1234000{index}",
                    },
                },
            }
        )
    return {
        "head": {"status": "200"},
        "body": {"content": {"pickupMessage": {"stores": stores}}},
    }


class InputTests(unittest.TestCase):
    def test_normalize_sku(self) -> None:
        self.assertEqual(normalize_sku(" mj3d4zp/a \n"), "MJ3D4ZP/A")

    def test_reject_incomplete_sku(self) -> None:
        with self.assertRaises(InputError):
            normalize_sku("MJ3D4ZP")

    def test_accept_hong_kong_exact_buy_url(self) -> None:
        url = "https://www.apple.com/hk-zh/shop/buy-mac/macbook-pro/example#specs"
        self.assertEqual(
            validate_product_url(url),
            "https://www.apple.com/hk-zh/shop/buy-mac/macbook-pro/example",
        )

    def test_reject_non_apple_or_non_buy_url(self) -> None:
        invalid = (
            "http://www.apple.com/hk-zh/shop/buy-mac/macbook-pro/example",
            "https://www.apple.com.evil.example/hk-zh/shop/buy-mac/example",
            "https://user@www.apple.com/hk-zh/shop/buy-mac/example",
            "https://www.apple.com/hk-zh/macbook-pro/",
        )
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(InputError):
                validate_product_url(url)

    def test_parse_product_query(self) -> None:
        self.assertEqual(parse_product_query("mj3d4zp/a").sku, "MJ3D4ZP/A")
        self.assertIsNotNone(
            parse_product_query(
                "https://www.apple.com/hk/shop/buy-iphone/iphone-17/example"
            ).url
        )


class ProductPageTests(unittest.TestCase):
    def test_extract_single_json_ld_sku(self) -> None:
        html = """
        <script type="application/ld+json">
          {"@type":"Product","offers":[{"sku":"MJ3D4ZP/A"}]}
        </script>
        """
        self.assertEqual(extract_product_sku(html), "MJ3D4ZP/A")

    def test_reject_page_without_sku(self) -> None:
        with self.assertRaises(InputError):
            extract_product_sku("<html><body>generic page</body></html>")

    def test_reject_page_with_multiple_skus(self) -> None:
        html = """
        <script type="application/ld+json">
          {"offers":[{"sku":"MJ3D4ZP/A"},{"sku":"MJ3E4ZP/A"}]}
        </script>
        """
        with self.assertRaises(InputError):
            extract_product_sku(html)


class PayloadTests(unittest.TestCase):
    def test_parse_available_unavailable_and_unknown(self) -> None:
        result = parse_stock_payload(
            stock_payload("available", "unavailable", "future"),
            "MJ3D4ZP/A",
            "https://www.apple.com/hk-zh/shop/buy-mac/example",
        )
        self.assertEqual(result["available_count"], 1)
        self.assertEqual(len(result["stores"]), 3)
        self.assertTrue(result["stores"][0]["available"])
        self.assertFalse(result["stores"][2]["available"])
        self.assertEqual(result["stores"][2]["status"], "future")
        self.assertEqual(result["product_name"], "14-inch MacBook Pro")
        self.assertIn("purchase_url", result)

    def test_empty_stores_is_not_reported_as_out_of_stock(self) -> None:
        payload = {
            "head": {"status": "200"},
            "body": {"content": {"pickupMessage": {"stores": []}}},
        }
        with self.assertRaises(AppleResponseError):
            parse_stock_payload(payload, "MJ3D4ZP/A")

    def test_missing_sku_is_an_error(self) -> None:
        payload = stock_payload("unavailable")
        payload["body"]["content"]["pickupMessage"]["stores"][0][
            "partsAvailability"
        ] = {}
        with self.assertRaises(AppleResponseError):
            parse_stock_payload(payload, "MJ3D4ZP/A")

    def test_malformed_payload_is_an_error(self) -> None:
        for payload in (None, {}, {"head": {"status": "500"}}):
            with self.subTest(payload=payload), self.assertRaises(AppleResponseError):
                parse_stock_payload(payload, "MJ3D4ZP/A")


class RetryAndLifecycleTests(unittest.TestCase):
    def test_query_skus_fetches_all_parts_once(self) -> None:
        skus = ("MDE34ZP/A", "MJ3D4ZP/A")
        payload = stock_payload("available")
        availability = payload["body"]["content"]["pickupMessage"]["stores"][0][
            "partsAvailability"
        ]
        availability[skus[0]] = dict(availability[skus[1]])
        page = Mock()
        page.evaluate.return_value = {
            "status": 200,
            "contentType": "application/json",
            "text": json.dumps(payload),
        }
        client = AppleStockClient()

        with (
            patch.object(client, "_ensure_shield"),
            patch.object(client, "_ensure_runtime", return_value=page),
        ):
            results = client.query_skus(skus)

        query = parse_qs(urlparse(page.evaluate.call_args.args[1]).query)
        self.assertEqual(query["parts.0"], [skus[0]])
        self.assertEqual(query["parts.1"], [skus[1]])
        self.assertEqual([result["sku"] for result in results], list(skus))
        page.evaluate.assert_called_once()

    def test_retry_once_then_succeed(self) -> None:
        attempts = 0
        resets = 0

        def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RetryableAppleError("541")
            return "ok"

        def reset() -> None:
            nonlocal resets
            resets += 1

        self.assertEqual(run_with_single_retry(operation, reset), "ok")
        self.assertEqual(attempts, 2)
        self.assertEqual(resets, 1)

    def test_second_retryable_failure_becomes_502_error(self) -> None:
        attempts = 0
        resets = 0

        def operation() -> None:
            nonlocal attempts
            attempts += 1
            raise RetryableAppleError("541")

        def reset() -> None:
            nonlocal resets
            resets += 1

        with self.assertRaises(AppleResponseError):
            run_with_single_retry(operation, reset)
        self.assertEqual(attempts, 2)
        self.assertEqual(resets, 1)

    def test_close_releases_browser_resources(self) -> None:
        client = AppleStockClient()
        context = Mock()
        browser = Mock()
        playwright = Mock()
        client._page = Mock()
        client._context = context
        client._browser = browser
        client._playwright = playwright

        client.close()

        context.close.assert_called_once_with()
        browser.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()
        self.assertIsNone(client._page)
        self.assertIsNone(client._context)
        self.assertIsNone(client._browser)
        self.assertIsNone(client._playwright)

    def test_close_tolerates_disconnected_playwright_driver(self) -> None:
        client = AppleStockClient()
        context = Mock()
        browser = Mock()
        playwright = Mock()
        context.close.side_effect = Exception("driver already closed")
        browser.close.side_effect = Exception("driver already closed")
        playwright.stop.side_effect = Exception("driver already closed")
        client._context = context
        client._browser = browser
        client._playwright = playwright

        client.close()

        context.close.assert_called_once_with()
        browser.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()


class MacauTests(unittest.TestCase):
    def test_macau_response_has_official_stores(self) -> None:
        result = macau_response()
        self.assertFalse(result["realtime_supported"])
        self.assertEqual(
            [store["store_number"] for store in result["stores"]],
            ["R697", "R672"],
        )

    def test_macau_api_does_not_call_apple_client(self) -> None:
        client = Mock()
        result = build_stock_response({"region": "mo"}, client)
        self.assertFalse(result["realtime_supported"])
        client.query_stock.assert_not_called()

    def test_hong_kong_api_calls_client(self) -> None:
        client = Mock()
        client.query_stock.return_value = {"region": "hk"}
        result = build_stock_response({"region": "hk", "query": "MJ3D4ZP/A"}, client)
        self.assertEqual(result, {"region": "hk"})
        client.query_stock.assert_called_once_with("MJ3D4ZP/A")

    def test_hong_kong_preset_queries_all_four_skus_once(self) -> None:
        skus = ("MDE34ZP/A", "MDE64ZP/A", "MJ3D4ZP/A", "MJ3E4ZP/A")
        client = Mock()
        client.query_skus.return_value = [
            {
                "sku": sku,
                "checked_at": "2026-07-20T12:00:00+08:00",
                "available_count": index % 2,
                "stores": [{"store_name": f"Store {index}"}],
            }
            for index, sku in enumerate(skus)
        ]

        result = build_stock_response(
            {"region": "hk", "preset": "macbook-pro-1tb"}, client
        )

        client.query_skus.assert_called_once_with(skus)
        client.query_stock.assert_not_called()
        self.assertEqual(result["variant_count"], 4)
        self.assertEqual(result["store_count"], 1)
        self.assertEqual(result["available_count"], 2)
        self.assertEqual(len(result["stores"]), 4)
        self.assertEqual(
            [store["configuration"] for store in result["stores"]],
            [
                "24GB · 1TB · 太空黑色",
                "24GB · 1TB · 银色",
                "32GB · 1TB · 太空黑色",
                "32GB · 1TB · 银色",
            ],
        )


if __name__ == "__main__":
    unittest.main()
