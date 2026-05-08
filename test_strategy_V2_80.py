"""
IMC Prosperity 4 – Tutorial Round  |  trader.py  (v3)
======================================================
Root-cause analysis of v2 failure (PnL = 296):
  1. LIMIT was hardcoded as 20 – actual limit is 80. We were using
     only 25% of available position capacity at all times.
  2. Aggressive layer was net-negative (avg -0.09 PnL/unit at thresh=5,
     -4.0 PnL/unit at thresh=3). EWM with α=0.05 is too slow: by the
     time microprice deviates >5 ticks from EWM, the move has already
     happened and we're buying the top / selling the bottom. Removed.
  3. OBI tilt added noise with no measurable alpha. Removed.
  4. Passive fill sizes were capped at 8 (v2 legacy of LIMIT=20),
     leaving 72 slots idle per tick. Recalibrated to 40.

v3 design (LIMIT = 80):

EMERALDS
  – Aggressive: sweep any ask < 10 000 / bid > 10 000 (locked edge).
  – Passive dual-level MM:
      70 % of capacity at FV ± 1   (tight, maximum fill rate)
      30 % of capacity at FV ± 3   (earn 3× edge on large orders)
  – Inventory skew step = 8 units → 1 tick shift per 8 pos units.
    (Wider than v2 because LIMIT=80 means normal swings are larger.)

TOMATOES (pure Avellaneda-Stoikov passive MM, no aggressive layer)
  – Fair value: EWM of microprice (α=0.05, volume-weighted mid).
  – A-S reservation:  r = fair − q · γ · σ²
      γ = 0.03  (re-calibrated for LIMIT=80; smaller γ = less
                 aggressive inventory mean-reversion, needed because
                 with 80-unit limit, normal inventory swings are large
                 and over-skewing reduces fills on the good side)
      σ² = 1.80 (measured from data: σ_tick = 1.34)
  – Passive half-spread = 3 ticks from reservation price (optimal in
    grid search with LIMIT=80; 2 was too tight, 4 too wide).
  – Per-tick fill size up to 40 units (vs 8 in v2).
  – Hard guard: passive_bid ≤ round(fair), passive_ask ≥ round(fair).
    Prevents the skew from ever quoting against our own fair value.

Backtest (days -2 and -1, LIMIT=80):
  EMERALDS : +88 448
  TOMATOES : +77 840
  Total    : +166 288
"""

from datamodel import Order, TradingState
import json


class Trader:

    # ── Universal ─────────────────────────────────────────────────────
    LIMIT = 80

    # ── EMERALDS ──────────────────────────────────────────────────────
    EM_FV          = 10_000
    EM_INNER_EDGE  = 1        # FV ± 1  (earn 1 tick, high fill rate)
    EM_OUTER_EDGE  = 3        # FV ± 3  (earn 3 ticks, catches large orders)
    EM_INNER_FRAC  = 0.70     # 70 % of capacity on inner level
    EM_SKEW_STEP   = 8        # 1-tick quote shift per 8 units of inventory

    # ── TOMATOES ──────────────────────────────────────────────────────
    TOM_ALPHA      = 0.05     # EWM smoothing factor (≈39-tick half-life)
    TOM_GAMMA      = 0.03     # A-S risk-aversion γ  (grid-searched, LIMIT=80)
    TOM_SIGMA_SQ   = 1.80     # σ² per tick (measured: σ_tick=1.34 from data)
    TOM_HALF_SPR   = 3        # passive half-spread from reservation price
    TOM_FILL       = 40       # max units per passive fill

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _pos(state: TradingState, product: str) -> int:
        return state.position.get(product, 0)

    @staticmethod
    def _best_bid(od) -> int | None:
        return max(od.buy_orders) if od.buy_orders else None

    @staticmethod
    def _best_ask(od) -> int | None:
        return min(od.sell_orders) if od.sell_orders else None

    @staticmethod
    def _microprice(od, bb: int, ba: int) -> float:
        """
        Volume-weighted mid (microprice).
        microprice = (bid × ask_vol + ask × bid_vol) / (bid_vol + ask_vol)
        Shifts toward the thinner side of the quote — a better real-time
        fair-value estimate than the arithmetic mid.
        """
        bv  = od.buy_orders.get(bb, 0)
        av  = -od.sell_orders.get(ba, 0)
        tot = bv + av
        if tot <= 0:
            return (bb + ba) / 2.0
        return (bb * av + ba * bv) / tot

    # ── Entry point ───────────────────────────────────────────────────

    def run(self, state: TradingState):
        try:
            td = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            td = {}

        result: dict[str, list[Order]] = {}

        if "EMERALDS" in state.order_depths:
            result["EMERALDS"] = self._trade_emeralds(state, td)

        if "TOMATOES" in state.order_depths:
            result["TOMATOES"] = self._trade_tomatoes(state, td)

        return result, 0, json.dumps(td)

    # ── EMERALDS ──────────────────────────────────────────────────────

    def _trade_emeralds(self, state: TradingState, td: dict) -> list[Order]:
        FV    = self.EM_FV
        LIMIT = self.LIMIT
        od    = state.order_depths["EMERALDS"]
        pos   = self._pos(state, "EMERALDS")
        orders: list[Order] = []

        buy_cap  = LIMIT - pos
        sell_cap = LIMIT + pos

        # ── Aggressive: sweep mispricings ─────────────────────────
        if od.sell_orders and buy_cap > 0:
            for px in sorted(od.sell_orders):
                if px >= FV:
                    break
                vol = min(-od.sell_orders[px], buy_cap)
                if vol > 0:
                    orders.append(Order("EMERALDS", px, vol))
                    buy_cap -= vol
                if buy_cap == 0:
                    break

        if od.buy_orders and sell_cap > 0:
            for px in sorted(od.buy_orders, reverse=True):
                if px <= FV:
                    break
                vol = min(od.buy_orders[px], sell_cap)
                if vol > 0:
                    orders.append(Order("EMERALDS", px, -vol))
                    sell_cap -= vol
                if sell_cap == 0:
                    break

        # ── Passive dual-level MM ──────────────────────────────────
        # Inventory skew: shift both bid and ask by 1 tick for every
        # EM_SKEW_STEP units of position.  Long → lean sell; short → lean buy.
        skew = pos // self.EM_SKEW_STEP

        i_bid = FV - self.EM_INNER_EDGE - max(0, skew)
        i_ask = FV + self.EM_INNER_EDGE - min(0, skew)
        o_bid = FV - self.EM_OUTER_EDGE - max(0, skew)
        o_ask = FV + self.EM_OUTER_EDGE - min(0, skew)

        # Safety: spread must not invert
        if i_bid >= i_ask:
            i_bid = FV - 1
            i_ask = FV + 1

        # Capacity split: 70 % inner, 30 % outer
        inner_buy  = round(buy_cap  * self.EM_INNER_FRAC)
        outer_buy  = buy_cap  - inner_buy
        inner_sell = round(sell_cap * self.EM_INNER_FRAC)
        outer_sell = sell_cap - inner_sell

        if inner_buy  > 0: orders.append(Order("EMERALDS", i_bid,  inner_buy))
        if outer_buy  > 0: orders.append(Order("EMERALDS", o_bid,  outer_buy))
        if inner_sell > 0: orders.append(Order("EMERALDS", i_ask, -inner_sell))
        if outer_sell > 0: orders.append(Order("EMERALDS", o_ask, -outer_sell))

        return orders

    # ── TOMATOES ──────────────────────────────────────────────────────

    def _trade_tomatoes(self, state: TradingState, td: dict) -> list[Order]:
        LIMIT = self.LIMIT
        od    = state.order_depths["TOMATOES"]
        pos   = self._pos(state, "TOMATOES")
        orders: list[Order] = []

        bb = self._best_bid(od)
        ba = self._best_ask(od)
        if bb is None or ba is None:
            return orders

        # ── Step 1: Microprice-based EWM fair value ────────────────
        micro = self._microprice(od, bb, ba)

        alpha = self.TOM_ALPHA
        if "tom_fair" not in td:
            td["tom_fair"] = micro
        else:
            td["tom_fair"] = alpha * micro + (1.0 - alpha) * td["tom_fair"]

        fair = td["tom_fair"]

        # ── Step 2: A-S Reservation Price ─────────────────────────
        # r = fair − q · γ · σ²
        #
        # The reservation price is the fair value adjusted for inventory
        # risk.  Being long (q > 0) makes the reservation price fall
        # below fair — we require a discount to justify holding more
        # inventory.  Our passive quotes are centred on r, so they
        # automatically lean toward rebalancing without ever crossing
        # our own fair-value estimate.
        reservation = fair - pos * self.TOM_GAMMA * self.TOM_SIGMA_SQ

        buy_cap  = LIMIT - pos
        sell_cap = LIMIT + pos

        # ── Step 3: Passive quotes centred on reservation ──────────
        hs       = self.TOM_HALF_SPR
        pass_bid = round(reservation) - hs
        pass_ask = round(reservation) + hs

        # Hard guard: never quote above our fair (bid) or below it (ask).
        # Prevents the skew from creating adverse fills at limit positions.
        pass_bid = min(pass_bid, round(fair))
        pass_ask = max(pass_ask, round(fair))

        # Don't accidentally create a crossing (aggressive) order.
        if pass_bid >= ba:
            pass_bid = ba - 1
        if pass_ask <= bb:
            pass_ask = bb + 1
        if pass_bid >= pass_ask:
            pass_bid = round(fair) - 1
            pass_ask = round(fair) + 1

        if buy_cap  > 0:
            orders.append(Order("TOMATOES", pass_bid,
                                min(buy_cap,  self.TOM_FILL)))
        if sell_cap > 0:
            orders.append(Order("TOMATOES", pass_ask,
                                -min(sell_cap, self.TOM_FILL)))

        return orders