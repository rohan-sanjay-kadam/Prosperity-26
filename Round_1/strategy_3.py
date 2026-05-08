"""
IMC Prosperity 4 – Round 1  |  trader.py  (v3)
================================================
Products  : INTARIAN_PEPPER_ROOT (IPR) | ASH_COATED_OSMIUM (ACO)
Pos limits: 80 each

DIAGNOSIS OF V2 FAILURE
────────────────────────────────────────────────────────────────────
BUG 1 — IPR bid guard was silently suppressing orders:
  Code: `if bid_price < round(mid): orders.append(...)`
  When bb=9998, ba=10002 → mid=10000.0 → bb+2=10000 = round(mid)
  Condition fails → NO BID POSTED at all that tick.
  Fix: guard against crossing the ask (`bid_price < ba`), not mid.

BUG 2 — EOD sell size was DOUBLE the position:
  Code: `orders.append(Order(..., -sell_cap))`
  sell_cap = LIMIT + pos = 80 + 80 = 160 when pos=+80.
  This would drive us from +80 → -80 (a catastrophic short!).
  Fix: sell exactly `pos` units to reach flat.

SUBMISSION DAY STRUCTURE (critical insight from images)
────────────────────────────────────────────────────────────────────
Training days: 10,000 ticks each (ts 0 → 999,900)
Submission day: ~1,000 ticks (ts 0 → ~99,900) — confirmed by
  "TICK 104/997" visible in image 4.

This explains the PnL targets:
  IPR rises ~100 ticks in 1000 submission ticks.
  Long 80 from tick 0: 80 × (100 - 6.5 spread cost) = 7,480 ≈ target 7,350.
  → MUST reach pos=80 in first 1-2 ticks, not slowly.

COMBINED STRATEGY (buy-and-hold + market-making)
────────────────────────────────────────────────────────────────────
IPR:
  Lifting the ask immediately is the RIGHT move. Every tick we spend
  below 80 units costs 0.001×100×(80-pos) in forgone appreciation.
  Waiting to fill passively at bb+1 (with all bots ahead in queue)
  means we might never reach 80. So:
  
  Phase 1 — Aggressive lift (any tick where pos < LIMIT):
    Post BID at ba (= current best ask). This crosses the spread
    and fills immediately against the standing ask. Yes it costs
    6.5 extra ticks vs mid, but we get filled THIS tick, not "eventually".
    Also post bid at bb+2 as secondary (fills via taker-sell flow).
  
  Phase 2 — MM overlay when at LIMIT:
    Post SELL at ba-1 (earn sell-side edge).
    Post BUY at bb+2 (re-buy if sell fills, maintaining trend exposure).
    Net: earn ~(ba-1)-(bb+2) = spread-3 ≈ 10 ticks per completed cycle.
    Risk: tiny (1 unit out of position for 1-2 ticks).
    This turns idle max-long position into active MM income.

  End-of-day liquidation (ts > EOD_START = 900,000):
    Sell exactly `pos` units at bb to exit flat before day end.
    Insurance against trend reversal. Cost ≈ 13 ticks × 80 = 1,040.
    Benefit: protection from potential -240,000 reversal.

ACO:
  Keep the working live-book MM (bb+1/ba-1) but add a SECOND outer
  level to capture taker orders that sweep past the inner level.
  Inner level (bb+1/ba-1): high fill rate, 7-tick edge.
  Outer level (bb-1/ba+1, wider depth): catches larger sweeps.
  This pushes capture rate from ~53% toward ~60%+ without any 
  directional assumption.

Backtest target (1000-tick submission day):
  IPR  : ~7,480  (80 units × 94 ticks price rise)
  ACO  : ~3,150  (dual-level fill rate improvement)
  TOTAL: ~10,630
"""

from datamodel import Order, TradingState
import json


class Trader:

    LIMIT = 80

    # IPR parameters
    IPR_EOD_START = 900_000   # ts after which we aggressively flatten

    # ACO parameters
    ACO_FV        = 10_000

    @staticmethod
    def _pos(state: TradingState, product: str) -> int:
        return state.position.get(product, 0)

    @staticmethod
    def _best_bid(od) -> int | None:
        return max(od.buy_orders) if od.buy_orders else None

    @staticmethod
    def _best_ask(od) -> int | None:
        return min(od.sell_orders) if od.sell_orders else None

    def run(self, state: TradingState):
        try:
            td = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            td = {}

        # Day transition detection
        ts = state.timestamp
        td["last_ts"] = ts

        result: dict[str, list[Order]] = {}

        if "INTARIAN_PEPPER_ROOT" in state.order_depths:
            result["INTARIAN_PEPPER_ROOT"] = self._trade_ipr(state, ts)

        if "ASH_COATED_OSMIUM" in state.order_depths:
            result["ASH_COATED_OSMIUM"] = self._trade_aco(state)

        return result, 0, json.dumps(td)

    # ── INTARIAN_PEPPER_ROOT ──────────────────────────────────────────

    def _trade_ipr(self, state: TradingState, ts: int) -> list[Order]:
        """
        Combined trend-following + market-making:

        ACCUMULATION (pos < LIMIT):
          Bid at ba (lift the ask). This crosses the spread for immediate
          fill. Yes it costs ~6.5 extra ticks vs mid, but every tick at
          pos<80 forfeits 0.001 in price appreciation. With only 1000
          ticks on submission day, passive accumulation is too slow.
          Also post bb+2 as secondary bid to catch taker-sell flow.

        MM OVERLAY (pos = LIMIT):
          Post SELL at ba-1 (earn sell edge, ~6.5 ticks above mid).
          Post BUY at bb+2  (re-buy ready, ~4.5 ticks below mid).
          Cycle earns (ba-1)-(bb+2) ≈ 10 ticks per completed round trip.
          Only 1 unit turns over at a time — trend exposure stays at ≥79.

        EOD LIQUIDATION (ts ≥ 900,000):
          Sell exactly `pos` units at bb (hit the bid) to reach flat.
          Insurance against day-end trend reversal at cost of ~13 ticks/unit.
        """
        LIMIT = self.LIMIT
        od    = state.order_depths["INTARIAN_PEPPER_ROOT"]
        pos   = self._pos(state, "INTARIAN_PEPPER_ROOT")
        orders: list[Order] = []

        bb = self._best_bid(od)
        ba = self._best_ask(od)

        if bb is None and ba is None:
            return orders

        mid = ((bb or 0) + (ba or 0)) / 2.0 if (bb and ba) else (bb or ba or 0.0)
        buy_cap  = LIMIT - pos
        sell_cap = LIMIT + pos

        # ── EOD: flatten before day end ────────────────────────────
        if ts >= self.IPR_EOD_START and pos > 0:
            if bb is not None:
                # Hit the bid — guaranteed fill, sell exactly our position
                orders.append(Order("INTARIAN_PEPPER_ROOT", bb, -pos))
            return orders

        # ── Sweep any ask strictly below mid (pure edge) ───────────
        if od.sell_orders and buy_cap > 0:
            for px in sorted(od.sell_orders):
                if px >= round(mid):
                    break
                vol = min(-od.sell_orders[px], buy_cap)
                if vol > 0:
                    orders.append(Order("INTARIAN_PEPPER_ROOT", px, vol))
                    buy_cap -= vol
                if buy_cap == 0:
                    break

        # ── MM overlay when at position limit ──────────────────────
        if pos >= LIMIT:
            if ba is not None and sell_cap > 0:
                # Sell 1 unit at ba-1 (best ask minus 1 — price priority)
                orders.append(Order("INTARIAN_PEPPER_ROOT", ba - 1, -1))
            if bb is not None:
                # Stand ready to immediately rebuy at bb+2
                orders.append(Order("INTARIAN_PEPPER_ROOT", bb + 2, 1))
            return orders

        # ── Aggressive accumulation: lift the ask ──────────────────
        if ba is not None and buy_cap > 0:
            # Bid at ba: crosses the spread → immediate fill against standing ask.
            # Cost: ~6.5 ticks above mid. Worth it to capture trend from tick 0.
            orders.append(Order("INTARIAN_PEPPER_ROOT", ba, buy_cap))

        # ── Secondary: passive bid at bb+2 (beats bb+1 bots) ──────
        # Serves as a backup fill source via taker-sell flow,
        # and catches remaining volume if the ask was partially filled.
        if bb is not None and buy_cap > 0:
            bid2 = bb + 2
            if ba is None or bid2 < ba:  # only if it doesn't cross the spread
                orders.append(Order("INTARIAN_PEPPER_ROOT", bid2, buy_cap))

        return orders

    # ── ASH_COATED_OSMIUM ─────────────────────────────────────────────

    def _trade_aco(self, state: TradingState) -> list[Order]:
        """
        Dual-level passive MM around FV = 10,000.

        Level 1 (inner, bb+1/ba-1): maximum fill rate, 7-tick edge.
          Posts at the tightest inside price — guaranteed priority.
          Fills most of the available taker flow.

        Level 2 (outer, bb-1/ba+1): catches large sweeps.
          When a large taker order exhausts the inner level, the outer
          level captures the overflow at a wider spread (9-tick edge).
          This adds coverage without any directional assumption.

        Aggressive sweep: any ask < 10,000 or bid > 10,000 is swept
        first (locked-in edge, no risk).
        """
        LIMIT  = self.LIMIT
        FV     = self.ACO_FV
        od     = state.order_depths["ASH_COATED_OSMIUM"]
        pos    = self._pos(state, "ASH_COATED_OSMIUM")
        orders: list[Order] = []

        bb = self._best_bid(od)
        ba = self._best_ask(od)

        buy_cap  = LIMIT - pos
        sell_cap = LIMIT + pos

        # ── Aggressive sweep ───────────────────────────────────────
        if od.sell_orders and buy_cap > 0:
            for px in sorted(od.sell_orders):
                if px >= FV:
                    break
                vol = min(-od.sell_orders[px], buy_cap)
                if vol > 0:
                    orders.append(Order("ASH_COATED_OSMIUM", px, vol))
                    buy_cap -= vol
                if buy_cap == 0:
                    break

        if od.buy_orders and sell_cap > 0:
            for px in sorted(od.buy_orders, reverse=True):
                if px <= FV:
                    break
                vol = min(od.buy_orders[px], sell_cap)
                if vol > 0:
                    orders.append(Order("ASH_COATED_OSMIUM", px, -vol))
                    sell_cap -= vol
                if sell_cap == 0:
                    break

        if bb is None or ba is None:
            return orders

        # ── Level 1: inner quotes (bb+1 / ba-1) ───────────────────
        inner_bid = min(bb + 1, FV - 1)
        inner_ask = max(ba - 1, FV + 1)
        if inner_bid >= inner_ask:
            return orders

        # Allocate 70% of capacity to inner level
        inner_buy  = max(1, round(buy_cap  * 0.70))
        inner_sell = max(1, round(sell_cap * 0.70))
        outer_buy  = buy_cap  - inner_buy
        outer_sell = sell_cap - inner_sell

        if inner_buy  > 0: orders.append(Order("ASH_COATED_OSMIUM", inner_bid,  inner_buy))
        if inner_sell > 0: orders.append(Order("ASH_COATED_OSMIUM", inner_ask, -inner_sell))

        # ── Level 2: outer quotes (bb-1 / ba+1) ───────────────────
        # Catches taker orders that sweep through the inner level.
        # 2-tick wider from inner = 1 tick outside the bot quotes.
        # Only post if there is remaining capacity.
        outer_bid = max(bb - 1, FV - 15)   # cap depth — don't quote too far
        outer_ask = min(ba + 1, FV + 15)

        if outer_bid < inner_bid and outer_buy  > 0:
            orders.append(Order("ASH_COATED_OSMIUM", outer_bid,  outer_buy))
        if outer_ask > inner_ask and outer_sell > 0:
            orders.append(Order("ASH_COATED_OSMIUM", outer_ask, -outer_sell))

        return orders