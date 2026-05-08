"""
IMC Prosperity 4 – Round 1  |  trader.py  (v5)
================================================
Products  : INTARIAN_PEPPER_ROOT (IPR) | ASH_COATED_OSMIUM (ACO)
Position limits: 80 each

WHAT THE VISUALISER IMAGES CONFIRMED
──────────────────────────────────────────────────────────────────

IMAGE 1 — IPR Position & Own Fills:
  Multi-level sweep is working: pos reaches 80 by tick 3.
    t=0:   11@12,006 + 20@12,009 = 31 units (L1+L2 lifts)
    t=100: 20@12,010 (passive bid fills)
    t=200: 10@12,007 + 19@12,010 → pos=80 ✓
  
  Trade Momentum = 0 VOL. Zero taker-buys confirmed across entire session.
  → The MM overlay (sell at ba-1, rebuy at bb+2) added in v3 never fires.
    Removed in v4. Stays removed in v5.
  
  POSITION DIP at day boundary = EOD sell triggering on TRAINING days.
  We sold 80→0 at ts=900,000 on day -2 and -1, then swept the ask to
  rebuild the next day. This roundtrip costs:
    sell at bb (-6.5 ticks) + rebuy at ba (+9 ticks) = ~1,240/day
    Over 2 training days: ~2,480 XIREC thrown away.
  FIX (v5): Remove EOD entirely. Hold position across all day boundaries.
  Once at pos=80 → post ZERO orders. Max efficiency.

IMAGE 2 — ACO Book at tick 495 (50% into submission):
  Bot bid=9,990, Bot ask=10,006. Mid=9,998 (floats, not pinned at 10,000).
  Our bid=9,991, Our ask=10,005. Price priority confirmed ✓.

IMAGE 3 — ACO Position + Own Fills at tick 221 (22% in):
  PROBLEM: Position drifted +10 → -60. Pattern: short bias in this session.
  Fill table shows 4 SELL fills vs 1 BUY fill in a 27-tick window.
  Root cause: taker-BUY flow dominated this session window → our asks fill
  repeatedly → we keep going shorter → sell_cap drains to 20 → even more
  fill capacity lost.
  
  Fill prices confirmed correct:
    BUY @ 9,991 = bb+1 (bb=9,990) → price priority ✓
    SELL @ 10,003 = ba-1 (ba=10,004) → price priority ✓
    SELL @ 10,008 = ba-1 (ba=10,009) → price priority ✓
  Prices are right. Volume allocation is the issue.

  FIX (v5): INVENTORY SIZE-SKEW on ACO.
  When short (pos<0): post only a fraction of sell_cap at ask.
  When long (pos>0): post only a fraction of buy_cap at bid.
  Formula (pure SIZE, NOT price — avoids the v5 tutorial round bug):
    buy_frac  = max(0.15, 1 - max(0, pos/LIMIT))
    sell_frac = max(0.15, 1 - max(0, -pos/LIMIT))
  
  At pos=-60: buy_frac=1.0 (buy 140 → aggressive rebalance),
              sell_frac=0.25 (post only 5 units at ask → stop digging deeper)
  At pos=0:   both 1.0 (symmetric, full capacity)
  At pos=+60: sell_frac=1.0, buy_frac=0.25 (stop buying when very long)

EXPECTED IMPROVEMENT:
  IPR: Remove EOD roundtrip waste → +2,480 on training days
  ACO: Size skew → prevents drift to limits → more symmetric fill capacity
  Target: 10,500+
"""

from datamodel import Order, TradingState
import json


class Trader:

    LIMIT  = 80
    ACO_FV = 10_000

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
        td["last_ts"] = state.timestamp

        result: dict[str, list[Order]] = {}

        if "INTARIAN_PEPPER_ROOT" in state.order_depths:
            result["INTARIAN_PEPPER_ROOT"] = self._trade_ipr(state)

        if "ASH_COATED_OSMIUM" in state.order_depths:
            result["ASH_COATED_OSMIUM"] = self._trade_aco(state)

        return result, 0, json.dumps(td)

    # ── INTARIAN_PEPPER_ROOT ──────────────────────────────────────────

    def _trade_ipr(self, state: TradingState) -> list[Order]:
        """
        Pure trend-follower. Hold max long (80) from first possible tick,
        never sell.

        Strategy:
          If pos < 80: sweep all available ask levels to accumulate as
            fast as possible. L1 + L2 fills ~32 units in tick 0.
            Passive bid at the next level fills the remainder over 2-3 ticks.
          If pos == 80: return EMPTY list. Post absolutely nothing.
            This is the most efficient state — we hold 80 units, capture
            the full 0.001/tick appreciation with zero order overhead.

        Why no EOD liquidation (removed in v5):
          EOD on training days caused a needless sell+rebuy roundtrip costing
          ~1,240 XIREC per day. Position carries across day boundaries in
          Prosperity, so holding across is free.
          Submission day (ts max ≈ 99,900) never reaches EOD_START=900,000
          anyway, so the guard was irrelevant there too.
          Risk of trend reversal: low (R²=0.9999 over 30,000 ticks). If
          it happens, ACO's symmetric MM provides partial buffer.

        Why no sell-side orders:
          100% of IPR trades are taker-sells (confirmed from data).
          Zero taker-buys means our ask orders never fill. They are dead
          weight that consumes order slots. Removed.
        """
        LIMIT  = self.LIMIT
        od     = state.order_depths["INTARIAN_PEPPER_ROOT"]
        pos    = self._pos(state, "INTARIAN_PEPPER_ROOT")

        # At position limit: hold perfectly, post nothing
        if pos >= LIMIT:
            return []

        bb = self._best_bid(od)
        ba = self._best_ask(od)

        if bb is None and ba is None:
            return []

        orders: list[Order] = []
        buy_cap = LIMIT - pos
        mid = (bb + ba) / 2.0 if (bb and ba) else float(ba or bb)

        # ── Layer 1: sweep any ask strictly below mid (pure edge) ──
        if od.sell_orders and buy_cap > 0:
            for px in sorted(od.sell_orders):
                if px >= round(mid):
                    break
                vol = min(-od.sell_orders[px], buy_cap)
                if vol > 0:
                    orders.append(Order("INTARIAN_PEPPER_ROOT", px, vol))
                    buy_cap -= vol
                if buy_cap == 0:
                    return orders

        # ── Layer 2: lift ask_price_1 (fills ~12 units immediately) ─
        if ba is not None and buy_cap > 0:
            vol1 = min(-od.sell_orders.get(ba, 0), buy_cap)
            if vol1 > 0:
                orders.append(Order("INTARIAN_PEPPER_ROOT", ba, vol1))
                buy_cap -= vol1

        # ── Layer 3: lift deeper ask levels (fills ~20 more units) ──
        if buy_cap > 0 and od.sell_orders:
            for px in sorted(od.sell_orders):
                if px == ba:
                    continue           # already handled
                if ba is not None and px > ba + 6:
                    break              # don't chase too far
                vol = min(-od.sell_orders[px], buy_cap)
                if vol > 0:
                    orders.append(Order("INTARIAN_PEPPER_ROOT", px, vol))
                    buy_cap -= vol
                if buy_cap == 0:
                    return orders

        # ── Layer 4: passive bid for remaining capacity ────────────
        # Sits 1 tick above the current ask → fills on next taker-sell.
        # Gets us from ~51 to 80 units in the next 1-2 ticks.
        if buy_cap > 0:
            passive_level = (ba + 1) if ba is not None else (bb + 2)
            if bb is not None:
                passive_level = min(passive_level, bb + 10)  # safety cap
            orders.append(Order("INTARIAN_PEPPER_ROOT", passive_level, buy_cap))

        return orders

    # ── ASH_COATED_OSMIUM ─────────────────────────────────────────────

    def _trade_aco(self, state: TradingState) -> list[Order]:
        """
        Symmetric passive MM with inventory size-skew.

        PRICES (unchanged from v4):
          bid = bb + 1   (1 tick above best bid → guaranteed priority)
          ask = ba - 1   (1 tick below best ask → guaranteed priority)
          Confirmed from fills: buy @ 9,991 (=bb+1), sell @ 10,003/10,008 (=ba-1)

        SIZE-SKEW (new in v5):
          When pos < 0 (short): reduce ask volume proportionally.
            → Post less at ask when already short, preventing the position
              from drifting deeper toward -80.
          When pos > 0 (long): reduce bid volume proportionally.
            → Post less at bid when already long.
          
          Formula:
            buy_frac  = max(0.15, 1 - max(0, pos/LIMIT))
            sell_frac = max(0.15, 1 - max(0, -pos/LIMIT))
          
          At pos=0:   buy_frac=1.0, sell_frac=1.0 (full symmetric)
          At pos=-60: buy_frac=1.0, sell_frac=0.25 (lean buy hard)
          At pos=+60: buy_frac=0.25, sell_frac=1.0 (lean sell hard)
          
          0.15 floor ensures we always show some presence on both sides
          so we don't completely abandon a side.

        NO outer level (confirmed dead: all ACO trade sizes ≤ 10 < bot bid vol 14).
        NO directional assumption (size skew is symmetric around 0).
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

        # ── Aggressive sweep: locked-in mispricing ─────────────────
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

        # ── Inner-level quotes with FV guard ──────────────────────
        our_bid = min(bb + 1, FV - 1)
        our_ask = max(ba - 1, FV + 1)
        if our_bid >= our_ask:
            return orders

        # ── Inventory size-skew ────────────────────────────────────
        pos_frac = pos / LIMIT   # range [-1, +1]

        # buy_frac: full when short (need to buy back), reduced when long
        buy_frac  = max(0.15, 1.0 - max(0.0,  pos_frac))
        # sell_frac: full when long (need to sell back), reduced when short
        sell_frac = max(0.15, 1.0 - max(0.0, -pos_frac))

        buy_post  = max(1, round(buy_cap  * buy_frac))  if buy_cap  > 0 else 0
        sell_post = max(1, round(sell_cap * sell_frac)) if sell_cap > 0 else 0

        if buy_post  > 0: orders.append(Order("ASH_COATED_OSMIUM", our_bid,  buy_post))
        if sell_post > 0: orders.append(Order("ASH_COATED_OSMIUM", our_ask, -sell_post))

        return orders