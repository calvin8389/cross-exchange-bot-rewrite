# Plan B — Blueprint / 工程指南 / 分支信息整理

## 1. 文档来源

本文件基于当前分支已有资料整理，主要来源：

- `rewrite_guide.md`
  - Part A：Spec 风格重写指南
  - Part B：Blueprint 风格代码骨架索引
  - Part C：工程指南（WS 常驻后台任务 + SQLite）
  - Part E：旧代码经验与踩坑记录
- `README.md`
  - 当前架构、状态机、支持交易所、运行方式、测试方式

当前分支：

- branch: `copilot/analyze-codebase-for-arbitrage-bot`

---

## 2. 项目目标摘要

这是一个基于 Python + asyncio 的跨交易所 funding-rate arbitrage bot，核心目标是：

- 在不同交易所之间扫描同标的 funding rate 差异
- 建立 delta-neutral 对冲仓位
- 持仓期间监控净 APR、uPnL 和风控条件
- 满足条件后平仓，并进入下一轮轮换

当前 README 中支持的交易所：

- Lighter
- EdgeX
- Hyperliquid
- GRVT

---

## 3. Blueprint：推荐代码骨架

推荐目录结构来自 `rewrite_guide.md` Part B，并已基本落实到当前仓库：

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
    lighter_orderbook_service.py

  exchanges/
    base.py
    edgex_adapter.py
    lighter_adapter.py
    hyperliquid_adapter.py
    grvt_adapter.py

  core/
    models.py
    scanner.py
    sizing.py
    execution.py
    orchestrator.py
```

### 各层职责

- `config.py`
  - 统一加载 `.env` 与 `bot_config.json`
  - 对 active exchanges 做 fail-fast 校验

- `db/`
  - `schema.sql`：定义 SQLite SSOT / 审计表结构
  - `store.py`：提供异步状态写入、事件写入、恢复所需读写能力

- `services/`
  - 提供 Lighter WS 常驻后台任务
  - 管理订阅、自动重连、stale data 防护

- `exchanges/`
  - 每个交易所实现 `ExchangeAdapter`
  - 对核心逻辑暴露统一接口，避免策略层和具体 SDK/REST 细节耦合

- `core/scanner.py`
  - 扫描 symbol × exchange pair
  - 输出排序后的 `Opportunity`

- `core/sizing.py`
  - 统一 size step / price tick
  - 使用可控的跨价逻辑生成执行价格

- `core/execution.py`
  - 并发开仓 / 平仓
  - 回滚
  - confirm / retry
  - 审计落库

- `core/orchestrator.py`
  - 状态机入口
  - 管理 IDLE / ANALYZING / OPENING / HOLDING / CLOSING / WAITING / ERROR

---

## 4. 工程指南重点

### 4.1 状态机

README 当前描述的主流程：

```text
IDLE → ANALYZING → OPENING → HOLDING → CLOSING → WAITING → IDLE
```

阶段职责：

- `IDLE`
  - 确认账户平仓 / 无残留风险仓位

- `ANALYZING`
  - 扫描全市场候选机会
  - 按净 APR 排序

- `OPENING`
  - 打开 top N 仓位
  - 任一失败时做回滚

- `HOLDING`
  - 定时刷新 funding / uPnL / 风控状态
  - 机会失效或风控触发时关闭仓位
  - 有空余槽位时补开 replacement positions

- `CLOSING`
  - 紧急或手动关闭所有活跃仓位

- `WAITING`
  - 冷却后再回到下一轮

### 4.2 WS 常驻后台任务

`rewrite_guide.md` Part C 的核心原则：

- WS 服务使用常驻后台任务，而不是每次查询临时建连
- 对 timeout / 断线 / stale data 做自动重连与降级
- getter 必须提供 `max_age_seconds` 保护
- OPENING / CLOSING 阶段禁止使用过期数据继续下单

### 4.3 SQLite 作为 SSOT + 审计

当前仓库数据库相关核心表（见 README / `src/db/schema.sql`）：

- `bot_kv`
  - 全局键值状态

- `events`
  - 结构化事件日志

- `cycles`
  - 一轮开平仓闭环记录

- `positions`
  - 当前/历史仓位记录

- `position_legs`
  - 每个仓位在不同交易所的腿信息

- `orders`
  - 每一条 OPEN / CLOSE 订单审计记录

- `funding_snapshots`
  - 持仓期间 funding 采样

- `funding_payments`
  - 实际 funding 支付记录

工程约束：

- SQLite 是状态与审计的单一事实来源
- 关键交易步骤必须可回放
- 恢复流程依赖库内记录，而不是易损坏的 JSON 状态文件

### 4.4 配置原则

来自 `rewrite_guide.md` Part A + 当前 `README.md`：

- 通过 `ACTIVE_EXCHANGES` 决定启用哪些交易所
- active exchange 缺失必填环境变量时启动失败
- `bot_config.json` 统一管理策略阈值与仓位 tier
- 关键策略参数包括：
  - `symbols_to_monitor`
  - `leverage`
  - `min_net_apr_threshold`
  - `min_volume_usd`
  - `max_spread_pct`
  - `cross_pct`
  - `hold_duration_hours`
  - `enable_stop_loss`
  - `max_concurrent_positions`
  - `position_tiers`
  - `symbol_tiers`

---

## 5. 当前分支已体现的架构方向

基于当前分支代码与最近提交，可确认以下方向已被落实：

### 5.1 多交易所统一适配层

- 已存在 `ExchangeAdapter` 抽象层
- 新增交易所时，原则上只需实现 adapter 并接入 `main.py`
- 核心策略逻辑无需因交易所种类改变而重写

### 5.2 多仓位 / 多符号并行管理

- 当前实现支持 `max_concurrent_positions`
- 按 symbol tier 控制不同标的的下单名义金额
- HOLDING 阶段可在关闭后补开 replacement positions

### 5.3 执行路径强化

本分支近期已继续完善以下运行时能力：

- 平仓审计更偏向真实 fill 价格
- Lighter client order id 生成更稳健
- HOLDING 风险控制补齐：
  - 最大持仓时长触发平仓
  - stop loss 触发平仓

### 5.4 风控闭环

结合 `rewrite_guide.md` 和当前代码：

- 单腿失败回滚
- 持仓期间止损
- 持仓超时平仓
- APR 失效平仓
- close confirm + retry
- 发现非对冲仓位时进入 `ERROR`

---

## 6. 旧代码经验中应持续保留的模式

来自 `rewrite_guide.md` Part E，应优先保留：

- 并发双腿开平仓
- 单腿失败立即回滚
- Decimal/tick-aware 舍入
- 启动时 ensure accounts flat
- funding rate 缓存
- aggressive limit order + 可配置 `cross_pct`
- 平仓确认与重试
- 基于杠杆的自动止损模型
- SQLite 事务型状态持久化

同时应持续避免：

- 每次查询都新建临时 WS 连接
- 单文件过大导致不可维护
- 用 JSON 文件充当状态真源
- 策略参数硬编码
- 粗粒度全局限流
- 字符串状态缺少类型约束

---

## 7. 运行与验证入口

README 当前给出的常用入口：

### 启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp bot_config.json.example bot_config.json
python -m src.main
```

### 测试 / 验证

```bash
python tests/test_lighter.py --public
python tests/test_edgex.py --public
python tests/test_hyperliquid.py --public
python tests/test_grvt.py --public --env prod
python tests/test_scanner.py --min-apr 10 --max-spread 0.3
python tests/test_orchestrator.py
```

---

## 8. 后续可继续完善的方向

如果以本文件作为后续执行参考，建议优先级如下：

1. 将 `min_volume_usd` 在 scanner 中做完整落地
2. 继续加强 stale-data 场景下 OPENING / CLOSING 的保护
3. 补强 recovery / reconciliation 流程
4. 统一 close/open 的 fill-aware 审计与 PnL 计算
5. 增加更聚焦的 orchestrator / execution 回归测试

---

## 9. 结论

当前分支已经具备较清晰的 blueprint 结构：

- 配置、状态、交易所适配、策略核心、执行、恢复彼此分层
- SQLite 作为 SSOT + 审计中心
- 状态机驱动交易闭环
- 通过统一 adapter 接口支持多交易所扩展

而 `rewrite_guide.md` 可以继续作为：

- 需求规格说明（Part A）
- 代码骨架蓝图（Part B）
- 工程约束与实施指南（Part C）
- 经验库 / 风险提示（Part E）

`plan_b.md` 的作用就是把这些对当前分支最重要的信息压缩成一个可快速查阅的入口文档。
