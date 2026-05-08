
from datamodel import Order, TradingState
import json


class Trader:

    LIMIT   = 80
    ACO_FV  = 10_000    # fixed mean-reversion anchor for ASH_COATED_OSMIUM

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

    # ── Entry point ───────────────────────────────────────────────────

    def run(self, state: TradingState):
        try:
            td = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            td = {}

        result: dict[str, list[Order]] = {}

        if "INTARIAN_PEPPER_ROOT" in state.order_depths:
            result["INTARIAN_PEPPER_ROOT"] = self._trade_ipr(state, td)

        if "ASH_COATED_OSMIUM" in state.order_depths:
            result["ASH_COATED_OSMIUM"] = self._trade_aco(state, td)

        return result, 0, json.dumps(td)

    # ── INTARIAN_PEPPER_ROOT ──────────────────────────────────────────

    def _trade_ipr(self, state: TradingState, td: dict) -> list[Order]:
       
        LIMIT  = self.LIMIT
        od     = state.order_depths["INTARIAN_PEPPER_ROOT"]
        pos    = self._pos(state, "INTARIAN_PEPPER_ROOT")
        orders: list[Order] = []

        bb  = self._best_bid(od)
        ba  = self._best_ask(od)
        mid = None

        if bb is not None and ba is not None:
            mid = (bb + ba) / 2.0
        elif bb is not None:
            mid = float(bb)
        elif ba is not None:
            mid = float(ba)

        if mid is None:
            return orders

        buy_cap  = LIMIT - pos
        sell_cap = LIMIT + pos

        # ── Layer 1: Aggressive — sweep asks below mid (below FV) ─────
        if od.sell_orders and buy_cap > 0:
            for px in sorted(od.sell_orders):
                if px >= round(mid):
                    break   # nothing below fair value
                vol = min(-od.sell_orders[px], buy_cap)
                if vol > 0:
                    orders.append(Order("INTARIAN_PEPPER_ROOT", px, vol))
                    buy_cap -= vol
                if buy_cap == 0:
                    break


        if bb is not None and buy_cap > 0:
            orders.append(Order("INTARIAN_PEPPER_ROOT", bb + 1, buy_cap))

        if ba is not None and sell_cap > 0 and pos >= LIMIT:
            orders.append(Order("INTARIAN_PEPPER_ROOT", ba - 1, -1))

        return orders

    # ── ASH_COATED_OSMIUM ─────────────────────────────────────────────

    def _trade_aco(self, state: TradingState, td: dict) -> list[Order]:
 
        LIMIT  = self.LIMIT
        FV     = self.ACO_FV
        od     = state.order_depths["ASH_COATED_OSMIUM"]
        pos    = self._pos(state, "ASH_COATED_OSMIUM")
        orders: list[Order] = []

        bb = self._best_bid(od)
        ba = self._best_ask(od)

        buy_cap  = LIMIT - pos
        sell_cap = LIMIT + pos

        # ── Aggressive: sweep locked-in mispricings ────────────────
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

        # ── Passive: live-book quoting with FV guard ───────────────
        if bb is None or ba is None:
            return orders

        our_bid = bb + 1
        our_ask = ba - 1

        # Never quote above FV on buy side or below FV on sell side
        our_bid = min(our_bid, FV - 1)
        our_ask = max(our_ask, FV + 1)

        # Safety: spread can't invert
        if our_bid >= our_ask:
            return orders

        if buy_cap  > 0: orders.append(Order("ASH_COATED_OSMIUM", our_bid,  buy_cap))
        if sell_cap > 0: orders.append(Order("ASH_COATED_OSMIUM", our_ask, -sell_cap))

        return orders