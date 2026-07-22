# 云端多策略平台目标 API 合同

> **设计稿 / 尚未实现 / 不可调用**
>
> 本文定义完整平台的目标 HTTP 合同，用来指导后续开发、评审和验收。当前可调用接口只以 [API.md](API.md) 和 [openapi-v1.json](openapi-v1.json) 为准。

设计版本：`2026-07-22`

目标路径版本：`/v2`

稳定性：Draft

## 1. 设计目标

目标平台在不破坏现有单策略选股和本地观察任务的前提下，实现：

- 云端只维持一条 Alpaca SIP WebSocket，并统一落原始事件和时点特征；
- 所有策略配置、选股、回测、模拟盘、复盘、产物和任务按 `strategy_id` 隔离；
- 参数化策略和安全表达式优先；
- Python 自定义策略只能在网络关闭、只读根文件系统、非 root、限时限资源的 digest-pinned 容器中运行；
- 同事只能读取指定策略的派生信号；
- AI 服务通过独立 scope 读取标准化行情/特征及执行受控 Paper 委托；
- 不提供 Alpaca 凭据、原始代理、Live 交易或做空能力。

## 2. 兼容原则

1. 当前 `/v1` 保留，原单策略选股和本地观察任务继续工作；
2. `/v2` 新能力使用新路径，不静默改变 `/v1` JSON 结构；
3. 默认单策略映射为 `strategy_id=default`，迁移只复制元数据，不移动或覆盖旧产物；
4. `/v1/paper/*` 在策略子账本完成后进入弃用期，但不会直接改成伪策略隔离；
5. `/v2` 的所有策略资源在数据库主键、对象存储 URI、队列消息和审计事件中都包含 `strategy_id`；
6. 不允许客户端传入对象存储绝对路径、数据库路径、任意上游 URL 或容器启动参数。

## 3. 身份、角色和 Scope

| Scope | 主体 | 目标能力 |
|---|---|---|
| `signals:read` | 同事 | 只读 token 绑定策略的派生信号 |
| `features:read` | AI/研究服务 | 查询共享特征目录和时点向量 |
| `market-data:read` | AI 行情服务 | 查询标准化事件、bars、quotes、news |
| `strategies:read` | 研究员 | 查看被授权策略和版本 |
| `strategies:write` | 策略负责人 | 创建版本、校验、激活和归档 |
| `research:run` | 研究员 | 发起选股、回测和复盘任务 |
| `paper:read` | AI/策略负责人 | 查看被授权策略的模拟账本 |
| `paper:write` | AI 执行服务 | 在被授权策略和风险预算内提交模拟指令 |
| `python:submit` | 高风险受控角色 | 上传 Python 包和发起隔离执行 |
| `artifacts:read` | 研究员/审计员 | 下载被授权策略的派生产物 |
| `audit:read` | 审计员 | 查询审计事件，不读取 secret |
| `admin:tokens` | 平台管理员 | 签发、轮换和吊销平台 token |

授权必须同时满足 scope 和资源策略。`strategies:read` 不是读取全部策略的全局通行证；主体还要在该 `strategy_id` 的 ACL 中。

## 4. 通用 HTTP 合同

### 4.1 请求头

| Header | 适用 | 规则 |
|---|---|---|
| `Authorization: Bearer …` | 除健康检查外 | 必填 |
| `Content-Type: application/json` | JSON body | 必填 |
| `Idempotency-Key` | 创建任务和写订单 | 1..128 字符；同主体、同资源、同 payload 幂等 |
| `If-Match` | 更新策略配置 | 使用资源 `etag`，防止覆盖并发修改 |
| `X-Request-Id` | 可选 | 客户端可提供；服务端验证格式并回显 |

### 4.2 通用响应头

- `Cache-Control: no-store`
- `X-Request-Id: <request_id>`
- `ETag: <etag>`：适用于可更新资源
- `Retry-After`：适用于 `429`、部分 `503`

### 4.3 成功信封

单资源：

```json
{
  "api_version": "v2",
  "request_id": "req_01J...",
  "data": {}
}
```

列表：

```json
{
  "api_version": "v2",
  "request_id": "req_01J...",
  "data": [],
  "page": {
    "next_cursor": "opaque-or-null",
    "has_more": false
  }
}
```

### 4.4 错误信封

```json
{
  "api_version": "v2",
  "request_id": "req_01J...",
  "error": {
    "code": "validation_failed",
    "message": "request validation failed",
    "details": [
      {"field": "parameters.lookback", "reason": "must be between 2 and 252"}
    ],
    "retryable": false
  }
}
```

| HTTP | 目标错误码 | 含义 |
|---:|---|---|
| 400 | `invalid_request` | JSON、时间、游标或参数格式错误 |
| 401 | `unauthorized` | token 不可用 |
| 403 | `forbidden`、`writes_disabled` | scope/资源授权不足或写总闸关闭 |
| 404 | `not_found` | 资源不存在；跨策略读取也返回 404 防枚举 |
| 409 | `version_conflict`、`idempotency_conflict` | ETag 冲突或幂等键复用不同 payload |
| 422 | `validation_failed`、`risk_rejected` | 策略校验或风险规则拒绝 |
| 429 | `rate_limited` | 速率或并发额度超限 |
| 502 | `upstream_failed` | 上游失败且响应已脱敏 |
| 503 | `temporarily_unavailable` | 依赖或调度器暂不可用 |

所有时间为 UTC ISO 8601；所有金额和价格为十进制字符串；所有 cursor 不透明，客户端不得解析。

## 5. 资源和状态模型

### 5.1 Strategy

```json
{
  "strategy_id": "gap_momentum",
  "display_name": "Gap Momentum",
  "description": "Long-only opening gap strategy",
  "owner_principal_id": "research-frank",
  "status": "active",
  "active_version": "2026.07.22.1",
  "created_at_utc": "2026-07-22T08:00:00Z",
  "updated_at_utc": "2026-07-22T08:30:00Z",
  "etag": "sha256:..."
}
```

`status`: `draft | active | paused | archived`。`strategy_id` 创建后不可修改，格式为 `^[a-z][a-z0-9_-]{0,63}$`。

### 5.2 StrategyVersion

```json
{
  "strategy_id": "gap_momentum",
  "version": "2026.07.22.1",
  "kind": "safe_expression",
  "symbols": ["AAPL", "MSFT"],
  "parameters": {
    "min_gap_pct": 0.02,
    "min_relative_volume": 1.5
  },
  "expression": "gap_pct >= min_gap_pct and relative_volume >= min_relative_volume",
  "python_package_digest": null,
  "created_by": "research-frank",
  "created_at_utc": "2026-07-22T08:10:00Z",
  "validation_status": "passed",
  "immutable": true
}
```

`kind`: `parameterized | safe_expression | python_sandbox`。版本一经创建不可修改；修改等于创建新版本。

### 5.3 Job

异步选股、回测、复盘和 Python 验证统一返回 Job：

```json
{
  "job_id": "job_01J...",
  "strategy_id": "gap_momentum",
  "job_type": "backtest",
  "status": "queued",
  "progress": {"completed": 0, "total": 100, "unit": "trading_days"},
  "submitted_at_utc": "2026-07-22T08:40:00Z",
  "started_at_utc": null,
  "finished_at_utc": null,
  "failure": null
}
```

状态只允许：`queued -> running -> succeeded | failed | cancelled`。终态不可逆。

### 5.4 Artifact

```json
{
  "artifact_id": "art_01J...",
  "strategy_id": "gap_momentum",
  "strategy_version": "2026.07.22.1",
  "stage": "backtest",
  "media_type": "application/json",
  "size_bytes": 18240,
  "content_sha256": "...64位小写sha256...",
  "created_at_utc": "2026-07-22T09:00:00Z",
  "download_url": "/v2/strategies/gap_momentum/artifacts/art_01J.../content"
}
```

内部 URI 必须为 `strategy://{strategy_id}/...`，绝不把存储凭据或真实文件路径返回客户端。

## 6. 目标路由总览

### 6.1 健康和平台状态

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/health` | 无 | 进程健康 |
| GET | `/ready` | 无/内网 | 数据库、队列和存储就绪，不探测交易写能力 |
| GET | `/v2/platform/capabilities` | 已认证 | 返回启用的只读能力和写总闸状态，不返回配置值 |

### 6.2 策略和版本

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v2/strategies` | `strategies:read` | 列出主体有权访问的策略 |
| POST | `/v2/strategies` | `strategies:write` | 创建策略壳，不激活 |
| GET | `/v2/strategies/{strategy_id}` | `strategies:read` | 读取策略摘要 |
| PATCH | `/v2/strategies/{strategy_id}` | `strategies:write` | 修改名称、说明或状态；要求 `If-Match` |
| GET | `/v2/strategies/{strategy_id}/versions` | `strategies:read` | 列出不可变版本 |
| POST | `/v2/strategies/{strategy_id}/versions` | `strategies:write` | 创建不可变版本 |
| GET | `/v2/strategies/{strategy_id}/versions/{version}` | `strategies:read` | 读取版本 |
| POST | `/v2/strategies/{strategy_id}/versions/{version}:validate` | `strategies:write` | 运行静态/沙箱校验 |
| POST | `/v2/strategies/{strategy_id}/versions/{version}:activate` | `strategies:write` | 原子切换活动版本 |

创建策略：

```json
{
  "strategy_id": "gap_momentum",
  "display_name": "Gap Momentum",
  "description": "Long-only opening gap strategy"
}
```

创建版本时，参数化/表达式策略必须提供 `expression` 且不能提供 Python；Python 策略只能引用已扫描通过的 `python_package_digest` 和 allowlisted entrypoint，不能提交 shell command。

激活前必须满足：版本校验通过、特征依赖可用、没有 short 动作、回测与风险门槛满足平台策略。激活操作写入审计事件。

### 6.3 特征目录和共享数据

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v2/features/catalog` | `features:read` | 特征名、定义版本、类型、可用区间、provenance |
| GET | `/v2/features/{symbol}` | `features:read` | 单标的时点向量 |
| POST | `/v2/features:batch-get` | `features:read` | 有上限的批量时点读取 |
| GET | `/v2/market-data/events` | `market-data:read` | 共享事件游标读取 |
| GET | `/v2/market-data/bars` | `market-data:read` | 标准化 SIP bars |
| GET | `/v2/market-data/quotes` | `market-data:read` | 标准化 SIP quotes |
| GET | `/v2/market-data/news` | `market-data:read` | 新闻元数据，不含全文代理 |

批量特征请求：

```json
{
  "items": [
    {"symbol": "AAPL", "asof_utc": "2026-07-22T15:30:00Z"},
    {"symbol": "MSFT", "asof_utc": "2026-07-22T15:30:00Z"}
  ],
  "feature_names": ["close", "minute_return", "nbbo_spread_bps"]
}
```

每个特征值都必须包含 `asof_utc`、`definition_version`、`provenance` 和 `input_event_digest`；缺失值返回明确的 `null` 和 `availability_reason`，禁止插值冒充事实。

### 6.4 选股任务

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| POST | `/v2/strategies/{strategy_id}/selection-runs` | `research:run` | 发起选股 |
| GET | `/v2/strategies/{strategy_id}/selection-runs` | `strategies:read` | 列出选股任务 |
| GET | `/v2/strategies/{strategy_id}/selection-runs/{run_id}` | `strategies:read` | 状态和输入快照 |
| GET | `/v2/strategies/{strategy_id}/selection-runs/{run_id}/results` | `strategies:read` | 分页读取结果 |
| POST | `/v2/strategies/{strategy_id}/selection-runs/{run_id}:cancel` | `research:run` | 取消未完成任务 |

```json
{
  "strategy_version": "2026.07.22.1",
  "asof_utc": "2026-07-22T15:30:00Z",
  "universe": {"kind": "symbols", "symbols": ["AAPL", "MSFT"]},
  "mode": "point_in_time"
}
```

结果每行包含 `strategy_id`、版本、symbol、rank、decision、reason、feature snapshot digest 和数据缺失说明。相同 `Idempotency-Key` 不重复创建任务。

### 6.5 回测

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| POST | `/v2/strategies/{strategy_id}/backtests` | `research:run` | 发起 point-in-time 回测 |
| GET | `/v2/strategies/{strategy_id}/backtests` | `strategies:read` | 列出回测 |
| GET | `/v2/strategies/{strategy_id}/backtests/{backtest_id}` | `strategies:read` | 配置、状态和摘要 |
| GET | `/v2/strategies/{strategy_id}/backtests/{backtest_id}/metrics` | `strategies:read` | 绩效与风险指标 |
| GET | `/v2/strategies/{strategy_id}/backtests/{backtest_id}/equity` | `strategies:read` | 分页权益曲线 |
| GET | `/v2/strategies/{strategy_id}/backtests/{backtest_id}/trades` | `strategies:read` | 分页交易和归因 |
| POST | `/v2/strategies/{strategy_id}/backtests/{backtest_id}:cancel` | `research:run` | 取消 |

```json
{
  "strategy_version": "2026.07.22.1",
  "start_date": "2025-01-01",
  "end_date": "2026-06-30",
  "initial_cash": "100000.00",
  "commission_model": "us_equity_zero_commission_v1",
  "slippage_model": "sip_spread_v1",
  "benchmark": "SPY",
  "data_policy": "point_in_time_only"
}
```

指标至少包括收益、年化波动、最大回撤、Sharpe、Sortino、胜率、换手率、暴露和数据缺失率，并附计算定义版本。禁止未来数据和静默填补。

### 6.6 策略模拟盘子账本

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| POST | `/v2/strategies/{strategy_id}/paper-sessions` | `paper:write` | 创建独立模拟会话/预算 |
| GET | `/v2/strategies/{strategy_id}/paper-sessions` | `paper:read` | 列出会话 |
| GET | `/v2/strategies/{strategy_id}/paper-sessions/{session_id}` | `paper:read` | 会话净值和风险状态 |
| GET | `/v2/strategies/{strategy_id}/paper-sessions/{session_id}/positions` | `paper:read` | 策略归因持仓 |
| GET | `/v2/strategies/{strategy_id}/paper-sessions/{session_id}/orders` | `paper:read` | 策略归因订单 |
| POST | `/v2/strategies/{strategy_id}/paper-sessions/{session_id}/orders` | `paper:write` | 提交受控只做多委托 |
| DELETE | `/v2/strategies/{strategy_id}/paper-sessions/{session_id}/orders/{order_id}` | `paper:write` | 撤单 |
| GET | `/v2/strategies/{strategy_id}/paper-sessions/{session_id}/ledger` | `paper:read` | 不可变策略子账本 |
| POST | `/v2/strategies/{strategy_id}/paper-sessions/{session_id}:pause` | `paper:write` | 停止新单 |

创建会话：

```json
{
  "strategy_version": "2026.07.22.1",
  "initial_cash": "25000.00",
  "risk_limits": {
    "max_gross_exposure": "25000.00",
    "max_position_value": "5000.00",
    "max_daily_loss": "500.00",
    "max_open_positions": 10
  }
}
```

目标订单必须携带 `signal_id`、`client_order_id`、symbol、qty、受限 entry/close schema。服务端先检查策略、版本、会话、信号、预算、持仓和永久只做多规则，再映射到云端共用 Alpaca Paper 账户。平台子账本按成交事件做确定性归因；无法唯一归因时进入 `reconciliation_required`，不得猜测。

### 6.7 派生信号

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v2/strategies/{strategy_id}/signals` | `signals:read` | 分页读取策略绑定信号 |
| GET | `/v2/strategies/{strategy_id}/signals/{signal_id}` | `signals:read` | 读取单个信号 |
| GET | `/v2/strategies/{strategy_id}/signal-stream` | `signals:read` | 可选 SSE，只含派生信号 |

协作者合同只包含信号、理由、策略版本、特征 provenance 和过期时间。不得通过字段扩展加入 bar、quote、feature 原值、账户、订单、Key、代理 URL 或内部对象路径。

### 6.8 自动复盘

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| POST | `/v2/strategies/{strategy_id}/reviews` | `research:run` | 发起日/周/指定区间复盘 |
| GET | `/v2/strategies/{strategy_id}/reviews` | `strategies:read` | 列出复盘 |
| GET | `/v2/strategies/{strategy_id}/reviews/{review_id}` | `strategies:read` | 状态和结构化摘要 |
| GET | `/v2/strategies/{strategy_id}/reviews/{review_id}/findings` | `strategies:read` | 分页问题、证据和建议 |
| POST | `/v2/strategies/{strategy_id}/reviews/{review_id}:approve` | `strategies:write` | 人工确认复盘，不自动改策略 |

```json
{
  "review_type": "daily",
  "period_start_utc": "2026-07-22T00:00:00Z",
  "period_end_utc": "2026-07-23T00:00:00Z",
  "paper_session_id": "ps_01J...",
  "compare_to_backtest_id": "bt_01J..."
}
```

复盘结论必须引用证据 artifact/digest。复盘永远不能直接修改活动策略、打开 Paper 写闸或转成 Live 交易。

### 6.9 Python 隔离执行

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| POST | `/v2/python-packages` | `python:submit` | 上传受限源码包；大小和文件类型有限制 |
| GET | `/v2/python-packages/{digest}` | `python:submit` | 扫描和审批状态 |
| POST | `/v2/python-packages/{digest}:validate` | `python:submit` | 发起隔离校验 |
| GET | `/v2/strategies/{strategy_id}/sandbox-runs/{run_id}` | `python:submit` | 读取执行状态、资源统计和脱敏日志 |
| POST | `/v2/strategies/{strategy_id}/sandbox-runs/{run_id}:cancel` | `python:submit` | 终止执行 |

包使用 `multipart/form-data` 上传后按内容 SHA-256 标识。服务端拒绝二进制、符号链接、路径穿越、过大解压比和未锁定依赖。执行环境必须：

- 镜像使用 digest 固定，不接受客户端镜像名；
- `network=none`；
- 非 root、只读根文件系统、临时目录限额；
- 仅挂载该策略的只读输入和独立可写输出；
- CPU、内存、进程、文件、输出和 wall-clock 全部设上限；
- 禁止 Docker socket、宿主凭据、Alpaca Key 和其他策略目录；
- 日志做 secret/路径脱敏并设置行数和字节上限；
- 输出先校验 schema、strategy_id 和 digest，再进入 artifact store。

### 6.10 产物、任务和审计

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| GET | `/v2/strategies/{strategy_id}/artifacts` | `artifacts:read` | 按 stage、日期、版本分页查询 |
| GET | `/v2/strategies/{strategy_id}/artifacts/{artifact_id}` | `artifacts:read` | 元数据 |
| GET | `/v2/strategies/{strategy_id}/artifacts/{artifact_id}/content` | `artifacts:read` | 校验授权后流式下载 |
| GET | `/v2/jobs/{job_id}` | 对应资源 scope | 读取异步任务 |
| POST | `/v2/jobs/{job_id}:cancel` | 对应写 scope | 取消尚未结束的任务 |
| GET | `/v2/audit-events` | `audit:read` | 按主体、策略、动作和时间查询 |

审计事件至少包含 request_id、principal_id、token_id（不是 token）、scope、strategy_id、动作、资源 ID、结果、UTC 时间和变更摘要 digest。禁止记录 Authorization、Alpaca header、完整下单 secret 或 Python 环境变量。

### 6.11 Token 管理

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| POST | `/v2/admin/tokens` | `admin:tokens` | 签发；secret 只返回一次 |
| GET | `/v2/admin/tokens` | `admin:tokens` | 只列 token_id、主体、scope、绑定策略、到期和状态 |
| POST | `/v2/admin/tokens/{token_id}:rotate` | `admin:tokens` | 轮换并设置短暂重叠期 |
| POST | `/v2/admin/tokens/{token_id}:revoke` | `admin:tokens` | 立即吊销 |

```json
{
  "principal_id": "colleague-alice",
  "scopes": ["signals:read"],
  "strategy_ids": ["gap_momentum"],
  "expires_at_utc": "2026-10-22T00:00:00Z",
  "description": "read-only colleague access"
}
```

`signals:read` 不能与 Paper、行情、特征或管理 scope 合并到同一同事 token。响应只在创建/轮换时出现一次 `token_secret`，以后接口永远不返回。

## 7. 速率、配额和并发

目标服务应按 principal、scope 和策略设置独立配额：

- 同事信号读取不影响 AI 行情摄取；
- 历史行情大窗口通过独立并发池，不阻塞实时 SIP owner；
- 每策略同时只允许有限个选股、回测、复盘和沙箱任务；
- Paper 写入使用最严格额度和全局 kill switch；
- 超额返回 429 和 `Retry-After`，不能退化为绕过缓存直连 Alpaca。

具体数值由部署配置决定，通过 `/v2/platform/capabilities` 仅公布非敏感上限。

## 8. Webhook（可选后续阶段）

如果引入 webhook，只允许发送派生任务状态和派生信号：

| 方法 | 路径 | Scope | 说明 |
|---|---|---|---|
| POST | `/v2/webhook-subscriptions` | `strategies:write` | 创建策略绑定订阅 |
| GET | `/v2/webhook-subscriptions` | `strategies:read` | 列出订阅 |
| DELETE | `/v2/webhook-subscriptions/{subscription_id}` | `strategies:write` | 删除订阅 |

必须使用 HTTPS、出站域名 allowlist、签名、时间戳和重放保护。不得允许私网/loopback/metadata 地址，防止 SSRF；payload 不得包含原始 SIP 或 secret。

## 9. 分阶段实现和验收

| 阶段 | 能力 | 关键验收 |
|---|---|---|
| A | 策略/版本管理 API、ACL、审计、token 生命周期 | 跨策略读写全拒绝；旧单策略测试全通过 |
| B | 选股/回测异步任务和 artifacts | 每个表、URI、队列消息含 strategy_id；时点数据无泄漏 |
| C | Paper 子账本、归因和每策略风险 | 与 Alpaca 对账可重放；未知归因不猜测；永久只做多 |
| D | 自动复盘 | 结论可追溯；不自动改配置或下单 |
| E | Python 包和沙箱 API | 无网络、无 key、跨策略读取测试、资源限制和超时测试通过 |
| F | webhook/SSE 和运维配额 | SSRF、重放、泄密、限流和断线恢复测试通过 |

每阶段都必须运行全量 pytest、Ruff、mypy、安全边界测试和兼容测试；任何阶段失败不得通过删除旧测试来“修复”。

## 10. 完成定义

只有同时满足以下条件，才能称为“完整云端多策略接口已开发”：

1. 上述路由有实现代码、OpenAPI 3.1、自动化契约测试和权限测试；
2. 所有策略资源在存储和 API 层均按 `strategy_id` 硬隔离；
3. v1 单策略选股和本地观察任务全量回归通过；
4. AI 仓库不含 Alpaca Key，并通过独立 scope 调用云端；
5. 同事 token 的可达路由自动证明仅有派生信号；
6. Paper 子账本与 Alpaca 账户可重放对账，写闸默认关闭，Live/short 路径不存在；
7. Python 沙箱通过网络、凭据、文件、进程、资源和跨策略逃逸测试；
8. 公开仓库不包含 secret、运行数据库、原始行情、账户数据或同事 token。
