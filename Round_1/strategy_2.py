from datamodel import Order, TradingState
import json


class Trader:

    LIMIT = 80

    # IPR timing parameters
    IPR_EOD_START    = 900_000   # timestamp after which we liquidate IPR
    IPR_EOD_CAUTION  = 800_000   # timestamp after which we stop buying (coast)
    ACO_FV           = 10_000    # ACO mean-reversion anchor

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

        # Track timestamp for EOD detection and day transitions
        ts = state.timestamp
        last_ts = td.get("last_ts", -1)
        if ts < last_ts:
            # Timestamp reset → new day started
            td["day"] = td.get("day", 0) + 1
        td["last_ts"] = ts

        result: dict[str, list[Order]] = {}

        if "INTARIAN_PEPPER_ROOT" in state.order_depths:
            result["INTARIAN_PEPPER_ROOT"] = self._trade_ipr(state, td, ts)

        if "ASH_COATED_OSMIUM" in state.order_depths:
            result["ASH_COATED_OSMIUM"] = self._trade_aco(state, td)

        return result, 0, json.dumps(td)

    # ── INTARIAN_PEPPER_ROOT ──────────────────────────────────────────

    def _trade_ipr(self, state: TradingState, td: dict, ts: int) -> list[Order]:

        LIMIT = self.LIMIT
        od    = state.order_depths["INTARIAN_PEPPER_ROOT"]
        pos   = self._pos(state, "INTARIAN_PEPPER_ROOT")
        orders: list[Order] = []

        bb  = self._best_bid(od)
        ba  = self._best_ask(od)

        # Compute midpoint (FV proxy)
        if bb is not None and ba is not None:
            mid = (bb + ba) / 2.0
        elif bb is not None:
            mid = float(bb) + 6.5
        elif ba is not None:
            mid = float(ba) - 6.5
        else:
            return orders  # no book, skip

        buy_cap  = LIMIT - pos
        sell_cap = LIMIT + pos

        # ── PHASE 3: End-of-day liquidation ───────────────────────────
        if ts >= self.IPR_EOD_START:
            # Sell ALL inventory aggressively before day end
            if bb is not None and sell_cap > 0:
                # Hit the bid (market sell) — guaranteed fill
                orders.append(Order("INTARIAN_PEPPER_ROOT", bb, -sell_cap))
            elif ba is not None and sell_cap > 0:
                # If no bid in book, post sell at ba-1
                orders.append(Order("INTARIAN_PEPPER_ROOT", ba - 1, -sell_cap))
            return orders

        # ── PHASE 2: Hold (coast, stop accumulating) ───────────────────
        if ts >= self.IPR_EOD_CAUTION:
            # Don't buy or sell — just hold for trend appreciation
            # Optionally: passive bid to maintain position if knocked out
            if bb is not None and buy_cap > 0 and pos < LIMIT:
                orders.append(Order("INTARIAN_PEPPER_ROOT", bb + 1, buy_cap))
            return orders

        # ── PHASE 1: Active accumulation ───────────────────────────────

        # Layer 1: Aggressive sweep of asks below mid (free edge)
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

 
        if bb is not None and buy_cap > 0:
            bid_price = bb + 2
            # Guard: never bid above mid (would cross the spread)
            if bid_price < round(mid):
                orders.append(Order("INTARIAN_PEPPER_ROOT", bid_price, buy_cap))

        return orders

    # ── ASH_COATED_OSMIUM ─────────────────────────────────────────────

    def _trade_aco(self, state: TradingState, td: dict) -> list[Order]:

        LIMIT = self.LIMIT
        FV    = self.ACO_FV
        od    = state.order_depths["ASH_COATED_OSMIUM"]
        pos   = self._pos(state, "ASH_COATED_OSMIUM")
        orders: list[Order] = []

        bb = self._best_bid(od)
        ba = self._best_ask(od)

        buy_cap  = LIMIT - pos
        sell_cap = LIMIT + pos

        # Aggressive sweep
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

        # Passive live-book MM
        if bb is None or ba is None:
            return orders

        our_bid = bb + 1
        our_ask = ba - 1
        our_bid = min(our_bid, FV - 1)   # never quote above FV
        our_ask = max(our_ask, FV + 1)   # never quote below FV

        if our_bid >= our_ask:
            return orders

        if buy_cap  > 0: orders.append(Order("ASH_COATED_OSMIUM", our_bid,  buy_cap))
        if sell_cap > 0: orders.append(Order("ASH_COATED_OSMIUM", our_ask, -sell_cap))

        return orders