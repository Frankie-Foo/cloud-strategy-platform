# 云端多策略平台 API v1 完整接口文档

文档版本：`1.0`

接口版本：`v1`

状态：描述当前代码中已经实现、可以调用的 14 个 HTTP 接口

机器可读契约：[OpenAPI 3.1](openapi-v1.json)

目标平台接口（尚未实现）：[完整平台 API 设计](PLATFORM_API_TARGET.md)

> 重要：本文只把已经实现的能力写成“可用接口”。策略管理、选股任务、回测、按策略隔离的模拟盘、自动复盘和 Python 沙箱管理尚未提供外部 HTTP 接口，详见目标平台设计文档。

## 1. 接口边界

当前 v1 是 AI 投资系统与云端 Alpaca 能力之间的安全网关，提供：

- 共享 SIP 实时事件的游标式读取；
- SIP 历史分钟线、报价和新闻的标准化读取；
- 统一的时点特征向量读取；
- 按 `strategy_id` 隔离的派生信号读取；
- 受双重开关保护的 Alpaca Paper 账户查询和只做多委托。

当前 v1 明确不提供：

- Alpaca Key、Secret、原始请求头或凭据查询；
- 任意 URL 转发、通用 `/proxy` 或 `/alpaca` 代理；
- Live 实盘接口；
- 做空、裸卖空或开放式订单参数透传；
- 给协作者的原始行情、特征、账户或下单权限；
- HTTP 形式的策略增删改、选股、回测、复盘或 Python 执行。

### 1.1 当前隔离程度

| 数据/能力 | 当前隔离方式 | 状态 |
|---|---|---|
| 派生信号 | token 与一个 `strategy_id` 强绑定 | 已实现 |
| 特征、行情 | 独立服务 token 与 scope | 已实现 |
| Alpaca 凭据 | 只在云端进程环境中存在 | 已实现 |
| Paper 读写 | 独立 scope + 服务端写开关 | 已实现 |
| Paper 订单/持仓 | 目前仍是整个云端 Paper 账户级 | **尚未按策略隔离** |
| 策略配置、选股、回测、复盘 | 内部存储/模块存在，但无 HTTP 管理面 | **接口尚未实现** |

因此，不能把 v1 Paper 接口直接开放给普通同事，也不能声称它已经完成多策略账本隔离。

## 2. 地址、协议和编码

本地默认地址：

```text
http://127.0.0.1:8765
```

生产环境必须使用 HTTPS 反向代理，例如：

```text
https://strategy-api.example.com
```

通用规则：

- 请求和响应编码为 UTF-8；
- POST 请求的 `Content-Type` 为 `application/json`；
- POST body 必须是 JSON object，最大 `64 KiB`；
- 所有响应均带 `Cache-Control: no-store`；
- 时间统一使用带 UTC 时区的 ISO 8601，例如 `2026-07-22T15:30:00Z` 或 `2026-07-22T15:30:00+00:00`；
- `+08:00`、无时区时间及结束时间不晚于开始时间会返回 `400 invalid_request`；
- 股票代码会去掉首尾空格并转为大写；多个代码使用英文逗号分隔并自动去重、排序；
- 金额、数量及部分成交字段保留为十进制字符串，调用方不要用二进制浮点数处理资金。

## 3. 身份认证和权限

除 `/health` 外，所有接口使用 Bearer token：

```http
Authorization: Bearer <TOKEN>
```

token 是随机秘密，只在签发时显示一次；服务端数据库仅保存 token 的 SHA-256 摘要。生产环境应通过密钥管理器传递，不应写进代码、Git、文档、聊天记录或日志。

### 3.1 Scope 权限表

| Scope | 建议主体 | 可以调用 | 不可以调用 |
|---|---|---|---|
| `signals:read` | 普通同事/信号消费者 | 绑定策略的信号接口 | 其他策略、特征、行情、Paper |
| `features:read` | AI 特征客户端 | 时点特征接口 | 信号、原始/历史行情、Paper |
| `market-data:read` | AI 行情摄取服务 | 事件、分钟线、报价、新闻 | 特征、信号、Paper |
| `market-data:write` | AI 行情摄取协调器 | 声明短期 SIP 标的租约，并读取行情 | 特征、信号、Paper |
| `paper:read` | AI 对账服务 | Paper 账户、持仓、订单查询 | 下单、撤单、其他域 |
| `paper:write` | AI 执行服务 | Paper 查询、下单、撤单 | 其他域；服务端关闭时仍不能写 |

`paper:write` 可以调用 Paper 只读接口，但不能跨到 `features:read`、`market-data:read` 或 `signals:read`。

`signals:read` token 必须在签发时绑定且只能绑定一个 `strategy_id`。用策略 `alpha` 的 token 请求策略 `beta` 会统一返回 `401 unauthorized`，不泄露 `beta` 是否存在。

### 3.2 本地签发 token

以下命令由管理员在云端仓库执行：

```powershell
.\.venv\Scripts\python -m scripts.issue_token `
  --principal ai-investment-features `
  --scope features:read

.\.venv\Scripts\python -m scripts.issue_token `
  --principal colleague-alice `
  --scope signals:read `
  --strategy-id alpha
```

命令输出中的 token 只显示一次。不要把真实输出粘贴到工单或群聊。

## 4. 路由总表

| 方法 | 路径 | Scope | 用途 |
|---|---|---|---|
| GET | `/health` | 无 | 健康检查 |
| GET | `/v1/strategies/{strategy_id}/signals` | `signals:read` | 读取派生信号 |
| GET | `/v1/features/{symbol}` | `features:read` | 读取时点特征向量 |
| POST | `/v1/market-data/subscriptions` | `market-data:write` | 向唯一 SIP Owner 租约订阅标的 |
| GET | `/v1/market-data/events` | `market-data:read` | 游标读取共享 SIP 事件 |
| GET | `/v1/market-data/bars` | `market-data:read` | 读取 SIP 1 分钟复权 K 线 |
| GET | `/v1/market-data/quotes` | `market-data:read` | 读取 SIP 历史报价 |
| GET | `/v1/market-data/news` | `market-data:read` | 读取新闻元数据 |
| GET | `/v1/paper/account` | `paper:read` | 读取 Paper 账户摘要 |
| GET | `/v1/paper/positions` | `paper:read` | 读取 Paper 持仓 |
| GET | `/v1/paper/orders/open` | `paper:read` | 读取 Paper 未完成订单 |
| GET | `/v1/paper/orders/by-client-id` | `paper:read` | 按幂等 ID 查订单 |
| POST | `/v1/paper/orders` | `paper:write` | 提交只做多 Paper 委托 |
| DELETE | `/v1/paper/orders/cancel/{order_id}` | `paper:write` | 撤销 Paper 委托 |

## 5. 通用响应和错误

成功响应包含 `api_version: "v1"`，健康接口除外仍使用相同字段。错误响应刻意保持简洁，避免泄露凭据、数据库或上游信息：

```json
{"error":"unauthorized"}
```

| HTTP | `error` | 含义 | 是否建议重试 |
|---:|---|---|---|
| 400 | `invalid_request` | 参数、UTC、body、数量或模型校验失败 | 修正请求后再试 |
| 401 | `unauthorized` | 未带 token、token 无效、scope 不符或策略绑定不符 | 不自动重试；检查授权 |
| 403 | `writes_disabled` | token 有 `paper:write`，但服务端写开关关闭 | 不自动重试；由管理员开启 |
| 404 | `not_found` | 路径不存在 | 不重试 |
| 405 | `method_not_allowed` | 应用不支持该 HTTP 方法 | 不重试 |
| 502 | `upstream_failed` | Alpaca 行情或 Paper 上游失败 | GET 可指数退避；POST 仅用同一幂等 ID 重试 |
| 503 | `unavailable` | 当前服务未配置所需存储或适配器 | 稍后重试或联系管理员 |

服务器不会把模型校验细节、Alpaca 错误正文或凭据返回给客户端。

## 6. 健康检查

### `GET /health`

无需认证。它只表示 HTTP 应用已经就绪，不代表 Alpaca 上游一定可用。

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

响应：

```json
{
  "api_version": "v1",
  "status": "ready"
}
```

## 7. 派生信号

### `GET /v1/strategies/{strategy_id}/signals`

所需 scope：`signals:read`，且 token 必须绑定路径中的同一个策略。

| 参数 | 位置 | 必填 | 规则 |
|---|---|---|---|
| `strategy_id` | path | 是 | `^[a-z][a-z0-9_-]{0,63}$` |
| `since` | query | 否 | UTC 时间；只返回此时间点及之后的信号 |

请求：

```http
GET /v1/strategies/alpha/signals?since=2026-07-22T14%3A30%3A00Z HTTP/1.1
Authorization: Bearer <SIGNALS_TOKEN>
```

响应：

```json
{
  "api_version": "v1",
  "principal_id": "colleague-alice",
  "signals": [
    {
      "signal_id": "alpha-20260722-AAPL-enter",
      "strategy_id": "alpha",
      "strategy_version": "2026.07.22.1",
      "symbol": "AAPL",
      "asof_utc": "2026-07-22T15:31:00Z",
      "action": "enter_long",
      "reason": "gap and volume conditions passed",
      "feature_provenance": [
        "cloud.alpaca.sip|feature:minute_return@sip.minute.v1"
      ]
    }
  ]
}
```

`action` 只允许 `watch`、`enter_long`、`exit_long`。响应不包含原始 bar/quote、Alpaca Key、Paper 账户状态或下单函数。

## 8. 时点特征

### `GET /v1/features/{symbol}`

所需 scope：`features:read`。

| 参数 | 位置 | 必填 | 规则 |
|---|---|---|---|
| `symbol` | path | 是 | 股票代码，例如 `AAPL` |
| `asof` | query | 是 | UTC 时间；读取不晚于该时点的各特征最新值 |

请求：

```http
GET /v1/features/AAPL?asof=2026-07-22T15%3A30%3A00Z HTTP/1.1
Authorization: Bearer <FEATURE_TOKEN>
```

有数据时：

```json
{
  "api_version": "v1",
  "feature_vector": {
    "symbol": "AAPL",
    "asof_utc": "2026-07-22T15:30:00Z",
    "input_event_id": "35cb34e89813f0000000000000000000000000000000000000000000000000000",
    "features": [
      {
        "name": "close",
        "value": 224.31,
        "asof_utc": "2026-07-22T15:30:00Z",
        "definition_version": "sip.minute.v1",
        "provenance": "cloud.alpaca.sip|feature:close@sip.minute.v1"
      },
      {
        "name": "minute_return",
        "value": 0.0012,
        "asof_utc": "2026-07-22T15:30:00Z",
        "definition_version": "sip.minute.v1",
        "provenance": "cloud.alpaca.sip|feature:minute_return@sip.minute.v1"
      }
    ]
  }
}
```

在该时点之前没有数据时返回 200，不是 404：

```json
{"api_version":"v1","feature_vector":null}
```

系统不猜测缺失事实；单个 `FeatureValue.value` 也可能是 `null`。

## 9. 市场数据

读取接口要求 `market-data:read`；订阅协调接口要求 `market-data:write`，该 scope 同时包含读取能力。它们只返回平台规定的标准化数据，不是通用 Alpaca 代理。实时账本保留每根分钟 bar；quote 按“每个 symbol、每个 UTC 秒最多一条”采样，防止高频报价挤压分钟 bar 和其他标的。

### 9.1 `POST /v1/market-data/subscriptions`

AI 行情客户端在读取事件前先声明一个有期限的标的租约。云端唯一 SIP Owner 会动态订阅所有未过期租约的标的并持续写入原始事件账本；租约过期后自动移除，防止僵尸订阅长期占用连接。

```http
POST /v1/market-data/subscriptions HTTP/1.1
Authorization: Bearer <MARKET_DATA_WRITE_TOKEN>
Content-Type: application/json

{
  "symbols": ["AAPL", "MSFT"],
  "replay_from_utc": "2026-07-22T14:30:00Z",
  "expires_at_utc": "2026-07-22T21:05:00Z"
}
```

响应中的 `start_after_sequence` 是 `replay_from_utc` 之前最后一个已落库事件的游标；客户端从该游标读取，既能回放已落库事件，也能继续消费实时事件。

```json
{
  "api_version": "v1",
  "symbols": ["AAPL", "MSFT"],
  "expires_at_utc": "2026-07-22T21:05:00+00:00",
  "start_after_sequence": 1200
}
```

标的数必须为 `1..500`，时间必须是 UTC，结束时间晚于回放起点，且窗口不得超过两天。普通同事的 `signals:read` token 不能调用此接口。

### 9.2 `GET /v1/market-data/events`

用于 AI 行情客户端轮询云端共享 SIP 事件日志。

| 参数 | 必填 | 默认 | 规则 |
|---|---|---|---|
| `symbols` | 是 | 无 | 英文逗号分隔；至少一个 |
| `after` | 否 | `0` | 非负整数；仅返回 sequence 大于该值的事件 |
| `limit` | 否 | `1000` | `1..10000` |

```http
GET /v1/market-data/events?symbols=AAPL,MSFT&after=1200&limit=1000 HTTP/1.1
Authorization: Bearer <MARKET_DATA_TOKEN>
```

响应中 `event` 是 bar 或 quote：

```json
{
  "api_version": "v1",
  "events": [
    {
      "sequence": 1201,
      "event": {
        "event_type": "bar",
        "symbol": "AAPL",
        "ts_utc": "2026-07-22T15:30:00Z",
        "open": 224.1,
        "high": 224.5,
        "low": 224.0,
        "close": 224.31,
        "volume": 18452,
        "trade_count": 593,
        "vwap": 224.28,
        "feed": "sip",
        "provenance": "cloud.alpaca.sip"
      }
    }
  ],
  "next_sequence": 1201
}
```

quote 事件字段为 `bid_price`、`ask_price`、`bid_size`、`ask_size` 和相同的公共字段。无新数据时 `events=[]`，`next_sequence` 保持请求中的 `after`。客户端处理成功后再持久化 `next_sequence`，可以做到至少一次消费。

### 9.3 `GET /v1/market-data/bars`

返回 SIP feed 的 `1Min`、split-adjusted 历史分钟线。适配器会自动读取 Alpaca 的所有分页，再合并成一个响应。

| 参数 | 必填 | 规则 |
|---|---|---|
| `symbols` | 是 | 英文逗号分隔 |
| `start` | 是 | UTC 开始时间 |
| `end` | 是 | UTC 结束时间，必须晚于 `start` |

```http
GET /v1/market-data/bars?symbols=AAPL&start=2026-07-22T14%3A30%3A00Z&end=2026-07-22T15%3A30%3A00Z HTTP/1.1
Authorization: Bearer <MARKET_DATA_TOKEN>
```

```json
{
  "api_version": "v1",
  "bars": [
    {
      "symbol": "AAPL",
      "ts_utc": "2026-07-22T14:30:00Z",
      "open": 223.8,
      "high": 224.0,
      "low": 223.7,
      "close": 223.95,
      "volume": 22140,
      "trade_count": 641,
      "vwap": 223.91,
      "source": "cloud.alpaca.market_data",
      "feed": "sip",
      "adjustment": "split_adjusted"
    }
  ]
}
```

### 9.4 `GET /v1/market-data/quotes`

参数与 bars 相同。响应：

```json
{
  "api_version": "v1",
  "quotes": [
    {
      "symbol": "AAPL",
      "ts_utc": "2026-07-22T14:30:00.123456Z",
      "bid_price": 223.94,
      "ask_price": 223.96,
      "bid_size": 3,
      "ask_size": 2,
      "bid_exchange": "Q",
      "ask_exchange": "P",
      "conditions": ["R"],
      "tape": "C",
      "source": "cloud.alpaca.market_data",
      "feed": "sip"
    }
  ]
}
```

### 9.5 `GET /v1/market-data/news`

| 参数 | 必填 | 规则 |
|---|---|---|
| `start` | 是 | UTC 开始时间 |
| `end` | 是 | UTC 结束时间，必须晚于 `start` |

```http
GET /v1/market-data/news?start=2026-07-22T00%3A00%3A00Z&end=2026-07-23T00%3A00%3A00Z HTTP/1.1
Authorization: Bearer <MARKET_DATA_TOKEN>
```

```json
{
  "api_version": "v1",
  "news": [
    {
      "id": 12345678,
      "headline": "Example headline",
      "summary": "Example summary",
      "author": "Example Newswire",
      "created_at": "2026-07-22T12:00:00Z",
      "updated_at": "2026-07-22T12:00:00Z",
      "symbols": ["AAPL"],
      "source": "example",
      "url": "https://example.invalid/article"
    }
  ]
}
```

新闻由上游提供元数据，平台明确请求 `include_content=false`，不提供全文代理。字段可能随上游新闻元数据增加；客户端应忽略未知字段。

## 10. Alpaca Paper

> 当前 v1 Paper 接口是整个云端 Paper 账户级，不含 `strategy_id` 路径，也没有策略子账本。只能授予 AI 执行/对账服务，不能授予普通同事。目标版本将以策略子账本、归因和风险额度替代这些兼容路由。

硬性安全规则：

- 固定访问 `https://paper-api.alpaca.markets`；
- 没有 Live base URL 配置；
- 入场只能 `buy`；平仓只能 `sell`；不提供 short；
- 入场必须是带止盈、止损的 bracket order；
- 只允许 `day`；不允许 extended hours；
- 写操作同时要求 `paper:write` token 和 `PAPER_BROKER_WRITE_ENABLED=true`；
- 写开关默认关闭；
- 下单由 `client_order_id` 保证幂等，不能每次重试生成新 ID。

### 10.1 `GET /v1/paper/account`

所需 scope：`paper:read` 或 `paper:write`。

```json
{
  "api_version": "v1",
  "account": {
    "status": "ACTIVE",
    "account_blocked": false,
    "trading_blocked": false,
    "equity": "100125.42",
    "last_equity": "100000",
    "buying_power": "200250.84"
  }
}
```

### 10.2 `GET /v1/paper/positions`

所需 scope：`paper:read` 或 `paper:write`。

```json
{
  "api_version": "v1",
  "positions": [
    {
      "symbol": "AAPL",
      "qty": "10",
      "side": "long",
      "market_value": "2243.10"
    }
  ]
}
```

### 10.3 `GET /v1/paper/orders/open`

返回最多 500 个未完成订单，并标准化为固定的六字段模型。上游 bracket 子单的额外字段不会透传。

```json
{
  "api_version": "v1",
  "orders": [
    {
      "id": "bb6d-example-order-id",
      "client_order_id": "alpha-20260722-aapl-entry-001",
      "symbol": "AAPL",
      "qty": 10,
      "filled_qty": "0",
      "status": "new"
    }
  ]
}
```

### 10.4 `GET /v1/paper/orders/by-client-id`

查询参数 `client_order_id` 必填。未找到时仍返回 200：

```json
{"api_version":"v1","order":null}
```

### 10.5 `POST /v1/paper/orders` — 入场

所需 scope：`paper:write`；服务端写开关必须开启。

市价入场：

```json
{
  "kind": "entry",
  "request": {
    "client_order_id": "alpha-20260722-aapl-entry-001",
    "symbol": "AAPL",
    "qty": 10,
    "side": "buy",
    "order_type": "market",
    "time_in_force": "day",
    "extended_hours": false,
    "take_profit_price": "231.00",
    "stop_loss_price": "219.00"
  }
}
```

限价入场把 `order_type` 改为 `limit`，并且必须提供 `limit_price`：

```json
{
  "kind": "entry",
  "request": {
    "client_order_id": "alpha-20260722-aapl-entry-002",
    "symbol": "AAPL",
    "qty": 10,
    "side": "buy",
    "order_type": "limit",
    "time_in_force": "day",
    "extended_hours": false,
    "limit_price": "223.50",
    "take_profit_price": "231.00",
    "stop_loss_price": "219.00"
  }
}
```

市场单携带非空 `limit_price` 或限价单缺少非空 `limit_price` 都返回 400；市场单可以省略该字段或传 `null`。任何额外字段也会被拒绝，不能借此绕过约束向 Alpaca 透传参数。

### 10.6 `POST /v1/paper/orders` — 平仓

```json
{
  "kind": "close",
  "request": {
    "client_order_id": "alpha-20260722-aapl-close-001",
    "symbol": "AAPL",
    "qty": 10,
    "side": "sell",
    "order_type": "market",
    "time_in_force": "day",
    "extended_hours": false
  }
}
```

成功响应：

```json
{
  "api_version": "v1",
  "order": {
    "id": "bb6d-example-order-id",
    "client_order_id": "alpha-20260722-aapl-close-001",
    "symbol": "AAPL",
    "qty": 10,
    "filled_qty": "0",
    "status": "accepted"
  }
}
```

服务端会先按 `client_order_id` 查询已有订单。存在时直接返回原订单；Alpaca 返回重复 ID 错误时也会再次查询，因此网络失败后的安全重试必须复用完全相同的 ID 和业务意图。

### 10.7 `DELETE /v1/paper/orders/cancel/{order_id}`

所需 scope：`paper:write`；服务端写开关必须开启。

```http
DELETE /v1/paper/orders/cancel/bb6d-example-order-id HTTP/1.1
Authorization: Bearer <PAPER_WRITE_TOKEN>
```

```json
{"api_version":"v1","cancelled":true}
```

上游返回未找到时为 `cancelled:false`。`true` 表示上游接受了撤单请求，不等于已完成最终状态对账；客户端仍应查询订单状态。

## 11. 调用示例

### 11.1 PowerShell

不要把 token 直接写入命令历史，示例从进程环境读取：

```powershell
$headers = @{ Authorization = "Bearer $env:CLOUD_FEATURE_API_TOKEN" }
$asof = [uri]::EscapeDataString("2026-07-22T15:30:00Z")
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8765/v1/features/AAPL?asof=$asof" `
  -Headers $headers
```

### 11.2 Python

```python
from datetime import datetime, timezone
import os

import httpx

base_url = os.environ["CLOUD_PLATFORM_BASE_URL"].rstrip("/")
token = os.environ["CLOUD_FEATURE_API_TOKEN"]

with httpx.Client(base_url=base_url, timeout=20.0) as client:
    response = client.get(
        "/v1/features/AAPL",
        params={"asof": datetime.now(timezone.utc).isoformat()},
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    feature_vector = response.json()["feature_vector"]
```

## 12. 重试、游标和兼容性

- 健康、特征、信号和查询接口可在 `502/503` 时使用带抖动的指数退避；
- `400/401/403/404/405` 不应自动重试；
- Paper POST 只允许使用同一 `client_order_id`、同一 payload 重试；
- Paper DELETE 可重试，但随后必须查询最终订单状态；
- SIP events 的 `after` 是排他游标，成功处理一批后保存 `next_sequence`；
- 客户端应忽略响应中的未知字段，但不能依赖未写入 OpenAPI 的字段；
- 破坏性变更进入新主版本路径；v1 内只允许向后兼容的可选字段扩展。

## 13. 部署和运维要求

本地启动：

```powershell
.\.venv\Scripts\python -m scripts.serve_api --host 127.0.0.1 --port 8765
```

| 云端环境变量 | 用途 | 是否允许下发给 AI/同事 |
|---|---|---|
| `ALPACA_API_KEY_ID` | 云端 Alpaca 身份 | 否 |
| `ALPACA_API_SECRET_KEY` | 云端 Alpaca 密钥 | 否 |
| `PAPER_BROKER_WRITE_ENABLED` | Paper 写操作总闸；默认 `false` | 否，由管理员控制 |

生产要求：

- Python HTTP server 只监听私网或 loopback；
- 前置 TLS 反向代理并设置请求超时和速率限制；
- token 只存入 secret manager；
- 分别签发 features、market-data、paper token，不能共用；
- 同事只签发策略绑定的 `signals:read`；
- 禁止在代理层增加能绕过应用认证的 Alpaca 转发规则；
- 禁止把 `.env`、SQLite 运行库或真实密钥提交 Git。

## 14. 导入 OpenAPI

`docs/openapi-v1.json` 是 OpenAPI 3.1 文件，可直接导入支持 OpenAPI 的 API 客户端、测试工具或文档渲染器。导入后需要自行设置 Bearer token；文件内不含任何真实 secret。

建议将 `x-required-scope` 作为内部权限检查和自动化契约测试的来源。OpenAPI 中只列出当前可用路由；目标设计接口不会混入该文件。

## 15. 已知限制和下一步

当前 v1 的真实能力边界如下：

1. Paper 仍是账户级兼容接口，没有 `strategy_id` 子账本、订单归因和每策略额度；
2. 策略注册、版本、激活目前通过本地脚本，不是管理 API；
3. 选股、回测、模拟盘、自动复盘和产物查询没有 HTTP API；
4. Python 沙箱已有基础设施约束，但没有包上传、审计、任务状态和日志接口；
5. token 没有公开的轮换、吊销、到期、最后使用时间和审计接口；
6. 列表接口除 SIP events 外没有分页契约；
7. 暂无请求 ID、限流响应头、异步 job 和 webhook 契约。

这些能力的完整目标合同、状态模型和兼容迁移路径见 [PLATFORM_API_TARGET.md](PLATFORM_API_TARGET.md)。
