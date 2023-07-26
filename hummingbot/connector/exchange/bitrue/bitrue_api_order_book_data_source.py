import asyncio
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

import gzip
import json

import hummingbot.connector.exchange.bitrue.bitrue_constants as CONSTANTS
from hummingbot.connector.exchange.bitrue import bitrue_web_utils as web_utils
from hummingbot.connector.exchange.bitrue.bitrue_order_book import BitrueOrderBook
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.exchange.bitrue.bitrue_exchange import BitrueExchange


class BitrueAPIOrderBookDataSource(OrderBookTrackerDataSource):
    HEARTBEAT_TIME_INTERVAL = 30.0
    TRADE_STREAM_ID = 1
    DIFF_STREAM_ID = 2
    ONE_HOUR = 60 * 60

    _logger: Optional[HummingbotLogger] = None
    _trading_pair_symbol_map: Dict[str, Mapping[str, str]] = {}
    _mapping_initialization_lock = asyncio.Lock()

    def __init__(self,
                 trading_pairs: List[str],
                 connector: 'BitrueExchange',
                 api_factory: Optional[WebAssistantsFactory] = None,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN,
                 throttler: Optional[AsyncThrottler] = None,
                 time_synchronizer: Optional[TimeSynchronizer] = None):
        super().__init__(trading_pairs)
        self._connector = connector
        self._diff_messages_queue_key = CONSTANTS.DIFF_EVENT_TYPE
        self._domain = domain
        self._time_synchronizer = time_synchronizer
        self._throttler = throttler
        self._api_factory = api_factory or web_utils.build_api_factory(
            throttler=self._throttler,
            time_synchronizer=self._time_synchronizer,
            domain=self._domain,
        )
        self._message_queue: Dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        self._last_ws_message_sent_timestamp = 0

    async def get_last_traded_prices(self,
                                     trading_pairs: List[str],
                                     domain: Optional[str] = None) -> Dict[str, float]:
        return await self._connector.get_last_traded_prices(trading_pairs=trading_pairs)

    async def _request_order_book_snapshot(self, trading_pair: str) -> Dict[str, Any]:
        """
        Retrieves a copy of the full order book from the exchange, for a particular trading pair.

        :param trading_pair: the trading pair for which the order book will be retrieved

        :return: the response from the exchange (JSON dictionary)
        """
        params = {
            # "symbol": await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair),
            "symbol": "MNTLUSDT",
            "limit": "1000"
        }

        data = await self._connector._api_request(path_url=CONSTANTS.ORDER_BOOK,
                                                  method=RESTMethod.GET,
                                                  params=params)

        return data

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        snapshot: Dict[str, Any] = await self._request_order_book_snapshot(trading_pair)
        # snapshot_timestamp: float = float(snapshot["lastUpdateId"]) * 1e-3
        # snapshot_timestamp = snapshot["lastUpdateId"]
        snapshot_timestamp: float = time.time()
        snapshot_msg: OrderBookMessage = BitrueOrderBook.snapshot_message_from_exchange_rest(
            snapshot,
            snapshot_timestamp,
            metadata={"trading_pair": trading_pair}
        )
        # print(f"snapshot_msg ===========> {snapshot_msg}")
        return snapshot_msg

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol=raw_message["symbol"])
        for trades in raw_message["data"]:
            trade_message: OrderBookMessage = BitrueOrderBook.trade_message_from_exchange(
                trades, {"trading_pair": trading_pair})
            message_queue.put_nowait(trade_message)

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol=raw_message["symbol"])
        for diff_message in raw_message["data"]:
            order_book_message: OrderBookMessage = BitrueOrderBook.diff_message_from_exchange(
                diff_message, diff_message["t"], {"trading_pair": trading_pair})
            message_queue.put_nowait(order_book_message)

    async def listen_for_order_book_snapshots(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
        """
        This method runs continuously and request the full order book content from the exchange every hour.
        The method uses the REST API from the exchange because it does not provide an endpoint to get the full order
        book through websocket. With the information creates a snapshot messages that is added to the output queue
        :param ev_loop: the event loop the method will run in
        :param output: a queue to add the created snapshot messages
        """
        while True:
            try:
                await asyncio.wait_for(self._process_ob_snapshot(snapshot_queue=output), timeout=self.ONE_HOUR)
            except asyncio.TimeoutError:
                await self._take_full_order_book_snapshot(trading_pairs=self._trading_pairs, snapshot_queue=output)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error.", exc_info=True)
                await self._take_full_order_book_snapshot(trading_pairs=self._trading_pairs, snapshot_queue=output)
                await self._sleep(5.0)

    async def listen_for_subscriptions(self):
        """
        Connects to the trade events and order diffs websocket endpoints and listens to the messages sent by the
        exchange. Each message is stored in its own queue.
        """
        print("websocket = inside listen_for_subscriptions func")
        ws = None
        while True:
            try:
                ws: WSAssistant = await self._api_factory.get_ws_assistant()
                print(f"ws =======> {ws}")
                await ws.connect(ws_url=CONSTANTS.WSS_URL)
                await self._subscribe_channels(ws)
                self._last_ws_message_sent_timestamp = self._time()

                while True:
                    try:
                        seconds_until_next_ping = (CONSTANTS.WS_HEARTBEAT_TIME_INTERVAL - (
                            self._time() - self._last_ws_message_sent_timestamp))

                        await asyncio.wait_for(self._process_ws_messages(ws=ws), timeout=seconds_until_next_ping)
                    except asyncio.TimeoutError:
                        ping_time = self._time()
                        payload = {
                            "ping": int(ping_time * 1e3)
                        }
                        print("inside exception code.................")
                        ping_request = WSJSONRequest(payload=payload)
                        await ws.send(request=ping_request)
                        self._last_ws_message_sent_timestamp = ping_time
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error(
                    "Unexpected error occurred when listening to order book streams. Retrying in 5 seconds...",
                    exc_info=True,
                )
                await self._sleep(5.0)
            finally:
                ws and await ws.disconnect()

    async def _subscribe_channels(self, ws: WSAssistant):
        """
        Subscribes to the trade events and diff orders events through the provided websocket connection.
        :param ws: the websocket assistant used to connect to the exchange
        """
        print(f"websocket inside _subscribe_channels func")
        # print(f"self trading_pairs ========> {self._trading_pairs}")
        try:
            for trading_pair in self._trading_pairs:
                # symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
                symbol = "MNTL-USDT"
                # print(f"symbol printing =======> {symbol}")
                # trade_payload = {
                #     "topic": "trade",
                #     "event": "sub",
                #     "symbol": symbol,
                #     "params": {
                #         "binary": False
                #     }
                # }
                # trade_payload = {
                #     "event":"sub",
                #     "params":{
                #         "cb_id":"mntlusdt",
                #         "channel":"market_mntlusdt_simple_depth_step0"
                #     }
                # }
#
                # subscribe_trade_request: WSJSONRequest = WSJSONRequest(payload=trade_payload)

                # depth_payload = {
                #     "topic": "diffDepth",
                #     "event": "sub",
                #     "symbol": symbol,
                #     "params": {
                #         "binary": False
                #     }
                # }
                depth_payload = {
                    "event":"sub",
                    "params":{
                        "cb_id":"mntlusdt",
                        "channel":"market_mntlusdt_simple_depth_step0"
                    }
                }
                subscribe_orderbook_request: WSJSONRequest = WSJSONRequest(payload=depth_payload)

                # await ws.send(subscribe_trade_request)
                await ws.send(subscribe_orderbook_request)

                self.logger().info(f"Subscribed to public order book and trade channels of {trading_pair}...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().error(
                "Unexpected error occurred subscribing to order book trading and delta streams...",
                exc_info=True
            )
            raise

    async def _process_ws_messages(self, ws: WSAssistant):
        print("inside _process_ws_messages func")
        async for ws_response in ws.iter_messages():
            data = ws_response.data
            dec_gzipped_data = gzip.decompress(data)
            my_json = dec_gzipped_data.decode('utf8').replace("'", '"')

            # Load the JSON to a Python list & dump it back out as formatted
            JSONdata = json.loads(my_json)
            if JSONdata.get("status") == "ok":
                continue

            self._message_queue[CONSTANTS.SNAPSHOT_EVENT_TYPE].put_nowait(JSONdata)


    async def _process_ob_snapshot(self, snapshot_queue: asyncio.Queue):
        print("inside _process_ob_snapshot function")
        message_queue = self._message_queue[CONSTANTS.SNAPSHOT_EVENT_TYPE]
        while True:
            try:
                json_msg = await message_queue.get()
                # trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(
                #     symbol=json_msg["symbol"])
                trading_pair = "MNTL-USDT"
                print(f"json_msg =======> {json_msg}")
                order_book_message: OrderBookMessage = BitrueOrderBook.snapshot_message_from_exchange_websocket(
                    json_msg, json_msg["ts"], {"trading_pair": trading_pair})
                snapshot_queue.put_nowait(order_book_message)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error when processing public order book updates from exchange")
                raise

    async def _take_full_order_book_snapshot(self, trading_pairs: List[str], snapshot_queue: asyncio.Queue):
        print("inside _take_full_order_book_snapshot function")
        for trading_pair in trading_pairs:
            try:
                snapshot: Dict[str, Any] = await self._request_order_book_snapshot(trading_pair=trading_pair)
                # snapshot_timestamp = snapshot["lastUpdateId"]
                snapshot_timestamp: float = time.time()
                snapshot_msg: OrderBookMessage = BitrueOrderBook.snapshot_message_from_exchange_rest(
                    snapshot,
                    snapshot_timestamp,
                    metadata={"trading_pair": trading_pair}
                )
                # print(f"snapshot_msg ========> {snapshot_msg}")
                snapshot_queue.put_nowait(snapshot_msg)
                self.logger().debug(f"Saved order book snapshot for {trading_pair}")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error(f"Unexpected error fetching order book snapshot for {trading_pair}.",
                                    exc_info=True)
                await self._sleep(5.0)

    def _time(self):
        return time.time()
