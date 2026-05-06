# 跨交易所套利机器人 — 优化建议

> 本文件基于对代码库的静态分析，按优先级列出可优化项，供参考实施。
> 仓库路径：`src/core/`、`src/exchanges/`

---

## 🔴 高优先级（正确性 / 风险）

### 1. `_do_closing` 串行关仓，存在对冲断裂窗口

**文件：** `src/core/orchestrator.py`（第 499–505 行）

**现状：**
```python
for pos in active:
    await close_position(self.adapters, self.store, exec_config, position_id=pos["id"])
```
紧急关仓时逐个 `await`，若有多个活跃仓位，第一个关完到第二个开始关之间存在一段时间的对冲断裂暴露风险。

**建议：**
```python
tasks = [
    close_position(self.adapters, self.store, exec_config, position_id=pos["id"])
    for pos in active
]
results = await asyncio.gather(*tasks, return_exceptions=True)
for pos, result in zip(active, results):
    if isinstance(result, Exception):
        logger.error("Close position %d failed: %s", pos["id"], result)
    else:
        success_count += 1
```
将所有仓位并发关闭，最大化降低对冲断裂时间。

---

### 2. 关仓重试使用过期价格快照

**文件：** `src/core/execution.py`（第 302–316 行）

**现状：**
```python
# long_bba / short_bba 在进入循环前一次性获取
for attempt in range(3):
    if attempt > 0:
        # 仍然使用最初快照的 long_bba / short_bba
        long_close_px = cross_price("sell", long_bba.bid, long_bba.ask, ...)
        short_close_px = cross_price("buy", short_bba.bid, short_bba.ask, ...)
```
重试时市价可能已大幅偏移，用过期报价计算的价格可能完全无法成交。

**建议：** 每次重试前重新拉取 BBA：
```python
for attempt in range(3):
    if attempt > 0:
        long_bba, short_bba = await asyncio.gather(
            long_adapter.get_best_bid_ask(long_market_id),
            short_adapter.get_best_bid_ask(short_market_id),
        )
        wider_pct = config.cross_pct * (1.0 + attempt * 0.5)
        long_close_px = cross_price("sell", long_bba.bid, long_bba.ask, tick=tick, cross_pct=wider_pct)
        short_close_px = cross_price("buy", short_bba.bid, short_bba.ask, tick=tick, cross_pct=wider_pct)
```

---

### 3. `_do_holding` 中多仓位风控检查串行执行

**文件：** `src/core/orchestrator.py`（第 308–410 行）

**现状：**
```python
for pos in active:
    long_positions = await long_adapter.get_open_positions()   # 阻塞
    short_positions = await short_adapter.get_open_positions() # 阻塞
    long_md = await long_adapter.get_market_details(symbol)    # 阻塞
    short_md = await short_adapter.get_market_details(symbol)  # 阻塞
```
N 个仓位 × 4 个 HTTP 请求 = 全部串行，极端行情下止损延迟线性增长。

**建议：** 把每个仓位的检查包成独立协程，用 `asyncio.gather` 并发：
```python
async def _check_one_position(pos):
    long_positions, short_positions = await asyncio.gather(
        long_adapter.get_open_positions(),
        short_adapter.get_open_positions(),
    )
    long_md, short_md = await asyncio.gather(
        long_adapter.get_market_details(symbol),
        short_adapter.get_market_details(symbol),
    )
    ...

await asyncio.gather(*[_check_one_position(p) for p in active])
```

---

### 4. `_do_error` 永久阻塞，无自动恢复或告警

**文件：** `src/core/orchestrator.py`（第 532–539 行）

**现状：**
```python
async def _do_error(self) -> None:
    while not self._stop.is_set():
        await asyncio.sleep(30)  # 永远等待，无任何动作
```
进入 ERROR 后 bot 只能靠人工重启，且没有外部通知。

**建议：**
- 在进入 ERROR 时发送外部通知（Telegram / Discord webhook，通过可选配置项控制）。
- 可选：超过指定时间（如 30 分钟）后自动尝试再次关仓 → IDLE，作为最后的自救路径。

---

## 🟠 中优先级（性能 / 效率）

### 5. `LighterAdapter.get_market_details` 每次扫描都拉取完整列表，无缓存

**文件：** `src/exchanges/lighter_adapter.py`（第 179–197 行）

**现状：**
每次调用 `get_market_details` 都请求 `/api/v1/orderBooks`，返回所有交易对的完整列表。`scanner.scan_all` 对每个 symbol 都调用一次，N 个 symbol = N 次相同的完整列表请求。

**对比：** `EdgeXAdapter._load_metadata` 已有惰性缓存机制（第 41–63 行）。

**建议：** 在 `LighterAdapter` 中添加 TTL 缓存（建议 5 分钟）：
```python
_market_cache: Optional[tuple[float, dict[str, MarketDetails]]] = None

async def _load_market_cache(self) -> dict[str, MarketDetails]:
    import time as _time
    if self._market_cache and _time.monotonic() - self._market_cache[0] < 300:
        return self._market_cache[1]
    # ... 拉取并缓存
    self._market_cache = (_time.monotonic(), cache)
    return cache
```

---

### 6. `LighterAdapter.get_balance` 和 `get_open_positions` 重复请求同一接口

**文件：** `src/exchanges/lighter_adapter.py`（第 56–71 行，第 145–173 行）

**现状：**
两个方法都请求 `/api/v1/account?by=index&value=<idx>`，在 `open_position` 开仓流程中会连续触发两次相同的 HTTP 调用。

**建议：** 合并为一个内部方法 `_fetch_account()` 返回原始账户数据，`get_balance` 和 `get_open_positions` 分别从中解析所需字段，避免重复请求。

---

### 7. 资金费率已支持批量拉取，但 `scanner` 仍按 symbol 逐个触发

**文件：** `src/core/scanner.py`（第 92–93 行），`src/exchanges/lighter_adapter.py`（第 105–139 行）

**现状：**
Lighter 的 `/api/v1/funding-rates` 一次返回全部 symbol 的资金费率，`LighterAdapter` 内部已有 30 秒 TTL 缓存。但 `scanner._scan_one` 每个 symbol 都调用 `adapter.get_funding_rate(market_id)`，在缓存过期后第一个 symbol 会触发全量拉取，后续 symbol 命中缓存。
问题是 `asyncio.gather(*tasks)` 并发时，多个 symbol 的 `get_funding_rate` 可能同时发现缓存已过期，并发触发多次相同的全量请求（缓存竞争）。

**建议：** 使用 `asyncio.Lock` 保护缓存刷新，避免并发重复请求：
```python
self._funding_lock = asyncio.Lock()

async def _refresh_funding_cache(self):
    async with self._funding_lock:
        if self._funding_cache and _time.monotonic() - self._funding_cache[0] < 30:
            return  # 已被另一个协程刷新，直接返回
        # ... 拉取并写入缓存
```

---

## 🟡 低优先级（代码质量 / 可维护性）

### 8. `utc_now_iso` 在函数体内部延迟导入，散落多处

**文件：** `src/core/execution.py`（第 102 行、第 251 行、第 325 行），`src/core/orchestrator.py`（第 303 行）

**现状：**
```python
async def some_function():
    from src.util.time import utc_now_iso  # 函数内部导入
```
**建议：** 在各模块顶部统一导入，提高可读性，消除重复。

---

### 9. `sizing.py` 中参数名硬编码为 `edgex`/`lighter`，语义具体化

**文件：** `src/core/sizing.py`（第 39–46 行）

**现状：**
```python
def unify_size_step(size: float, edgex_step: float, lighter_step: float) -> float:
def unify_price_tick(price: float, edgex_tick: float, lighter_tick: float, side: str) -> float:
```
这两个函数实际上是通用工具，与交易所无关，但参数名写死了 edgex/lighter，会误导未来接入第三个交易所的开发者。

**建议：** 重命名为 `step_a`/`step_b` 和 `tick_a`/`tick_b`（或直接改为接受列表参数）。

---

### 10. `aiosqlite.Row` 访问无类型保障，拼写错误只能在运行时暴露

**文件：** `src/core/orchestrator.py`（第 309–312 行），`src/core/execution.py`（第 261–264 行）

**现状：**
```python
pos_id   = pos["id"]
symbol   = pos["symbol"]
long_ex  = pos["exchange_long"]
short_ex = pos["exchange_short"]
```
列名拼写错误只能在运行时引发 `KeyError`，无静态检查。

**建议：** 引入轻量的 `TypedDict` 或 `dataclass` 包装 DB 行：
```python
class PositionRow(TypedDict):
    id: int
    symbol: str
    exchange_long: str
    exchange_short: str
    ...
```

---

## 📊 运维 / 可观测性

### 11. SQLite 无清理策略，长期运行数据无限增长

**表：** `funding_snapshots`、`events`

每 `check_interval_seconds`（默认 60 秒）每个活跃仓位写入 2 条 `funding_snapshots`。5 个仓位 × 30 天 = ~43 万条记录，且永不清理。`events` 表同样如此。

**建议：** 添加定期清理任务（如 bot 启动时、或每次进入 WAITING 状态时），删除 N 天前的记录：
```python
await store.conn.execute(
    "DELETE FROM funding_snapshots WHERE recorded_at < datetime('now', '-7 days')"
)
```

---

### 12. 无外部告警通知机制

当前所有状态变化只写入本地 log 和 SQLite。建议添加可选的 Webhook 通知（Telegram/Discord），至少覆盖以下场景：
- 进入 `ERROR` 状态
- 仓位关闭失败（`CLOSING_FAILED`）
- 发现未对冲仓位（`RECOVERY_UNHEDGED`）

实现方式：在 `BotConfig` 中添加 `alert_webhook_url: Optional[str]`，在关键事件写入 `store.append_event` 时同时触发异步 HTTP POST。

---

## 🔒 配置安全

### 13. 私钥以明文字符串在内存中全程存活

**文件：** `src/config.py`（`Env` dataclass 的私钥字段）

私钥在读取后以 Python 字符串形式在内存中存活直至进程退出，存在内存泄露风险（如 core dump、debug 工具等）。

**建议（可选）：**
1. 在 `__post_init__` 中立即用私钥构造签名器对象，然后将原始字符串字段置为空字符串：
   ```python
   self._signer = build_signer(self.private_key)
   object.__setattr__(self, "private_key", "")
   ```
2. 支持从文件路径读取私钥，而不仅限于环境变量。

---

## 优先级汇总

| 编号 | 问题 | 影响 | 优先级 |
|------|------|------|--------|
| 1 | `_do_closing` 串行关仓 | 对冲断裂风险 | 🔴 高 |
| 2 | 关仓重试使用过期价格 | 订单可能无法成交 | 🔴 高 |
| 3 | `_do_holding` 串行风控 | 止损延迟 | 🔴 高 |
| 4 | `_do_error` 永久阻塞 | 需要人工干预 | 🔴 高 |
| 5 | `get_market_details` 无缓存 | 多余 HTTP 请求 | 🟠 中 |
| 6 | `get_balance`/`get_open_positions` 重复请求 | 多余 HTTP 请求 | 🟠 中 |
| 7 | 资金费率缓存并发竞争 | 偶发重复拉取 | 🟠 中 |
| 8 | 延迟导入散落多处 | 可读性 | 🟡 低 |
| 9 | 参数名硬编码交易所名 | 可维护性 | 🟡 低 |
| 10 | DB Row 无类型保障 | 运行时 KeyError 风险 | 🟡 低 |
| 11 | SQLite 无清理策略 | 磁盘占用增长 | 📊 运维 |
| 12 | 无外部告警通知 | 事故响应慢 | 📊 运维 |
| 13 | 私钥内存明文 | 安全性 | 🔒 安全 |
