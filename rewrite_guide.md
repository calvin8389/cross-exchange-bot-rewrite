# Rewrite Guide (Python + asyncio)

> This document consolidates the rewrite guidance into a single, beginner-friendly place.
>
> Included:
> - Spec-style guide (what to build)
> - Blueprint-style guide (how to structure code)
> - Engineering guide (WS background services + SQLite audit store)
> - Lighter WebSocket protocol notes (order_book + ticker + user_stats)
> - Lessons from old code (patterns to preserve / patterns to avoid)
> - Appendix: M0/M1 minimal runnable demo (copy-paste)
>
---

## Part A — Spec 风格：重写指南（需求规格/架构说明）

### 1. 范围与非目标
#### 1.1 范围（必须覆盖）
- 两交易所（EdgeX、Lighter）之间对同一标的建立 delta-neutral 头寸（多/空对冲）。
- 机器人轮换：扫描机会 → 开仓 → 持有监控 → 平仓 → 冷却 → 下一轮。
- 筛选门槛：funding 可用、成交量门槛、价差门槛、净 APR 门槛。
- 风险控制：开仓单腿失败回滚；持仓期间止损触发平仓；平仓不完全检测并升级为 ERROR。
- 状态持久化：重启后可恢复单一对冲仓位；发现多仓/不对冲时进入安全模式或 ERROR（策略可配置）。

#### 1.2 非目标（可后续再做）
- 多策略并行、跨多账户、跨多交易所扩展。
- 完整的 GUI/监控平台。
- 收益统计严谨会计（本文只要求“能记录关键数据并可审计”）。

### 2. 系统目标与质量属性（工程约束）
#### 2.1 可运行性（Deployability）
- 在 Linux/macOS + Docker 中可一键运行（推荐 Docker）。
- 启动时进行配置校验（env & config file），错误应 fail-fast。
- 每个外部资源（HTTP client/WS/SDK client）必须有明确生命周期管理（可 close、可重连）。

#### 2.2 正确性（Correctness，指逻辑闭环）
- 开仓成功定义：两边仓位方向相反，且绝对数量在容忍误差内。
- 平仓成功定义：两边仓位均归零（或低于最小 tick/step）。
- 回滚必须覆盖：下单 API 报错、下单成功但未成交/未建仓（需要仓位确认超时机制）。

#### 2.3 鲁棒性（Resilience）
- 对 WS 超时、HTTP 429、暂时网络故障要有：
  - 退避重试（exponential backoff）
  - 降级策略（延迟下一轮、切换数据源、扩大下单跨价等）
- 不允许在未知仓位状态下继续开新仓（必须先 ensure flat 或进入安全态）。
- **StaleDataError 防护**：WS 服务的 getter（`get_balance`、`get_best_bid_ask`）在数据超过 `max_age_seconds` 时会抛出 `StaleDataError`。所有调用方**必须 catch 此异常**并降级处理，不得让异常传播导致进程退出。
  - **sleep 间隔约束**：主循环的 `asyncio.sleep` 应**显著小于** `max_age_seconds`（建议取 `max_age` 的 1/2 到 1/3，但不超过 5s 上限以控制写库/日志压力）。例如：ticker `max_age=3s` → sleep 1s；userstats `max_age=10s` → sleep 5s。
  - **降级优先级（区分阶段）**：
    - **Scanner/快照阶段**：stale → 记录 `STALE_DATA` warn event → continue 跳过本轮 → 若**连续 N 次** stale（建议 N=3），触发对应 WS service 重启/重连
    - **OPENING/CLOSING 执行阶段**：stale → **不允许继续下单**；应进入短暂 WAIT/RETRY 或触发 service 重连，直到数据恢复或超时进入 ERROR。禁止用过期价格执行交易

#### 2.4 可维护性（Maintainability）
- 策略与交易所适配层解耦（对称接口）。
- 所有核心步骤结构化日志（JSON 或 key-value），可重放关键决策链。

### 3. 配置规范
#### 3.1 环境变量（env）
必须项（缺失则启动失败）：
- EdgeX：EDGEX_BASE_URL, EDGEX_ACCOUNT_ID(int), EDGEX_STARK_PRIVATE_KEY
- Lighter：LIGHTER_BASE_URL, LIGHTER_WS_URL, API_KEY_PRIVATE_KEY, ACCOUNT_INDEX(int), API_KEY_INDEX(int)

可选项：
- EDGEX_WS_URL（若未来接入）

#### 3.2 配置文件（bot_config.json）
必须字段：
- symbols_to_monitor: [str]
- quote: "USD"
- leverage: int
- notional_per_position: float
- hold_duration_hours: float
- wait_between_cycles_minutes: float
- check_interval_seconds: int
- min_net_apr_threshold: float
- min_volume_usd: float
- max_spread_pct: float
- cross_pct: float（统一管理侵入式限价跨价百分比）
- enable_stop_loss: bool

建议字段：
- min_open_notional_usd: float（替代硬编码 10.0）
- order_confirm_timeout_seconds: int
- close_confirm_timeout_seconds: int
- max_recoverable_symbols: int（默认 1）

### 4. 核心业务流程（验收口径）
#### 4.1 启动流程
1) 读取 env & config（校验）
2) 初始化日志系统
3) 加载 state（SQLite，见 Part C）
4) 执行 recover：
   - 若发现可恢复的单一对冲仓位 → 进入 HOLDING
   - 若发现非对冲/多仓 → 进入 SAFE/ERROR（按配置）
5) 启动前清理残留挂单（至少 Lighter）
6) 进入主循环

#### 4.2 扫描机会（ANALYZING）
对每个 symbol 生成 Opportunity：
- 数据项：edgex_rate, lighter_rate, edgex_apr, lighter_apr, net_apr, volume, spread
- 过滤规则：
  - funding 数据齐全
  - total_volume >= min_volume_usd
  - spread_pct <= max_spread_pct
- 决策规则：
  - long/short 方向由净 APR 更大的一侧决定
  - 候选按 net_apr 降序且 net_apr >= min_net_apr_threshold

#### 4.3 开仓（OPENING）
- 读取两边 available balance，用 min() 决定最大可开 notional（考虑 leverage）并打安全折扣。
- 计算 size_base：notional / mid
- 统一舍入：保证两边 size_base 一致或在容忍误差内
- 并发下单（两腿）
- 订单确认：
  - 在 confirm timeout 内必须观察到两边仓位建立（或订单成交回报）
  - 若一腿失败或确认超时 → 回滚已成功腿 + 记录失败原因

#### 4.4 持有监控（HOLDING）
- 每 check_interval_seconds：
  - 读取两边仓位与 uPnL（或止损指标）
  - 更新 state
  - 若触发 stop loss 或达到 target_close_at → 进入平仓

#### 4.5 平仓（CLOSING）
- 并发平仓两腿（reduce-only + 侵入式限价）
- 在 close confirm timeout 内必须确认两边仓位为零
- 若平仓不完全：
  - 执行补救策略（重新报价、撤单重发、扩大 cross_pct），超过上限进入 ERROR

#### 4.6 冷却等待（WAITING）
- 等待 wait_between_cycles_minutes
- 回到 IDLE（下一轮）

---

## Part B — Blueprint 风格：从 0 到可运行的“代码骨架索引”（给初学者）

这一节的目标：你按顺序创建文件、填入最小函数，就能先跑通 **M0/M1（DB + WS）**，再跑 **M2/M3（adapter + scanner）**。

### B.1 推荐最简目录结构（直观、可逐步扩展）

```text
src/
  main.py
  config.py
  logging_.py

  util/
    time.py
    retry.py

  db/
    schema.sql
    store.py

  services/
    lighter_userstats_service.py
    lighter_ticker_service.py
    lighter_orderbook_service.py   # 可选

  exchanges/
    edgex_adapter.py
    lighter_adapter.py

  core/
    models.py
    scanner.py
    sizing.py
    execution.py
    orchestrator.py
```

### B.2 每个文件“应该提供什么”（核心函数签名）

#### `src/config.py`
- `load_env() -> Env`
- `load_bot_config(path: str) -> BotConfig`

#### `src/db/store.py`
- `await start()` / `await close()`
- `await init_schema(schema_sql: str)`
- `await kv_set(key, value)` / `await kv_get(key)`
- `await append_event(Event(...))`

#### `src/services/lighter_userstats_service.py`
- `await start()` / `await stop()`
- `await wait_ready(timeout=...)`
- `get_balance(max_age_seconds=...) -> (available, portfolio)`

#### `src/services/lighter_ticker_service.py`
- `await start()` / `await stop()`
- `await subscribe(market_id)`
- `get_best_bid_ask(market_id, max_age_seconds=...) -> (bid, ask)`

#### `src/core/scanner.py`
- `scan_all(symbols) -> list[Opportunity]`

#### `src/core/execution.py`
- `open_position(opportunity) -> PositionState`
- `close_position(position) -> None`

#### `src/core/orchestrator.py`
- `run()`

#### `src/core/models.py`
- `BotState(StrEnum)` — IDLE / ANALYZING / OPENING / HOLDING / CLOSING / WAITING / ERROR / SHUTDOWN
- `Opportunity` (dataclass) — symbol, edgex_rate, lighter_rate, edgex_apr, lighter_apr, net_apr, volume, spread, direction (which side to long)
- `PositionState` (dataclass) — symbol, edgex_size, lighter_size, edgex_entry, lighter_entry, opened_at, cycle_id
- `CycleRecord` (dataclass) — cycle_id, symbol, opened_at, closed_at, edgex_pnl, lighter_pnl, net_pnl, status

#### `src/core/sizing.py`
- `calculate_position_size(available_edgex, available_lighter, leverage, mid_price, safety_factor=0.95) -> float` — `min(edgex_avail, lighter_avail) * leverage * safety_factor / mid_price`
- `unify_size_step(size, edgex_step, lighter_step) -> float` — 取较粗 step size（max of the two），对数量做 `floor` 保证两边仓位大小一致且不超过可用余额
- `unify_price_tick(price, edgex_tick, lighter_tick, side) -> float` — 取较粗 price tick，对 BUY 做 `ceil`（避免限价过低无法成交），对 SELL 做 `floor`（避免限价过高无法成交）

### B.3 初学者推荐的落地顺序
1) 先实现 Store + schema（M0）。**Store 必须同时提供 `kv_set()` 和 `kv_get()`**，后者是 M6 Recovery 的前置依赖。
2) 再实现 UserStatsService + TickerService（M1）。
3) main.py 同时启动 DB + WS 服务，每 10 秒写 events（证明链路稳定）。
   - **M1 验收关键**：main loop 必须 catch `StaleDataError` 并 continue，不能 crash。
   - `asyncio.sleep` 间隔建议设为 5s（远小于 `get_balance(max_age_seconds=10)` 和 `get_best_bid_ask(max_age_seconds=3)`），避免正常波动导致超时。
4) 再接 adapters + scanner。

---

## Part C — 工程指南：WS 常驻后台任务 + SQLite（SSOT + 审计）

### C.1 你选择的落地方案
- WS 服务（order book / ticker / user_stats）采用常驻后台任务 + 自动重连。
- 状态存储使用 SQLite（aiosqlite），支持审计与重启恢复。

### C.2 SQLite 表（最小集合，建议）
- bot_kv：全局状态（state、schema_version、current_cycle_id）
- cycles：每一轮开平仓闭环的业务记录
- positions：当前活跃仓位（is_active=1 仅一条）
- events：结构化事件流（JSON payload）
- account_snapshots：余额/可用快照

**DDL 参考（可按实现调整，非最终强制结构）：**

```sql
-- 每轮交易闭环记录
CREATE TABLE IF NOT EXISTS cycles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  state TEXT NOT NULL,              -- OPENING / HOLDING / CLOSING / CLOSED / ERROR
  direction TEXT NOT NULL,          -- long_edgex_short_lighter / short_edgex_long_lighter
  edgex_size REAL,
  lighter_size REAL,
  edgex_entry_price REAL,
  lighter_entry_price REAL,
  opened_at TEXT,
  closed_at TEXT,
  edgex_close_pnl REAL,
  lighter_close_pnl REAL,
  leverage INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 当前活跃仓位（最多一条 is_active=1）
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id INTEGER NOT NULL REFERENCES cycles(id),
  symbol TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,  -- 1 = open, 0 = closed
  edgex_contract_id TEXT,
  edgex_side TEXT,                        -- BUY / SELL
  edgex_size REAL,
  edgex_entry_price REAL,
  edgex_unrealized_pnl REAL,
  lighter_market_id INTEGER,
  lighter_side TEXT,                      -- BUY / SELL
  lighter_size REAL,
  lighter_entry_price REAL,
  lighter_unrealized_pnl REAL,
  stop_loss_price REAL,
  target_close_at TEXT,
  opened_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_positions_active ON positions(is_active);
-- 确保同时最多一条活跃仓位（SQLite 支持部分索引）
CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_one_active ON positions(is_active) WHERE is_active = 1;

-- 定期账户快照（余额/可用/投资组合值）
CREATE TABLE IF NOT EXISTS account_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  exchange TEXT NOT NULL,                -- edgex / lighter
  total_equity REAL,
  available_balance REAL,
  portfolio_value REAL,
  ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_exchange_ts ON account_snapshots(exchange, ts);
```

### C.3 WS 服务建议
- 优先使用 ticker 作为 best bid/ask 执行价源。
- order_book 用于深度过滤与诊断，采用 offset 序列校验 + gap 刷新。
- user_stats 用于余额/可用保证金，常驻订阅。

### C.4 Milestones（建议实现顺序）
- M0：SQLite schema + store + state/event 基础
- M1：UserStatsService + TickerService（可持续更新 + 自动重连）
- M2：Adapters（最小查询能力）
- M3：Scanner（输出候选、落库 events）
- M4：Open execution（并发下单 + confirm + rollback + 落库 positions/cycles）
- M5：Close execution（并发平仓 + confirm + 补救 + ERROR）
- M6：Recovery（重启恢复/安全阻断）

---

## Part D — Lighter WebSocket（订阅 / 字段 / 一致性 / 恢复）

### D.1 权威参考
- https://apidocs.lighter.xyz/docs/websocket-reference#order-book

### D.2 订阅与频道（已验证）
- order_book subscribe: `{"type":"subscribe","channel":"order_book/{MARKET_INDEX}"}`
  - push channel: `order_book:{MARKET_INDEX}`
- ticker subscribe: `{"type":"subscribe","channel":"ticker/{MARKET_INDEX}"}`
  - push channel: `ticker:{MARKET_INDEX}`
- user_stats subscribe: `{"type":"subscribe","channel":"user_stats/{ACCOUNT_INDEX}"}`

### D.3 字段解释（抓包样例）
#### update/order_book
- `order_book.bids/asks` 是增量列表，`size==0` 删除价位
- `offset` 用于 +1 序列校验

#### update/ticker
- `ticker.a` best ask
- `ticker.b` best bid

### D.4 一致性规则（建议）
- order_book: offset 必须严格 +1；gap/integrity fail 刷新订阅
- getter 做 max_age_seconds 过期保护，过期禁止用于交易

---

## Part E — Lessons from Old Code（`old/` 参考代码分析）

旧代码是一套已生产运行的 delta-neutral 轮换套利机器人（`old/lighter_edgex_hedge.py`，3496 行）。以下是从中提取的**应保留模式**和**应避免模式**，以及跨交易所对接的踩坑记录。

> **注意**：此部分为经验总结，不代表新架构必须 1:1 复刻旧实现。所有设计决策最终以 Part A Spec 验收口径为准，旧代码位置仅作参考溯源。

### E.1 应保留的设计模式

| 模式 | 旧代码位置 | 新代码应如何采纳 |
|---|---|---|
| **并发开平仓 + 单腿失败回滚** | `lighter_edgex_hedge.py:open_delta_neutral_position()` | `core/execution.py`：`asyncio.gather()` 双腿下单，任一腿失败 → 立即 close 成功腿 + log error event |
| **Decimal tick-aware 舍入** | `edgex_client.py:_round_to_tick()`, `_ceil_to_tick()`, `_floor_to_tick()` | `core/sizing.py`：使用 `Decimal` 而非 `float`；取两交易所较粗 tick size，floor 保证两边 size 一致 |
| **启动 ensure-accounts-flat** | `lighter_edgex_hedge.py:ensure_accounts_flat()` | `core/orchestrator.py` 启动流程：扫描两交易所所有非零仓位，发现未托管仓位 → ERROR 阻断 |
| **Funding rate 缓存（300s TTL）** | `lighter_edgex_hedge.py:FUNDING_CACHE` | `core/scanner.py`：字典缓存 `{symbol: (rate, ts)}`，过期重新获取 |
| **Aggressive limit order（3% 跨价）** | `edgex_client.py:cross_price()` | `exchanges/` adapters：BUY = mid × (1 + cross_pct/100)，SELL = mid × (1 - cross_pct/100)；`cross_pct` 从 `bot_config.json` 读取，不再硬编码 |
| **Close confirm + 重试** | `lighter_edgex_hedge.py:close_delta_neutral_position()` | `core/execution.py`：平仓后轮询仓位归零（最多 2 次重试），不完整 → 扩大 cross_pct 重试 → 仍失败 → ERROR |
| **止损自动计算** | `lighter_edgex_hedge.py:check_stop_loss()` | `core/orchestrator.py` HOLDING 监控：`stop_loss_pct = (100 / leverage) * 0.7`，基于维持保证金模型 |
| **原子写入 state 文件** | `lighter_edgex_hedge.py:StateManager`（temp file + `os.replace()`） | `db/store.py`：SQLite 事务已提供原子性，无需额外处理 |

### E.2 应避免的设计问题（新代码已/应改进）

| 旧代码问题 | 影响 | 新代码改进方案 |
|---|---|---|
| **每次查询新建临时 WS 连接** | `lighter_client.py` 中 `get_lighter_balance()`、`LighterOrderBookFetcher` 每次创建新 WS 连接再关闭，延迟高、资源浪费 | ✅ 已改：`src/services/` 使用常驻后台 asyncio Task + 自动重连 |
| **3496 行单文件** | 测试困难、职责不清、改动风险大 | ✅ 已改：模块化 `src/` package（db / services / exchanges / core） |
| **JSON 文件存状态** | 无查询能力、无并发安全、无可审计性 | ✅ 已改：SQLite + events 审计日志 |
| **`cross_pct=3.0` 硬编码** | `edgex_client.py` 和 `lighter_client.py` 中写死，调整需改代码 | `bot_config.json` 中统一管理，adapter 从配置读取 |
| **全局 `asyncio.Semaphore(2)`** | 所有 Lighter 调用共用一个限流器，粒度太粗 | 改为 per-adapter 限流，或基于 endpoint 的细粒度限流 |
| **字符串状态名（`"IDLE"`）** | 无类型检查，typo 风险 | 使用 `enum.StrEnum`（`core/models.py` 中 `BotState`） |
| **双配置文件源** | `.env` + `bot_config.json` 分开管理，启动前需检查两处 | `src/config.py` 统一加载、校验、导出 `AppConfig` |
| **Scanner 串行查询** | `check_all_spreads.py` 每个 symbol 间隔 1s，全量扫描耗时长 | 可并行 `asyncio.gather()` + per-symbol 小延迟防 rate limit |

### E.3 交易所对接踩坑记录（直接写入 adapter 实现注释）

**EdgeX (`edgex_client.py`)**：
- `account_id` 传给 SDK 时**必须是 `int`**，不能是字符串（SDK 内部有位运算）
- `contract_id` 在 `CreateOrderParams` 中**必须是 `str`**
- 合约命名格式：`{SYMBOL}{QUOTE}`（如 `"BTCUSD"`），不是 `"BTC-USD"`
- 设置杠杆地址：`/api/v1/private/account/updateLeverageSetting`（内部接口，非公开 SDK）
- `get_24_hour_quote()` 的 `value` 字段是 24h 成交量（USD），不是 `volume`

**Lighter (`lighter_client.py`)**：
- 仓位大小：`pos.position * pos.sign`（`pos.position` 是无符号量，`pos.sign` 为 ±1），**不是** `pos.size`
- 开仓均价：`pos.avg_entry_price`，**不是** `pos.entry_price`
- 平仓：必须在 `signer.create_order()` 中传 `reduce_only=1`
- Market 识别：Lighter 用数字 ID，EdgeX 用字符串 contract_id，映射关系见 `doc/markets_and_indexes.txt`
- 资金费率查询：`FundingApi.funding_rates()` 返回的 ` Funding` 字段注意大小写

### E.4 `src/core/models.py` 类型定义参考

```python
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional

class BotState(StrEnum):
    IDLE = "IDLE"
    ANALYZING = "ANALYZING"
    OPENING = "OPENING"
    HOLDING = "HOLDING"
    CLOSING = "CLOSING"
    WAITING = "WAITING"
    ERROR = "ERROR"
    SHUTDOWN = "SHUTDOWN"

@dataclass
class Opportunity:
    symbol: str
    edgex_rate: Optional[float] = None
    lighter_rate: Optional[float] = None
    edgex_apr: float = 0.0
    lighter_apr: float = 0.0
    net_apr: float = 0.0
    volume: float = 0.0
    spread: float = 0.0
    direction: str = ""          # "long_edgex_short_lighter" | "short_edgex_long_lighter"

@dataclass
class PositionState:
    symbol: str
    cycle_id: int
    edgex_size: float = 0.0
    lighter_size: float = 0.0
    edgex_entry: float = 0.0
    lighter_entry: float = 0.0
    opened_at: str = ""

@dataclass
class CycleRecord:
    cycle_id: int
    symbol: str
    state: BotState = BotState.IDLE
    opened_at: Optional[str] = None
    closed_at: Optional[str] = None
    edgex_pnl: float = 0.0
    lighter_pnl: float = 0.0
    net_pnl: float = 0.0
```

---

## Appendix — M0/M1 最小可运行 Demo (copy-paste)

> Target: validate two things (no trading yet)
> 1) SQLite can write `state/events`
> 2) Lighter WS (user_stats + ticker) can run as background tasks and keep updating

### Install
```bash
pip install aiosqlite websockets
```

### Environment
```bash
export LIGHTER_WS_URL=wss://mainnet.zklighter.elliot.ai/stream
export ACCOUNT_INDEX=0
export MARKET_ID=0
```

### `src/db/schema.sql`
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS bot_kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  event_type TEXT NOT NULL,
  cycle_id INTEGER,
  position_id INTEGER,
  data_json TEXT NOT NULL,
  message TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
```

### `src/util/time.py`
```python
from datetime import datetime, timezone

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
```

### `src/util/retry.py`
```python
import asyncio
import random
from dataclasses import dataclass

@dataclass
class Backoff:
    initial: float = 1.0
    factor: float = 2.0
    maximum: float = 30.0
    jitter: float = 0.25

    def delay(self, attempt: int) -> float:
        base = min(self.initial * (self.factor ** attempt), self.maximum)
        j = base * self.jitter
        return base + random.uniform(-j, j)

async def sleep_backoff(backoff: Backoff, attempt: int) -> None:
    await asyncio.sleep(max(0.0, backoff.delay(attempt)))
```

### `src/db/store.py`
```python
import json
from dataclasses import dataclass
from typing import Optional

import aiosqlite

from src.util.time import utc_now_iso

@dataclass
class Event:
    level: str
    event_type: str
    data: dict
    message: str = ""
    cycle_id: Optional[int] = None
    position_id: Optional[int] = None
    ts: Optional[str] = None

class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def start(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._conn.execute("PRAGMA busy_timeout=5000;")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Store not started"
        return self._conn

    async def init_schema(self, schema_sql: str) -> None:
        await self.conn.executescript(schema_sql)
        await self.conn.commit()

    async def kv_set(self, key: str, value: str) -> None:
        now = utc_now_iso()
        await self.conn.execute(
            "INSERT INTO bot_kv(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now),
        )
        await self.conn.commit()

    async def kv_get(self, key: str) -> Optional[str]:
        row = await self.conn.execute(
            "SELECT value FROM bot_kv WHERE key=?", (key,)
        )
        r = await row.fetchone()
        return r["value"] if r else None

    async def append_event(self, ev: Event) -> None:
        ts = ev.ts or utc_now_iso()
        await self.conn.execute(
            "INSERT INTO events(ts,level,event_type,cycle_id,position_id,data_json,message) VALUES(?,?,?,?,?,?,?)",
            (ts, ev.level, ev.event_type, ev.cycle_id, ev.position_id, json.dumps(ev.data, ensure_ascii=False), ev.message),
        )
        await self.conn.commit()
```

### `src/services/lighter_userstats_service.py`
```python
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import websockets

from src.util.retry import Backoff, sleep_backoff

logger = logging.getLogger(__name__)

class StaleDataError(RuntimeError):
    pass

@dataclass
class UserStatsSnapshot:
    available: float
    portfolio: float
    ts_epoch: float

class LighterUserStatsService:
    def __init__(self, ws_url: str, account_index: int):
        self.ws_url = ws_url
        self.account_index = account_index

        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

        self._snap: Optional[UserStatsSnapshot] = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        if self._task:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="lighter-userstats")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def wait_ready(self, timeout: float = 30.0) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    async def get_balance(self, max_age_seconds: float = 10.0) -> Tuple[float, float]:
        now = time.time()
        async with self._lock:
            if not self._snap:
                raise StaleDataError("no user_stats")
            age = now - self._snap.ts_epoch
            if age > max_age_seconds:
                raise StaleDataError(f"user_stats stale age={age:.2f}s")
            return self._snap.available, self._snap.portfolio

    async def _run(self) -> None:
        backoff = Backoff()
        attempt = 0
        sub_msg = {"type": "subscribe", "channel": f"user_stats/{self.account_index}"}

        while not self._stop.is_set():
            try:
                async with websockets.connect(self.ws_url, ping_interval=None) as ws:
                    attempt = 0
                    await ws.send(json.dumps(sub_msg))
                    while not self._stop.is_set():
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                            continue
                        if t not in ("update/user_stats", "subscribed/user_stats"):
                            continue
                        stats = msg.get("stats") or {}
                        avail = float(stats.get("available_balance", 0) or 0)
                        port = float(stats.get("portfolio_value", 0) or 0)
                        async with self._lock:
                            self._snap = UserStatsSnapshot(available=avail, portfolio=port, ts_epoch=time.time())
                        self._ready.set()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("UserStats WS error: %s", e)
                await sleep_backoff(backoff, attempt)
                attempt += 1
```

### `src/services/lighter_ticker_service.py`
```python
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import websockets

from src.util.retry import Backoff, sleep_backoff

logger = logging.getLogger(__name__)

class StaleDataError(RuntimeError):
    pass

def _parse_mid(channel: str) -> Optional[int]:
    if not isinstance(channel, str) or not channel.startswith("ticker:"):
        return None
    try:
        return int(channel.split(":", 1)[1])
    except Exception:
        return None

@dataclass
class TickerTop:
    bid: Optional[float] = None
    ask: Optional[float] = None
    ts_epoch: float = 0.0

class LighterTickerService:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._ws = None
        self._lock = asyncio.Lock()
        self._subscribed: set[int] = set()
        self._tops: Dict[int, TickerTop] = {}

    async def start(self) -> None:
        if self._task:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="lighter-ticker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def subscribe(self, market_id: int) -> None:
        mid = int(market_id)
        async with self._lock:
            self._subscribed.add(mid)
            self._tops.setdefault(mid, TickerTop())
        if self._ws:
            await self._ws.send(json.dumps({"type": "subscribe", "channel": f"ticker/{mid}"}))

    async def get_best_bid_ask(self, market_id: int, max_age_seconds: float = 3.0) -> Tuple[float, float]:
        mid = int(market_id)
        now = time.time()
        async with self._lock:
            top = self._tops.get(mid)
            if not top or top.bid is None or top.ask is None:
                raise StaleDataError("ticker not ready")
            age = now - top.ts_epoch
            if age > max_age_seconds:
                raise StaleDataError(f"ticker stale age={age:.2f}s")
            return float(top.bid), float(top.ask)

    async def _run(self) -> None:
        backoff = Backoff()
        attempt = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.ws_url, ping_interval=None) as ws:
                    self._ws = ws
                    attempt = 0
                    async with self._lock:
                        mids = sorted(self._subscribed)
                    for mid in mids:
                        await ws.send(json.dumps({"type": "subscribe", "channel": f"ticker/{mid}"}))

                    while not self._stop.is_set():
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                            continue
                        if t != "update/ticker":
                            continue
                        mid = _parse_mid(msg.get("channel", ""))
                        if mid is None:
                            continue
                        tick = msg.get("ticker") or {}
                        a = tick.get("a") or {}
                        b = tick.get("b") or {}
                        try:
                            ask = float(a.get("price"))
                            bid = float(b.get("price"))
                        except Exception:
                            continue
                        async with self._lock:
                            top = self._tops.setdefault(mid, TickerTop())
                            top.ask = ask
                            top.bid = bid
                            top.ts_epoch = time.time()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Ticker WS error: %s", e)
                self._ws = None
                await sleep_backoff(backoff, attempt)
                attempt += 1
```

### `src/logging_.py`
```python
import logging
import sys

def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
```

### `src/main.py`
```python
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from src.logging_ import setup_logging
from src.db.store import Store, Event
from src.services.lighter_userstats_service import LighterUserStatsService, StaleDataError
from src.services.lighter_ticker_service import LighterTickerService

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    load_dotenv()

    ws_url = os.environ.get("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")
    account_index = int(os.environ.get("ACCOUNT_INDEX", "0"))
    market_id = int(os.environ.get("MARKET_ID", "0"))

    store = Store("bot.sqlite3")
    await store.start()
    await store.init_schema(Path("src/db/schema.sql").read_text(encoding="utf-8"))

    await store.kv_set("state", "BOOT")
    await store.append_event(Event(level="info", event_type="BOOT", data={"ws_url": ws_url}))

    userstats = LighterUserStatsService(ws_url, account_index)
    ticker = LighterTickerService(ws_url)

    await userstats.start()
    await ticker.start()
    await ticker.subscribe(market_id)

    await userstats.wait_ready(timeout=30)
    await store.kv_set("state", "RUNNING")

    try:
        while True:
            try:
                avail, port = await userstats.get_balance()
                bid, ask = await ticker.get_best_bid_ask(market_id)
            except StaleDataError as e:
                logger.warning("Stale data, skipping snapshot: %s", e)
                await store.append_event(Event(
                    level="warn", event_type="STALE_DATA",
                    data={"reason": str(e)},
                ))
                await asyncio.sleep(3)
                continue

            logger.info("balance avail=%.2f port=%.2f | ticker bid=%.2f ask=%.2f", avail, port, bid, ask)

            await store.append_event(Event(
                level="info",
                event_type="SNAPSHOT",
                data={
                    "exchange": "lighter",
                    "available": avail,
                    "portfolio": port,
                    "market_id": market_id,
                    "bid": bid,
                    "ask": ask,
                },
            ))

            await asyncio.sleep(5)

    finally:
        await userstats.stop()
        await ticker.stop()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
```

### Run
```bash
python -m src.main
```

Expected:
- prints every 5 seconds (可调整，见 Part A 2.3 sleep 间隔约束)
- `bot.sqlite3` keeps growing (events inserted)
- `STALE_DATA` warn events 在数据过期时写入，连续 N 次（建议 3 次）后触发 WS 重连；正常网络下不应频繁出现
