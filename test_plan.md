# 测试计划 — Cross-Exchange Funding-Rate Bot

**原则：从不花钱到花钱，每层必须全绿才进入下一层。**

反复实盘调试导致累计手续费损失超过 $20。此计划通过离线验证、参数预检查、最小仓位分阶段上线来消除这一问题。

---

## 执行顺序总览

```
Phase 0  fetch_fixtures.py              ← 网络，无认证，零费用
Phase 1A sizing.py 单元测试             ← 无网络，零费用
Phase 1B adapter 解析单元测试（Mock）   ← 无网络，零费用
Phase 1C DB store 单元测试              ← 无网络，零费用
Phase 2  公开 API 集成测试              ← 网络，无认证，零费用
Phase 3  认证只读测试                   ← 网络，有认证，零费用
Phase 4  下单参数预验证（不下单）       ← 网络，有认证，零费用
Phase 5  执行引擎单元测试（Mock）       ← 无网络，零费用
Phase 6  Orchestrator dry-run           ← 网络，无下单，零费用
Phase 7  单腿微小实盘（DOGE ~$5）       ← ⚠️ 实盘，手动触发
Phase 8  双腿微小实盘（DOGE ~$5）       ← ⚠️ 实盘，手动触发
Phase 9  main.py 完整运行               ← ⚠️ 实盘，生产
```

---

## Phase 0 — 元数据爬取与固化（一次性）

**目标**：把各交易所的精度约束固化为本地 JSON fixture，作为所有后续阶段的唯一真相来源。

**脚本**：`tests/fetch_fixtures.py`

### 爬取字段（每交易所 × 每合约）

| 字段 | 用途 |
|------|------|
| `price_tick` | 最小价格单位，Lighter 为 `10^-supported_price_decimals` |
| `size_step` | 最小数量单位，Lighter 为 `10^-supported_size_decimals` |
| `min_order_size` | 最小下单量（部分交易所有此限制） |
| `max_leverage` | 最大杠杆倍数 |
| `contract_type` | perp / spot |
| `base_currency` / `quote_currency` | 币种 |
| 价格小数位数 | 用于格式验证 |
| 数量小数位数 | 用于格式验证 |

### 输出文件

```
tests/fixtures/lighter_markets.json    ← GET /api/v1/orderBooks
tests/fixtures/edgex_contracts.json   ← edgex_sdk Client.get_metadata()
tests/fixtures/hl_meta.json           ← POST /info {"type":"meta"}
tests/fixtures/grvt_markets.json      ← GRVT 公开 meta endpoint
```

**完成标准**：4 个 fixture 文件入库，覆盖所有配置的交易对（BTC, ETH, SOL, DOGE 等）。

---

## Phase 1A — `sizing.py` 精度数学单元测试

**脚本**：`tests/unit/test_sizing.py`（无网络，使用 Phase 0 fixture 中的真实 tick/step 值）

### `round_price_to_tick`

- BUY 方向向上取整，SELL 方向向下取整
- 使用真实 tick：BTC=0.1, ETH=0.01, DOGE=0.0001
- 极端值：price=0.00001, tick=0.00001（SHIB 类合约）
- 结果必须是 tick 的整数倍（无浮点残差）

### `round_size_to_step`

- 向下保守取整（避免超买）
- step=0.001 / step=1 / step=100 各场景
- 断言：`result / step` 为整数

### `cross_price`

- 结果必须是 tick 的整数倍
- BUY 方向结果 > mid，SELL 方向结果 < mid

### `calculate_position_size`

- `leverage=0` → `ValueError`
- `mid_price=0` → `ValueError`
- `mid_price<0` → `ValueError`
- 正常输入：结果 ≤ `min(available_a, available_b) * leverage * safety_factor / mid`

---

## Phase 1B — Adapter 解析单元测试（Mock HTTP）

**脚本**：`tests/unit/test_adapter_parsing.py`（使用 `unittest.mock.AsyncMock`）

对每个 adapter（Lighter / EdgeX / Hyperliquid / GRVT）：

```python
# 给定 fixture 中的原始 API 响应 JSON，注入 Mock HTTP 客户端
# 断言解析出的 MarketDetails 与 fixture 一致
assert md.price_tick == fixture[symbol]["price_tick"]
assert md.size_step  == fixture[symbol]["size_step"]

# Balance 不变量
assert bal.available >= 0
assert bal.available <= bal.total_equity

# Position 精度
for pos in positions:
    assert pos.symbol in fixture
    size_mod = abs(pos.size) % fixture[pos.symbol]["size_step"]
    assert size_mod < 1e-9, f"size residual: {size_mod}"
```

---

## Phase 1C — DB Store 单元测试

**脚本**：`tests/unit/test_store.py`

- `Store` 初始化：schema 正确创建，所有表存在
- `kv_set` / `kv_get` 往返一致性
- `append_event` 写入 → 查询验证字段完整
- 事件表无限增长问题（Issue #11）：插入 10000 条旧记录 → 触发 prune → 验证行数 ≤ 上限

---

## Phase 2 — 公开 API 集成测试（有网络，无认证）

**脚本**：`tests/test_<exchange>.py --public`（扩展现有脚本）

对每个配置的交易对：

```python
md = await adapter.get_market_details(symbol)
# 与 fixture 比对
assert md.price_tick == fixture[symbol]["price_tick"]
assert md.size_step  == fixture[symbol]["size_step"]
assert md.price_tick > 0 and md.size_step > 0

bba = await adapter.get_best_bid_ask(md.market_id)
assert bba.bid > 0 and bba.ask > 0
assert bba.bid < bba.ask                              # bid < ask
assert (bba.ask - bba.bid) / bba.bid < 0.01          # 价差 < 1%
# bid 应为 price_tick 整数倍（检查 adapter 是否截断精度）
assert abs(bba.bid % md.price_tick) < 1e-9

fr = await adapter.get_funding_rate(md.market_id)
# 极端资金费率告警（不 fail，仅打印）
if fr and abs(fr.rate) > 0.01:
    print(f"WARNING: extreme funding rate {fr.rate}")
# 验证年化系数正确
# Lighter: rate × 3 × 365 × 100，EdgeX: rate × 24 × 365 × 100
```

**完成标准**：所有交易所公开接口 ✓，BBA 精度与 fixture 匹配。

---

## Phase 3 — 认证只读测试（有网络，有认证）

**脚本**：`tests/test_<exchange>.py --account`

```python
bal = await adapter.get_balance()
assert bal.available >= 0
assert bal.total_equity >= bal.available

positions = await adapter.get_open_positions()
for pos in positions:
    assert pos.symbol.upper() in fixture          # symbol 已知
    # size 是 size_step 整数倍
    assert abs(pos.size) % fixture[pos.symbol]["size_step"] < 1e-9
    # entry_price 是 price_tick 整数倍
    assert pos.entry_price % fixture[pos.symbol]["price_tick"] < 1e-9
```

**完成标准**：账户数据格式合法，精度与 fixture 一致。

---

## Phase 4 — 下单参数预验证（有网络，不下单）

**脚本**：`tests/test_<exchange>.py --validate-order`（新增模式，只计算不发送）

对每个交易所 × 每个配置的交易对：

```python
bba = await adapter.get_best_bid_ask(md.market_id)
mid = (bba.bid + bba.ask) / 2.0

size_base  = round_size_to_step(notional / mid, md.size_step)
buy_price  = cross_price("buy",  bba.bid, bba.ask, md.price_tick, 3.0)
sell_price = cross_price("sell", bba.bid, bba.ask, md.price_tick, 3.0)

# ── Lighter 特有：scaled integer 无残差 ──────────────────────────
base_scaled  = int(round(size_base / md.size_step))
price_scaled = int(buy_price / md.price_tick)
assert base_scaled  == size_base / md.size_step   # 无浮点残差
assert price_scaled == buy_price / md.price_tick  # 无浮点残差
assert type(base_scaled)  is int
assert type(price_scaled) is int

# ── EdgeX 特有：string Decimal 可安全解析 ────────────────────────
from decimal import Decimal, InvalidOperation
try:
    Decimal(str(size_base))
    Decimal(str(buy_price))
except InvalidOperation as e:
    raise AssertionError(f"EdgeX string format invalid: {e}")

# ── 最小下单量 ──────────────────────────────────────────────────
assert size_base >= md.size_step
# 若 fixture 有 min_notional
if "min_notional" in fixture[symbol]:
    assert size_base * mid >= fixture[symbol]["min_notional"]

print(f"  ✓ {exchange} {symbol}: size={size_base} price={buy_price} (NOT sent)")
```

**完成标准**：每个交易所每个合约的参数验证 ✓，特别是 Lighter int 缩放无残差，Hyperliquid float 精度无截断。

---

## Phase 5 — 执行引擎单元测试（Mock Adapter）

**脚本**：`tests/unit/test_execution.py`（使用 `unittest.mock.AsyncMock`）

### `open_position` 路径

| 场景 | 预期结果 |
|------|----------|
| 双腿成功 | `PositionState` 正确，DB 有 position + legs 记录，cycle 状态=HOLDING |
| 长腿失败 | 短腿 rollback 被调用，cycle 状态=ERROR |
| 短腿失败 | 长腿 rollback 被调用，cycle 状态=ERROR |
| 双腿均失败 | 无 rollback 尝试，cycle 状态=ERROR |
| `confirm_positions` 超时 | 紧急平仓被调用（`close_position` ×2），cycle 状态=ERROR |

### `close_position` 路径

| 场景 | 预期结果 |
|------|----------|
| 第 1 次关仓成功 | cycle 状态=CLOSED，PnL 记录正确 |
| 第 1 次失败，第 2 次成功（更宽 cross_pct） | **BBA 需重新拉取**（验证 Issue #2 修复） |
| 3 次均失败 | `RuntimeError` + cycle 状态=ERROR |

---

## Phase 6 — Orchestrator Dry-Run（有网络，无下单）

**脚本**：`tests/test_orchestrator.py`（默认模式，不含 `--live`）

验证状态机完整遍历：

```
IDLE → 检查仓位平坦
     → [ANALYZING] scan_all() 返回 candidates
     → [OPENING] 计算 size_base, long_price, short_price
              → 验证参数合法（同 Phase 4 规则）
              → 打印 "dry-run: would place order" 但不调用 place_order()
     → [HOLDING] 打印当前 funding rate / uPnL（只读）
     → [CLOSING] 计算关仓价格（不调用 close_position()）
     → [WAITING] 进入冷却
```

**DB 验证**：
- dry-run 模式不应写入 positions/legs 记录（只有 events 可写）
- orchestrator state kv 正确流转

**完成标准**：整个状态机 dry-run 无错误，日志可读，无 `place_order` 调用。

---

## Phase 7 — 单腿微小实盘（单交易所，手动触发）

> ⚠️ **实盘，需手动触发，在 Phase 1–6 全绿后才执行**

**脚本**：`tests/test_lighter.py --order --notional 5 --symbol DOGE`
（约 $2 USD，手续费约 $0.02）

**验证步骤**：
1. 订单被接受 → 返回 `client_order_id`
2. 5s 后 `get_open_positions()` 确认仓位存在，size 精度匹配 fixture
3. 立即关仓（reduce-only）
4. `get_open_positions()` 确认归零
5. 对 EdgeX 重复同样流程

**完成标准**：两个交易所单腿开平仓均成功，无残留仓位。

---

## Phase 8 — 双腿微小实盘（完整机器人，手动触发）

> ⚠️ **实盘，需手动触发，在 Phase 7 全绿后才执行**

**命令**：`python tests/test_orchestrator.py --live --symbol DOGE --notional 5`

**验证步骤**：
1. 双腿 `asyncio.gather` 并发开仓
2. 两腿均通过 `confirm_positions` 确认
3. 5s 后并发关仓
4. 两腿均通过 `confirm_flat` 确认归零
5. DB 有完整 cycle 记录（OPENING → HOLDING → CLOSED）

**完成标准**：双腿开平仓成功，cycle 状态=CLOSED，实际手续费 < $1。

---

## Phase 9 — main.py 完整运行（生产）

> ⚠️ **仅在 Phase 8 全绿后启动**

- 从 `bot_config.json` 加载配置
- 观察第一个完整 cycle（IDLE → OPENING → HOLDING → CLOSING → IDLE）
- 监控 `events` 表，确认无 `ERROR` 事件
- 确认 funding payment 被正确记录

---

## 已知风险与修复项（需在 Phase 5 前解决）

以下问题在静态分析中发现，部分会导致测试失败，需先修复：

| # | 问题 | 影响 Phase | 修复方式 |
|---|------|-----------|---------|
| 1 | `_do_closing` 串行关仓（有对冲窗口） | Phase 8 | 改为 `asyncio.gather` |
| 2 | 关仓重试使用过期 BBA | Phase 5, 8 | 重试前重新拉取 BBA |
| 3 | `_do_holding` 风控检查串行 | Phase 6, 9 | 改为 `asyncio.gather` |
| 4 | `LighterAdapter.get_market_details` 无缓存 | Phase 2, 6 | 添加 lazy cache（同 EdgeX） |
| 5 | `get_balance`/`get_open_positions` 重复调用同一端点 | Phase 3 | 合并为 `_fetch_account()` |
| 6 | Funding rate 缓存无锁（并发 stampede） | Phase 2, 9 | 添加 `asyncio.Lock` |

---

## 运行命令速查

```bash
# Phase 0: 爬取元数据 fixture
python tests/fetch_fixtures.py

# Phase 1: 单元测试（无网络）
python -m pytest tests/unit/ -v

# Phase 2: 公开 API（各交易所）
python tests/test_lighter.py --public
python tests/test_edgex.py --public
python tests/test_hyperliquid.py --public

# Phase 3: 认证只读
python tests/test_lighter.py --account
python tests/test_edgex.py --account
python tests/test_hyperliquid.py --account

# Phase 4: 下单参数预验证（不下单）
python tests/test_lighter.py --validate-order
python tests/test_edgex.py --validate-order
python tests/test_hyperliquid.py --validate-order

# Phase 6: Orchestrator dry-run
python tests/test_orchestrator.py

# Phase 7: 单腿实盘（手动触发，谨慎）
python tests/test_lighter.py --order --notional 5 --symbol DOGE
python tests/test_edgex.py --order --notional 5 --symbol DOGE

# Phase 8: 双腿实盘（手动触发，谨慎）
python tests/test_orchestrator.py --live --symbol DOGE --notional 5
```
