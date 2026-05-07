# Case Study 01 — SUI Funding Rate Arbitrage: First Live Trade

**Date:** 2026-05-07  
**Symbol:** SUI  
**Strategy:** Cross-exchange funding rate arbitrage  
**Status:** Completed (auto-closed by bot)

---

## 1. Setup

| Item | Value |
|------|-------|
| Long exchange | EdgeX |
| Short exchange | Lighter |
| Notional | ~$200 |
| Leverage | 1× |
| Entry net APR | 18.83% |
| Exit trigger | net APR < 10% threshold |

---

## 2. Trade Timeline

| Time (UTC+8) | Event |
|--------------|-------|
| 21:30:04 | Bot opened SUI long on EdgeX @ 0.9943, short on Lighter @ 0.9933 |
| 21:30 → 21:51 | HOLDING — net APR declined from 18.83% → 9.20% as Lighter funding rate converged from −7.88% → +1.75% |
| 21:51:51 | Bot detected net APR 9.20% < threshold 10% → triggered auto-close |
| 21:51:56 | EdgeX sold 201.2 SUI @ 0.9811, Lighter bought back 201.2 SUI @ 0.98102 |
| 21:51:56 | Scanner ran immediately, found SUI edgex/hyperliquid at 18.37% → opened new position |

**Hold duration:** 21.9 minutes

---

## 3. P&L Breakdown

### Price P&L (hedged)

| Leg | Entry | Exit | Size | P&L |
|-----|------:|-----:|-----:|----:|
| EdgeX long | 0.9943 | 0.9811 | 201.2 | −$2.66 |
| Lighter short | 0.9933 | 0.98102 | 201.2 | +$2.47 |
| **Net price P&L** | | | | **−$0.19** |

### Cost & Income

| Item | Amount |
|------|-------:|
| EdgeX fee (open + close) | −$0.143 |
| Lighter fee | $0.00 (free) |
| Funding income (22 min, avg APR ~14%) | +$0.001 |
| **Total P&L** | **−$0.33** |

**Return on notional:** −0.16%

---

## 4. Analysis

### What worked
- **Hedge was effective**: price P&L was nearly zero (−$0.19), confirming the delta-neutral structure held even during a ~1.3% SUI price move.
- **Auto-close triggered correctly**: the bot exited cleanly as soon as the edge disappeared, before the short leg turned from profit to loss.
- **Immediate re-entry**: after closing, the scanner found a new opportunity on edgex/hyperliquid at 18.37% within seconds — demonstrating the cycle works end-to-end.

### What caused the loss
- **Hold time too short**: 22 minutes is far too brief to accrue meaningful funding income. With $200 notional at 14% APR, you earn ~$0.001 per 22 minutes. Fees alone require ~1.7 hours of holding to break even.
- **Lighter funding rate converged rapidly**: the Lighter SUI funding rate moved from −7.88% to +1.75% in under 22 minutes, which is unusually fast. This compressed the edge before fees could be recovered.
- **Threshold too tight**: the 10% floor caused premature exit. A lower threshold (7–8%) would have kept the position alive longer, giving funding more time to accumulate.

### Fee break-even analysis

```
Break-even hold time = total_fee / (avg_notional × avg_net_apr / 8760)
                     = $0.143 / ($200 × 0.14 / 8760)
                     ≈ 44.8 hours  ← at 14% APR
                     ≈ 19.4 hours  ← at 18.83% APR (entry rate)
```

**Implication**: at $200 notional, EdgeX fees require ~19 hours of holding at entry APR just to break even. The current `hold_duration_hours = 4` is insufficient unless APR stays very high.

---

## 5. Recommendations

| Parameter | Current | Suggested | Reason |
|-----------|:-------:|:---------:|--------|
| `min_net_apr_threshold` | 10% | **7–8%** | Reduce premature exits; give funding time to accumulate |
| `notional_per_position` | $200 | **$500+** | Fee/notional ratio improves; $0.14 fee on $500 = 0.028% vs 0.072% on $200 |
| `hold_duration_hours` | 4h | **8–12h** | Target at least one full funding period (8h on most CEX/DEX) |
| `min_net_apr_threshold` entry | (scanner) | **15%+** | Only enter when spread is wide enough to cover fees at realistic hold time |

### Fee structure note
- EdgeX charges maker/taker fees — consider using limit orders (maker) to reduce fee from ~0.07% to ~0.02%
- Lighter is currently free — this makes it the preferred short leg whenever possible

---

## 6. Raw Data Reference

```
position_id : 1
symbol      : SUI
is_active   : 0 (closed)
exchange_long  : edgex
exchange_short : lighter
opened_at   : 2026-05-07T13:30:04Z
updated_at  : 2026-05-07T13:51:56Z

position_legs:
  id=1 edgex  long  201.2 entry=0.9943  close=0.9524*  unrealized=-2.48
  id=2 lighter short 201.2 entry=0.99338 close=1.01031* unrealized=+2.38

(* bot-recorded close_price differs from actual fill — see Section 3 for verified prices)

orders:
  edgex   OPEN  buy  201.2 fill=0.9943  notional=200.05  fee=0
  lighter OPEN  sell 201.2 fill=0.99338 notional=199.87  fee=0
  edgex   CLOSE sell 201.2 fill=0.9524* notional=191.62  fee=0
  lighter CLOSE buy  201.2 fill=1.01031* notional=203.27  fee=0

(actual verified fills: edgex close=0.9811, lighter close=0.98102)
```

---

*Next case: SUI round 2 (edgex/hyperliquid) — ongoing as of 2026-05-07 21:52*
