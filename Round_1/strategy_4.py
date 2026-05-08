"""
IMC Prosperity 4 – Round 1  |  trader.py  (v4)
================================================
Products  : INTARIAN_PEPPER_ROOT (IPR) | ASH_COATED_OSMIUM (ACO)
Pos limits: 80 each

CONFIRMED FINDINGS FROM TRADE DATA
────────────────────────────────────────────────────────────────────
IPR — 100% of trades are taker-SELLS. Zero taker-buys ever.
  Consequences for our strategy:
  (a) MM overlay (sell at ba-1) is dead code. Removed.
  (b) Our ask orders are irrelevant — no one ever lifts them.
  (c) The ONLY lever is the BUY side: accumulate 80 units and hold.

  V3 bug: lifting at ba (=ask_price_1) only fills ask_volume_1 ≈ 12
  units per tick. We need to sweep ALL three ask levels simultaneously
  to fill ~51 units in the first tick:
    Buy at ask_price_1 → fills ask_volume_1 ≈ 12 units
    Buy at ask_price_2 → fills ask_volume_2 ≈ 20 units
    Buy remaining at ask_price_2+1 → passive, fills next tick via taker

  This closes the average shortfall from 76 → ~79+ units.

ACO — All trade sizes ≤ 10 units. Bot bid1_volume ≈ 14 units.
  Consequences:
  (a) No single trade ever sweeps level 1 (10 < 14). Outer level
      at bb-1/ba+1 is dead code. Removed.
  (b) Keep only inner bb+1 / ba-1 (guaranteed priority, 7-tick edge).
  (c) Clean, simple, no wasted order slots.

EOD Liquidation (IPR):
  Sell exactly `pos` units (not sell_cap). BUG from v2 fixed.
  EOD_START = 900,000 — well above submission day (≈99,900 max ts).
  So EOD never fires on submission day = correct, we hold all day.
"""

from datamodel import Order, TradingState
import json


class Trader:

    LIMIT         = 80
    ACO_FV        = 10_000
    IPR_EOD_START = 900_000   # only relevant for training days

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
        Pure trend-follower: hold maximum long (80 units) at all times.

        IPR has ONLY taker-sell flow (confirmed from trade data). This means:
          • Our ask orders never fill → no MM overlay needed
          • We accumulate purely by being the best bid when takers sell

        Multi-level ask sweep (key v4 fix):
          Post BID at each ask level to fill all available ask depth:
          - Bid at ask_price_1 → immediate fill of ask_volume_1 ≈ 12 units
          - Bid at ask_price_2 → immediate fill of ask_volume_2 ≈ 20 units
          Together: ~32 units per tick when asks are available.
          Plus passive bid at ask_price_1 + 1 for remaining capacity.

        Hold at pos=80:
          When at position limit, post nothing (no sell side needed).
          This differs from v3 which wasted a sell order that never fired.

        EOD liquidation (ts ≥ 900,000):
          Hits the bid to exit exactly `pos` units (not sell_cap!).
          Only relevant on training days. Submission day tops out at ~99,900 ts.
        """
        LIMIT = self.LIMIT
        od    = state.order_depths["INTARIAN_PEPPER_ROOT"]
        pos   = self._pos(state, "INTARIAN_PEPPER_ROOT")
        orders: list[Order] = []

        bb = self._best_bid(od)
        ba = self._best_ask(od)

        if bb is None and ba is None:
            return orders

        buy_cap = LIMIT - pos

        # ── EOD: flatten position before day end ───────────────────
        if ts >= self.IPR_EOD_START and pos > 0:
            if bb is not None:
                orders.append(Order("INTARIAN_PEPPER_ROOT", bb, -pos))  # sell exactly pos
            return orders

        # Already at limit → hold, do nothing
        if buy_cap <= 0:
            return orders

        mid = (bb + ba) / 2.0 if (bb and ba) else (ba or bb or 0.0)

        # ── Layer 1: Sweep asks strictly below mid (pure edge) ─────
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

        # ── Layer 2: Lift Level 1 ask (ask_price_1) ────────────────
        # Crossing the spread gives us the highest price priority.
        # Fills ask_volume_1 ≈ 12 units immediately.
        if ba is not None and buy_cap > 0:
            vol_at_ba = min(-od.sell_orders.get(ba, 0), buy_cap)
            if vol_at_ba > 0:
                orders.append(Order("INTARIAN_PEPPER_ROOT", ba, vol_at_ba))
                buy_cap -= vol_at_ba

        # ── Layer 3: Lift Level 2 ask (ask_price_2) ────────────────
        # Fills ask_volume_2 ≈ 20 additional units.
        if buy_cap > 0 and od.sell_orders:
            ask_prices_sorted = sorted(od.sell_orders.keys())
            for ask_px in ask_prices_sorted:
                if ask_px == ba:
                    continue  # already handled in layer 2
                if ask_px > ba + 5:
                    break     # don't chase too far up the book
                vol = min(-od.sell_orders[ask_px], buy_cap)
                if vol > 0:
                    orders.append(Order("INTARIAN_PEPPER_ROOT", ask_px, vol))
                    buy_cap -= vol
                if buy_cap == 0:
                    return orders

        # ── Layer 4: Passive bid at ba+1 for remaining capacity ────
        # Sits just above any standing asks → next taker sell fills us.
        if bb is not None and buy_cap > 0:
            passive_bid = (ba + 1) if ba is not None else (bb + 2)
            # Guard: don't go above a reasonable level (avoid runaway)
            if bb is not None:
                passive_bid = min(passive_bid, bb + 10)
            orders.append(Order("INTARIAN_PEPPER_ROOT", passive_bid, buy_cap))

        return orders

    # ── ASH_COATED_OSMIUM ─────────────────────────────────────────────

    def _trade_aco(self, state: TradingState) -> list[Order]:
        """
        Clean symmetric passive MM at bb+1 / ba-1.

        Outer level removed (v4 fix): all ACO trade sizes ≤ 10 units,
        bot bid1_volume ≈ 14 — so no trade ever sweeps to level 2.
        The outer bb-1/ba+1 level in v3 never filled once.

        Pure inner-level quoting:
          bid = bb + 1  (1 tick above best bid → guaranteed priority)
          ask = ba - 1  (1 tick below best ask → guaranteed priority)
          Edge per fill ≈ 7 ticks per side.
          Natural capacity skew manages inventory automatically.

        Aggressive sweep:
          Any ask < FV=10,000 or bid > FV=10,000 is free edge. Sweep first.
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

        # ── Inner-level passive MM ─────────────────────────────────
        our_bid = min(bb + 1, FV - 1)
        our_ask = max(ba - 1, FV + 1)

        if our_bid >= our_ask:
            return orders

        if buy_cap  > 0: orders.append(Order("ASH_COATED_OSMIUM", our_bid,  buy_cap))
        if sell_cap > 0: orders.append(Order("ASH_COATED_OSMIUM", our_ask, -sell_cap))

        return orders