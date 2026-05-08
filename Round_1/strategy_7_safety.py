

from datamodel import Order, TradingState
import json


class Trader:

    LIMIT = 80
    ACO_FV = 10_000

    # IPR trailing stop parameters
    IPR_EWM_ALPHA   = 0.10   # smoothing (9-tick lag, fast enough to track trend)
    IPR_STOP_THRESH = 25     # ticks below EWM → stop triggered (11σ from noise)

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

        result: dict[str, list[Order]] = {}

        if "INTARIAN_PEPPER_ROOT" in state.order_depths:
            result["INTARIAN_PEPPER_ROOT"] = self._trade_ipr(state, td)

        if "ASH_COATED_OSMIUM" in state.order_depths:
            result["ASH_COATED_OSMIUM"] = self._trade_aco(state)

        return result, 0, json.dumps(td)

    # ── INTARIAN_PEPPER_ROOT ──────────────────────────────────────────

    def _trade_ipr(self, state: TradingState, td: dict) -> list[Order]:
 
        LIMIT = self.LIMIT
        od    = state.order_depths["INTARIAN_PEPPER_ROOT"]
        pos   = self._pos(state, "INTARIAN_PEPPER_ROOT")
        orders: list[Order] = []

        bb = self._best_bid(od)
        ba = self._best_ask(od)

        # Compute midpoint for EWM update
        if bb is not None and ba is not None:
            mid = (bb + ba) / 2.0
        elif bb is not None:
            mid = float(bb) + 6.5
        elif ba is not None:
            mid = float(ba) - 6.5
        else:
            return orders  # empty book, skip

        # Update EWM (persisted across ticks)
        alpha = self.IPR_EWM_ALPHA
        if "ipr_ewm" not in td:
            td["ipr_ewm"] = mid
        else:
            td["ipr_ewm"] = alpha * mid + (1.0 - alpha) * td["ipr_ewm"]
        ewm = td["ipr_ewm"]

        # ── STOP-LOSS CHECK ────────────────────────────────────────
        deviation = mid - ewm  # negative = price falling below EWM
        already_stopped = td.get("ipr_stopped", False)

        if not already_stopped and deviation < -self.IPR_STOP_THRESH and pos > 0:
            # Trend has broken: sell everything at bid (guaranteed fill)
            td["ipr_stopped"] = True
            if bb is not None:
                orders.append(Order("INTARIAN_PEPPER_ROOT", bb, -pos))
            return orders

        # Stay flat if previously stopped
        if already_stopped:
            return orders

        # ── HOLDING (pos = LIMIT, trend intact) ───────────────────
        if pos >= LIMIT:
            return []  # silent hold — optimal

        # ── ACCUMULATING (pos < LIMIT) ────────────────────────────
        buy_cap = LIMIT - pos

        # Layer 1: Sweep any ask strictly below mid (pure edge)
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

        # Layer 2: Lift ask_price_1 (fills ~11 units immediately)
        if ba is not None and buy_cap > 0:
            vol1 = min(-od.sell_orders.get(ba, 0), buy_cap)
            if vol1 > 0:
                orders.append(Order("INTARIAN_PEPPER_ROOT", ba, vol1))
                buy_cap -= vol1

        # Layer 3: Lift deeper ask levels (fills ~20 more units)
        if buy_cap > 0 and od.sell_orders:
            for px in sorted(od.sell_orders):
                if px == ba:
                    continue
                if ba is not None and px > ba + 6:
                    break
                vol = min(-od.sell_orders[px], buy_cap)
                if vol > 0:
                    orders.append(Order("INTARIAN_PEPPER_ROOT", px, vol))
                    buy_cap -= vol
                if buy_cap == 0:
                    return orders

        # Layer 4: Passive bid for remaining capacity (1 tick above ask)
        if buy_cap > 0:
            passive_level = (ba + 1) if ba is not None else (bb + 2 if bb else 0)
            if bb is not None:
                passive_level = min(passive_level, bb + 10)
            if passive_level > 0:
                orders.append(Order("INTARIAN_PEPPER_ROOT", passive_level, buy_cap))

        return orders

    # ── ASH_COATED_OSMIUM ─────────────────────────────────────────────

    def _trade_aco(self, state: TradingState) -> list[Order]:

        LIMIT = self.LIMIT
        FV    = self.ACO_FV
        od    = state.order_depths["ASH_COATED_OSMIUM"]
        pos   = self._pos(state, "ASH_COATED_OSMIUM")
        orders: list[Order] = []

        bb = self._best_bid(od)
        ba = self._best_ask(od)

        buy_cap  = LIMIT - pos
        sell_cap = LIMIT + pos

        # Aggressive sweep: locked-in mispricing
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

        our_bid = min(bb + 1, FV - 1)
        our_ask = max(ba - 1, FV + 1)
        if our_bid >= our_ask:
            return orders

        # Size-based inventory skew
        pos_frac  = pos / LIMIT
        buy_frac  = max(0.15, 1.0 - max(0.0,  pos_frac))
        sell_frac = max(0.15, 1.0 - max(0.0, -pos_frac))

        buy_post  = max(1, round(buy_cap  * buy_frac))  if buy_cap  > 0 else 0
        sell_post = max(1, round(sell_cap * sell_frac)) if sell_cap > 0 else 0

        if buy_post  > 0: orders.append(Order("ASH_COATED_OSMIUM", our_bid,  buy_post))
        if sell_post > 0: orders.append(Order("ASH_COATED_OSMIUM", our_ask, -sell_post))

        return orders