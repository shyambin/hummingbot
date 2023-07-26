from typing import Dict, Optional

from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType


class BitrueOrderBook(OrderBook):
    @classmethod
    def snapshot_message_from_exchange_websocket(cls,
                                                 msg: Dict[str, any],
                                                 timestamp: float,
                                                 metadata: Optional[Dict] = None) -> OrderBookMessage:
        """
        Creates a snapshot message with the order book snapshot message
        :param msg: the response from the exchange when requesting the order book snapshot
        :param timestamp: the snapshot timestamp
        :param metadata: a dictionary with extra information to add to the snapshot data
        :return: a snapshot message with the snapshot information received from the exchange
        """
        print("inside snapshot_message_from_exchange_websocket")
        print(f"message ======> {msg}")
        if metadata:
            msg.update(metadata)
        ts = msg["ts"]
        return OrderBookMessage(OrderBookMessageType.SNAPSHOT, {
            "trading_pair": msg["trading_pair"],
            "update_id": ts,
            "bids": msg["tick"]["buys"],
            "asks": msg["tick"]["asks"]
        }, timestamp=timestamp)

    @classmethod
    def snapshot_message_from_exchange_rest(cls,
                                            msg: Dict[str, any],
                                            timestamp: float,
                                            metadata: Optional[Dict] = None) -> OrderBookMessage:
        """
        Creates a snapshot message with the order book snapshot message
        :param msg: the response from the exchange when requesting the order book snapshot
        :param timestamp: the snapshot timestamp
        :param metadata: a dictionary with extra information to add to the snapshot data
        :return: a snapshot message with the snapshot information received from the exchange
        """
        if metadata:
            msg.update(metadata)
        ts = msg["lastUpdateId"]
        parsed_data = {
            "trading_pair": msg["trading_pair"],
            "update_id": ts,
            # "bids": msg["bids"],
            # "asks": msg["asks"]
            "bids": [[bid[0], bid[1]] for bid in msg["bids"]],
            "asks": [[ask[0], ask[1]] for ask in msg["asks"]],

        }
        snapshot_timestamp = timestamp
        order_book_msg: OrderBookMessage = OrderBookMessage(
            OrderBookMessageType.SNAPSHOT, parsed_data, snapshot_timestamp
        )
        # return OrderBookMessage(OrderBookMessageType.SNAPSHOT, {
        #     "trading_pair": msg["trading_pair"],
        #     "update_id": ts,
        #     # "bids": msg["bids"],
        #     # "asks": msg["asks"]
        #     "bids": [(bid[0], bid[1]) for bid in msg["bids"]],
        #     "asks": [(ask[0], ask[1]) for ask in msg["asks"]],
        # }, timestamp=timestamp)
        return order_book_msg

    @classmethod
    def diff_message_from_exchange(cls,
                                   msg: Dict[str, any],
                                   timestamp: Optional[float] = None,
                                   metadata: Optional[Dict] = None) -> OrderBookMessage:
        """
        Creates a diff message with the changes in the order book received from the exchange
        :param msg: the changes in the order book
        :param timestamp: the timestamp of the difference
        :param metadata: a dictionary with extra information to add to the difference data
        :return: a diff message with the changes in the order book notified by the exchange
        """
        print("inside diff_message_from_exchange func")
        if metadata:
            msg.update(metadata)
        ts = msg["t"]
        return OrderBookMessage(OrderBookMessageType.DIFF, {
            "trading_pair": msg["trading_pair"],
            "update_id": ts,
            "bids": msg["b"],
            "asks": msg["a"]
        }, timestamp=timestamp)

    @classmethod
    def trade_message_from_exchange(cls, msg: Dict[str, any], metadata: Optional[Dict] = None):
        """
        Creates a trade message with the information from the trade event sent by the exchange
        :param msg: the trade event details sent by the exchange
        :param metadata: a dictionary with extra information to add to trade message
        :return: a trade message with the details of the trade as provided by the exchange
        """
        print("inside trade_message_from_exchange func")
        if metadata:
            msg.update(metadata)
        ts = msg["t"]
        return OrderBookMessage(OrderBookMessageType.TRADE, {
            "trading_pair": msg["trading_pair"],
            "trade_type": float(TradeType.BUY.value) if msg["m"] else float(TradeType.SELL.value),
            "trade_id": ts,
            "update_id": ts,
            "price": msg["p"],
            "amount": msg["q"]
        }, timestamp=ts * 1e-3)
