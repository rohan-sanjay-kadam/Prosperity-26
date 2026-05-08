from datamodel import Order, TradingState
from typing import List, Dict

POSITION_LIMIT = 200

class Trader:

    def run(self, state: TradingState):
        result = {}

        product = "HYDROGEL_PACK"
        if product not in state.order_depths:
            return result, 0, ""

        order_depth = state.order_depths[product]
        position = state.position.get(product, 0)

        orders: List[Order] = []

        # ---------------------------
        # Extract order book
        # ---------------------------
        if not order_depth.buy_orders or not order_depth.sell_orders:
            return result, 0, ""

        best_bid = max(order_depth.buy_orders.keys())
        best_ask = min(order_depth.sell_orders.keys())

        bid_volume = order_depth.buy_orders[best_bid]
        ask_volume = order_depth.sell_orders[best_ask]

        mid_price = (best_bid + best_ask) / 2

        # ---------------------------
        # Inventory skew
        # ---------------------------
        inventory_skew = position / POSITION_LIMIT  # -1 to 1

        # More aggressive if near flat
        base_size = 20
        size = int(base_size * (1 - abs(inventory_skew)))
        size = max(5, size)

        # ---------------------------
        # Quote prices
        # ---------------------------
        spread = best_ask - best_bid

        if spread > 1:
            bid_price = best_bid + 1
            ask_price = best_ask - 1
        else:
            # No edge in crossing tight spread blindly
            bid_price = best_bid
            ask_price = best_ask

        # ---------------------------
        # Inventory-based adjustments
        # ---------------------------
        if position > 100:
            # Too long → prioritize selling
            ask_price = max(ask_price - 1, best_bid)
            size = int(size * 1.5)

        elif position < -100:
            # Too short → prioritize buying
            bid_price = min(bid_price + 1, best_ask)
            size = int(size * 1.5)

        # ---------------------------
        # Place orders
        # ---------------------------
        buy_qty = min(size, POSITION_LIMIT - position)
        sell_qty = min(size, POSITION_LIMIT + position)

        if buy_qty > 0:
            orders.append(Order(product, bid_price, buy_qty))

        if sell_qty > 0:
            orders.append(Order(product, ask_price, -sell_qty))

        result[product] = orders

        return result, 0, ""