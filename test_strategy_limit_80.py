from datamodel import Order, TradingState
import json


class Trader:

    # ── constants ──────────────────────────────────────────────────────
    LIMIT          = 80
    EM_FV          = 10_000
    EM_PASS_EDGE   = 1
    EM_SKEW_STEP   = 5

    TOM_ALPHA      = 0.05
    TOM_THRESH     = 5
    TOM_PASS_EDGE  = 4
    TOM_SKEW_STEP  = 5

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _pos(state: TradingState, product: str) -> int:
        return state.position.get(product, 0)

    @staticmethod
    def _best_bid(order_depth) -> int | None:
        return max(order_depth.buy_orders) if order_depth.buy_orders else None

    @staticmethod
    def _best_ask(order_depth) -> int | None:
        return min(order_depth.sell_orders) if order_depth.sell_orders else None

    # ── main entry point ───────────────────────────────────────────────

    def run(self, state: TradingState):
        try:
            td = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            td = {}

        result: dict[str, list[Order]] = {}

        # NOTE: correct attribute is order_depths (PLURAL)
        if "EMERALDS" in state.order_depths:
            result["EMERALDS"] = self._trade_emeralds(state, td)

        if "TOMATOES" in state.order_depths:
            result["TOMATOES"] = self._trade_tomatoes(state, td)

        return result, 0, json.dumps(td)

    # ── EMERALDS ───────────────────────────────────────────────────────

    def _trade_emeralds(self, state: TradingState, td: dict) -> list[Order]:
        FV    = self.EM_FV
        LIMIT = self.LIMIT
        orders: list[Order] = []
        od    = state.order_depths["EMERALDS"]
        pos   = self._pos(state, "EMERALDS")

        buy_cap  = LIMIT - pos
        sell_cap = LIMIT + pos

        # Layer 1: Aggressive — sweep any ask strictly below FV
        if od.sell_orders and buy_cap > 0:
            for ask_px in sorted(od.sell_orders.keys()):
                if ask_px >= FV:
                    break
                vol = min(-od.sell_orders[ask_px], buy_cap)
                if vol > 0:
                    orders.append(Order("EMERALDS", ask_px, vol))
                    buy_cap -= vol
                if buy_cap == 0:
                    break

        # Layer 1: Aggressive — sweep any bid strictly above FV
        if od.buy_orders and sell_cap > 0:
            for bid_px in sorted(od.buy_orders.keys(), reverse=True):
                if bid_px <= FV:
                    break
                vol = min(od.buy_orders[bid_px], sell_cap)
                if vol > 0:
                    orders.append(Order("EMERALDS", bid_px, -vol))
                    sell_cap -= vol
                if sell_cap == 0:
                    break

        # Layer 2: Passive — post best-bid / best-ask with inventory skew
        skew = pos // self.EM_SKEW_STEP

        pass_bid = FV - self.EM_PASS_EDGE - max(0,  skew)
        pass_ask = FV + self.EM_PASS_EDGE - min(0,  skew)

        if pass_bid >= pass_ask:
            pass_bid = FV - 1
            pass_ask = FV + 1

        if buy_cap > 0:
            orders.append(Order("EMERALDS", pass_bid,  buy_cap))
        if sell_cap > 0:
            orders.append(Order("EMERALDS", pass_ask, -sell_cap))

        return orders

    # ── TOMATOES ───────────────────────────────────────────────────────

    def _trade_tomatoes(self, state: TradingState, td: dict) -> list[Order]:
        LIMIT = self.LIMIT
        orders: list[Order] = []
        od    = state.order_depths["TOMATOES"]
        pos   = self._pos(state, "TOMATOES")

        best_bid = self._best_bid(od)
        best_ask = self._best_ask(od)
        if best_bid is None or best_ask is None:
            return orders

        mid = (best_bid + best_ask) / 2.0

        # Update EWM fair value (persisted across ticks via traderData)
        alpha = self.TOM_ALPHA
        if "tom_fair" not in td:
            td["tom_fair"] = mid
        else:
            td["tom_fair"] = alpha * mid + (1.0 - alpha) * td["tom_fair"]

        fair = td["tom_fair"]

        buy_cap  = LIMIT - pos
        sell_cap = LIMIT + pos

        # Layer 1: Aggressive — cross spread on strong deviations
        deviation = mid - fair

        if deviation < -self.TOM_THRESH and buy_cap > 0:
            vol = min(-od.sell_orders[best_ask], buy_cap)
            if vol > 0:
                orders.append(Order("TOMATOES", best_ask, vol))
                buy_cap -= vol

        elif deviation > self.TOM_THRESH and sell_cap > 0:
            vol = min(od.buy_orders[best_bid], sell_cap)
            if vol > 0:
                orders.append(Order("TOMATOES", best_bid, -vol))
                sell_cap -= vol

        # Layer 2: Passive — limit orders around EWM fair value with skew
        skew = pos // self.TOM_SKEW_STEP

        pass_bid = round(fair) - self.TOM_PASS_EDGE - max(0,  skew)
        pass_ask = round(fair) + self.TOM_PASS_EDGE - min(0,  skew)

        if pass_bid >= best_ask:
            pass_bid = best_ask - 1
        if pass_ask <= best_bid:
            pass_ask = best_bid + 1
        if pass_bid >= pass_ask:
            pass_bid = round(fair) - 1
            pass_ask = round(fair) + 1

        if buy_cap > 0:
            orders.append(Order("TOMATOES", pass_bid,  buy_cap))
        if sell_cap > 0:
            orders.append(Order("TOMATOES", pass_ask, -sell_cap))

        return orders