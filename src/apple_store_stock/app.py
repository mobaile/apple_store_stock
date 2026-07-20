from __future__ import annotations

import argparse
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import urlparse

from .core import AppleStockClient, InputError, StockError, macau_response

HOST = "127.0.0.1"
PORT = 8765
MAX_REQUEST_BYTES = 16 * 1024


def build_stock_response(payload: Any, client: AppleStockClient) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InputError("请求必须是 JSON 对象。")
    region = str(payload.get("region") or "").strip().lower()
    if region == "mo":
        return macau_response()
    if region != "hk":
        raise InputError("region 只支持 hk 或 mo。")
    query = payload.get("query")
    if not isinstance(query, str):
        raise InputError("香港查询必须提供 query 字符串。")
    return client.query_stock(query)


class StockHTTPServer(HTTPServer):
    # ponytail: 单用户本地工具使用串行服务器；需要远程多人访问时再增加并发。
    def __init__(self, address: tuple[str, int], client: AppleStockClient) -> None:
        super().__init__(address, StockRequestHandler)
        self.stock_client = client


class StockRequestHandler(BaseHTTPRequestHandler):
    server: StockHTTPServer

    def _common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_bytes(
        self, status: int, content_type: str, body: bytes, cache: str = "no-store"
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            body = files("apple_store_stock").joinpath("web/index.html").read_bytes()
            self._send_bytes(
                200,
                "text/html; charset=utf-8",
                body,
                cache="no-cache",
            )
            return
        if path == "/favicon.ico":
            self._send_bytes(204, "image/x-icon", b"")
            return
        self._send_json(404, {"error": "not_found", "message": "页面不存在。"})

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/stock":
            self._send_json(404, {"error": "not_found", "message": "接口不存在。"})
            return

        try:
            raw_length = self.headers.get("Content-Length")
            length = int(raw_length or "0")
            if length <= 0:
                raise InputError("请求体不能为空。")
            if length > MAX_REQUEST_BYTES:
                raise InputError("请求体超过 16KB 限制。")
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InputError("请求体不是有效的 UTF-8 JSON。") from exc
            result = build_stock_response(payload, self.server.stock_client)
        except StockError as exc:
            self._send_json(exc.status_code, exc.as_dict())
            return
        except (TypeError, ValueError):
            self._send_json(
                400,
                {"error": "invalid_input", "message": "Content-Length 无效。"},
            )
            return
        except Exception:
            self.log_error("处理库存请求时发生未预期错误")
            self._send_json(
                500,
                {"error": "internal_error", "message": "服务器发生未预期错误。"},
            )
            return
        self._send_json(200, result)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="香港 Apple Store 库存查询")
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="启动后不自动打开浏览器",
    )
    args = parser.parse_args()

    client = AppleStockClient()
    try:
        server = StockHTTPServer((HOST, PORT), client)
    except OSError as exc:
        raise SystemExit(f"无法监听 http://{HOST}:{PORT}：{exc}") from exc

    url = f"http://{HOST}:{PORT}"
    print(f"Apple Store 库存查询已启动：{url}")
    print("按 Control+C 停止。")
    if not args.no_open:
        timer = threading.Timer(0.4, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        server.server_close()
        client.close()


if __name__ == "__main__":
    main()
