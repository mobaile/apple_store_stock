# Apple Store 库存查询

本地查询香港 Apple Store 的精确 SKU 库存。支持直接输入 Apple part number，或粘贴完成全部配置后的 Apple 香港商品网址。

## 功能

- 精确到颜色、内存和储存空间的 SKU 查询。
- 展示 Apple 返回的所有香港门店、原始库存状态、地址和电话。
- 内置以下 14 英寸 M5 MacBook Pro 预设：
  - `MJ3D4ZP/A`：32GB、1TB、太空黑色。
  - `MJ3E4ZP/A`：32GB、1TB、银色。
- 澳门只展示官方两家直营店及电话，明确标注 Apple 未开放实时库存查询。
- 浏览器和 SHIELD Cookie 仅保存在内存，退出程序后销毁。

## 环境要求

- macOS 上已安装 Google Chrome。
- 已安装 [uv](https://docs.astral.sh/uv/)。
- Python 3.13；uv 会按照 `.python-version` 自动选择环境。

## 安装与启动

```bash
cd /Volumes/resourse/CurCode/apple_store_stock
uv sync
uv run apple-store-stock
```

程序监听：

```text
http://127.0.0.1:8765
```

默认会自动打开浏览器。如不需要自动打开：

```bash
uv run apple-store-stock --no-open
```

按 `Control+C` 停止服务并关闭无头 Chrome。

## 如何查询

可以输入精确 SKU：

```text
MJ3D4ZP/A
```

也可以在 Apple 香港购买页面完成尺寸、芯片、内存、硬盘和颜色选择后，复制最终商品网址。泛产品页面包含多个或没有唯一 SKU，程序会拒绝查询，避免把错误型号的结果当成目标配置。

首次香港查询需要启动无头 Chrome 并完成 Apple SHIELD 校验，通常比后续查询慢。若 Cookie 过期或 Apple 返回 541，程序会自动重建一次会话；第二次仍失败时会明确报错，不会显示成“无货”。

## HTTP 接口

香港：

```bash
curl -sS http://127.0.0.1:8765/api/stock \
  -H 'Content-Type: application/json' \
  -d '{"region":"hk","query":"MJ3D4ZP/A"}'
```

澳门：

```bash
curl -sS http://127.0.0.1:8765/api/stock \
  -H 'Content-Type: application/json' \
  -d '{"region":"mo"}'
```

澳门响应中的 `realtime_supported` 固定为 `false`。程序不会调用不存在的澳门库存端点，也不会推测门店库存。

## 测试

```bash
uv run python -m unittest discover -s tests -v
```

测试使用固定响应样本，不依赖当前库存。实时接口只在手动冒烟检查时访问。

## 已知限制

- Apple 的 `fulfillment-messages` 是未公开接口，字段或 SHIELD 校验可能随时改变。
- 当前仅为单用户本地工具，串行处理查询。
- 不包含定时监控、通知、数据库、登录或远程部署。
- 澳门官网没有在线购买及门店提取接口，因此无法可靠查询实时库存；请致电门店确认。

