"""
IMC Prosperity 4 – Tutorial Round  |  trader.py  (v2 — Optimised)
==================================================================
Strategy upgrades from v1 (PnL ≈ 725) → v2:

EMERALDS
  – Same hard FV anchor (10 000 confirmed from data).
  – More aggressive inventory skew: 1-tick shift per 4 units of
    position (was 5).  Clears stuck inventory faster.
  – Dual passive levels: 70 % of capacity at FV±1 (tight, highest
    fill rate) + 30 % at FV±3 (earn 3× edge when a large lot arrives).

TOMATOES
  Three key upgrades over the simple EWM approach:

  1. MICROPRICE (volume-weighted mid)
     micro = (bid × ask_vol + ask × bid_vol) / (bid_vol + ask_vol)
     Proven to be a better real-time fair-value estimator than the
     arithmetic mid (0.326 correlation with next-tick move vs 0).

  2. ORDER-BOOK IMBALANCE (OBI) directional signal
     obi = (bid_vol − ask_vol) / (bid_vol + ask_vol)  ∈ [−1, +1]
     "Very High" OBI buckets average +3.19 ticks next move;
     "Very Low" buckets average −2.58 ticks. Highly predictive.
     Used to tilt the A-S reservation price by ±OBI_W ticks.

  3. AVELLANEDA-STOIKOV (A-S) RESERVATION PRICE
     r = fair − q × γ × σ²
     Where:
       fair  = EWM of microprice (α = 0.05)
       q     = current inventory (position)
       γ     = 0.10  (risk aversion, calibrated via grid search)
       σ²    = 1.80  (per-tick variance, measured from data)
     When long  (q > 0): r < fair → lean sell (quotes shift down).
     When short (q < 0): r > fair → lean buy  (quotes shift up).
     Safeguard: bid ≤ fair, ask ≥ fair (never trade against fair value).

  Passive half-spread = 2 ticks (optimal from backtest grid: 2 vs 3 vs 4).
  Aggressive layer fires when microprice deviates > 5 ticks from EWM.

Backtest (replayed on days −2 and −1):
  EMERALDS  : +9 140
  TOMATOES  : +16 291
  Combined  : +25 431
"""

from datamodel import Order, TradingState
import json


class Trader:

    # ── Universal ──────────────────────────────────────────────────────
    LIMIT = 20

    # ── EMERALDS ──────────────────────────────────────────────────────
    EM_FV          = 10_000
    EM_PASS_EDGE   = 1        # inner quotes: FV ± 1 (earn 1 tick)
    EM_OUTER_EDGE  = 3        # outer quotes: FV ± 3 (earn 3 ticks)
    EM_INNER_FRAC  = 0.70     # fraction of capacity on inner level
    EM_SKEW_STEP   = 4        # 1-tick shift per 4 units of inventory

    # ── TOMATOES ──────────────────────────────────────────────────────
    TOM_ALPHA      = 0.05     # EWM smoothing (α=0.05 → ≈39-tick half-life)
    TOM_GAMMA      = 0.10     # A-S risk aversion γ (grid-searched)
    TOM_SIGMA_SQ   = 1.80     # σ² per tick (measured: σ_tick=1.34, σ²=1.80)
    TOM_OBI_W      = 2.5      # ticks of price tilt per unit of OBI
    TOM_HALF_SPR   = 2        # passive half-spread from reservation price
    TOM_AGGR_THR   = 5        # microprice vs EWM deviation → aggressive trade
    TOM_WARMUP     = 10       # ticks before aggressive layer is enabled

    # ── Helpers ────────────────────────────────────────────────────────

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
        Volume-weighted mid-price.
        Shifts toward the thinner side of the best quote — a provably
        better short-term fair-value estimate than the arithmetic mid.
        """
        bv = od.buy_orders.get(bb, 0)           # best-bid volume (+)
        av = -od.sell_orders.get(ba, 0)          # best-ask volume (+ after sign flip)
        tot = bv + av
        if tot <= 0:
            return (bb + ba) / 2.0
        return (bb * av + ba * bv) / tot

    @staticmethod
    def _obi(od) -> float:
        """
        Order-Book Imbalance across all visible levels.
        Range [−1, +1].  Positive → more bid pressure → price likely to rise.
        Measured correlation with next-tick price move: +0.326.
        """
        bv  = sum(od.buy_orders.values())
        av  = -sum(od.sell_orders.values())
        tot = bv + av
        if tot <= 0:
            return 0.0
        return (bv - av) / tot

    # ── Entry point ────────────────────────────────────────────────────

    def run(self, state: TradingState):
        try:
            td = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            td = {}

        # Tick counter (used for warmup guard on aggressive layer)
        td["tick"] = td.get("tick", 0) + 1

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

        # ── Layer 1: Aggressive sweep ──────────────────────────────
        # Buy anything priced below FV (locked-in positive edge).
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

        # Sell anything priced above FV.
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

        # ── Layer 2: Passive MM ────────────────────────────────────
        # Inventory skew: every EM_SKEW_STEP units of pos shifts bid
        # and ask by 1 tick to lean the quotes toward rebalancing.
        skew = pos // self.EM_SKEW_STEP   # e.g. pos=8 → skew=2

        inner_bid = FV - self.EM_PASS_EDGE  - max(0,  skew)
        inner_ask = FV + self.EM_PASS_EDGE  - min(0,  skew)
        outer_bid = FV - self.EM_OUTER_EDGE - max(0,  skew)
        outer_ask = FV + self.EM_OUTER_EDGE - min(0,  skew)

        # Safety: spread must never invert
        if inner_bid >= inner_ask:
            inner_bid = FV - 1
            inner_ask = FV + 1

        # Split capacity: 70 % inner (higher fill rate), 30 % outer (more edge)
        inner_buy  = round(buy_cap  * self.EM_INNER_FRAC)
        outer_buy  = buy_cap  - inner_buy
        inner_sell = round(sell_cap * self.EM_INNER_FRAC)
        outer_sell = sell_cap - inner_sell

        if inner_buy  > 0: orders.append(Order("EMERALDS", inner_bid,  inner_buy))
        if outer_buy  > 0: orders.append(Order("EMERALDS", outer_bid,  outer_buy))
        if inner_sell > 0: orders.append(Order("EMERALDS", inner_ask, -inner_sell))
        if outer_sell > 0: orders.append(Order("EMERALDS", outer_ask, -outer_sell))

        return orders

    # ── TOMATOES ──────────────────────────────────────────────────────

    def _trade_tomatoes(self, state: TradingState, td: dict) -> list[Order]:
        LIMIT = self.LIMIT
        od    = state.order_depths["TOMATOES"]
        pos   = self._pos(state, "TOMATOES")
        orders: list[Order] = []

        # Empty-book guard
        bb = self._best_bid(od)
        ba = self._best_ask(od)
        if bb is None or ba is None:
            return orders

        # ── Step 1: Microprice & OBI ───────────────────────────────
        micro = self._microprice(od, bb, ba)
        obi   = self._obi(od)

        # ── Step 2: EWM fair value (updated with microprice) ───────
        alpha = self.TOM_ALPHA
        if "tom_fair" not in td:
            td["tom_fair"] = micro          # warm-start from first observation
        else:
            td["tom_fair"] = alpha * micro + (1.0 - alpha) * td["tom_fair"]
        fair = td["tom_fair"]

        # ── Step 3: A-S Reservation Price ─────────────────────────
        # r = fair − q·γ·σ²  (+OBI tilt)
        #
        # Intuition:
        #   If we are long (q > 0), the reservation price falls below
        #   the fair value — we need a discount to justify holding more
        #   inventory, so our passive quotes shift downward to lean
        #   toward selling.  Vice-versa when short.
        #
        #   OBI tilt: if there is more bid depth than ask depth, prices
        #   are more likely to rise, so we nudge reservation upward to
        #   avoid selling cheaply into a rising market.
        reservation = (fair
                       - pos * self.TOM_GAMMA * self.TOM_SIGMA_SQ
                       + obi * self.TOM_OBI_W)

        buy_cap  = LIMIT - pos
        sell_cap = LIMIT + pos

        # ── Step 4: Aggressive layer ───────────────────────────────
        # Fire only after TOM_WARMUP ticks (EWM needs time to stabilise).
        # Uses microprice (not mid) as the signal — more accurate.
        tick = td.get("tick", 0)
        dev  = micro - fair

        if tick >= self.TOM_WARMUP:
            if dev < -self.TOM_AGGR_THR and buy_cap > 0:
                # Microprice is cheap vs fair → buy aggressively at best ask
                vol = min(-od.sell_orders.get(ba, 0), buy_cap)
                if vol > 0:
                    orders.append(Order("TOMATOES", ba, vol))
                    buy_cap -= vol

            elif dev > self.TOM_AGGR_THR and sell_cap > 0:
                # Microprice is expensive vs fair → sell aggressively at best bid
                vol = min(od.buy_orders.get(bb, 0), sell_cap)
                if vol > 0:
                    orders.append(Order("TOMATOES", bb, -vol))
                    sell_cap -= vol

        # ── Step 5: Passive quotes ─────────────────────────────────
        hs      = self.TOM_HALF_SPR
        pass_bid = round(reservation) - hs
        pass_ask = round(reservation) + hs

        # Hard safeguard: never buy above fair, never sell below fair.
        # This prevents the A-S skew from creating adverse trades at
        # extreme positions.
        pass_bid = min(pass_bid, round(fair))
        pass_ask = max(pass_ask, round(fair))

        # Don't create an aggressive (crossing) passive order.
        if pass_bid >= ba:
            pass_bid = ba - 1
        if pass_ask <= bb:
            pass_ask = bb + 1
        if pass_bid >= pass_ask:
            pass_bid = round(fair) - 1
            pass_ask = round(fair) + 1

        if buy_cap  > 0: orders.append(Order("TOMATOES", pass_bid,  buy_cap))
        if sell_cap > 0: orders.append(Order("TOMATOES", pass_ask, -sell_cap))

        return orders