import asyncio
import logging
import time

from const import (
    ERROR_LOOKBACK_MIN,
    MAX_ERROR_COUNT,
    MAX_RETRY_ATTEMPT,
    UDPC_UPDATE_CHECK_TIMEOUT,
)
from juicebox_telnet import JuiceboxTelnet

_LOGGER = logging.getLogger(__name__)


class JuiceboxUDPCUpdater:
    def __init__(
        self,
        juicebox_host,
        jpp_host,
        telnet_port,
        udpc_port=8047,
        telnet_timeout=None,
        loglevel=None,
        mqtt_handler=None,
    ):
        if loglevel is not None:
            _LOGGER.setLevel(loglevel)
        self._juicebox_host = juicebox_host
        self._jpp_host = jpp_host
        self._udpc_port = udpc_port
        self._telnet_port = telnet_port
        self._telnet_timeout = telnet_timeout
        self._default_sleep_interval = 30
        self._udpc_update_loop_task = None
        self._telnet = None
        self._error_count = 0
        self._error_timestamp_list = []
        self._stop_event = asyncio.Event()
        self._supervisor_task = None
        self._mqtt_handler = mqtt_handler

    async def start(self):
        _LOGGER.info("Starting JuiceboxUDPCUpdater")
        if self._mqtt_handler:
            await self._mqtt_handler.publish_task_status(
                "juicebox_udpcupdater", "JuiceboxUDPCUpdater Starting"
            )
        # Run a resilient supervisor loop that keeps trying until closed
        while not self._stop_event.is_set():
            try:
                connected = await self._connect()
                if connected and self._udpc_update_loop_task is not None:
                    await self._udpc_update_loop_task
                else:
                    # Couldn't start the loop; quick retry
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                # Graceful shutdown
                break
            except ChildProcessError as e:
                _LOGGER.warning(
                    "JuiceboxUDPCUpdater encountered an error, will retry. "
                    f"({e.__class__.__qualname__}: {e})"
                )
                if self._mqtt_handler:
                    await self._mqtt_handler.publish_task_status(
                        "juicebox_udpcupdater",
                        "JuiceboxUDPCUpdater encountered an error, will retry.",
                    )
                await self._add_error()
                await asyncio.sleep(5)
            except Exception as e:
                _LOGGER.exception(
                    f"JuiceboxUDPCUpdater unexpected error; retrying. {e.__class__.__qualname__}: {e}"
                )
                if self._mqtt_handler:
                    await self._mqtt_handler.publish_task_status(
                        "juicebox_udpcupdater",
                        "JuiceboxUDPCUpdater unexpected error; retrying.",
                    )
                await self._add_error()
                await asyncio.sleep(5)
            finally:
                if self._telnet is not None:
                    try:
                        await self._telnet.close()
                    except Exception:
                        pass
                    self._telnet = None
                # Short pause before next supervisor iteration
                await asyncio.sleep(1)

    async def close(self):
        self._stop_event.set()
        # Cancel background loop if running
        if (
            self._udpc_update_loop_task is not None
            and not self._udpc_update_loop_task.done()
        ):
            self._udpc_update_loop_task.cancel()
            try:
                await self._udpc_update_loop_task
            except Exception:
                pass
            self._udpc_update_loop_task = None
        if self._telnet is not None:
            await self._telnet.close()
            self._telnet = None

    async def _connect(self) -> bool:
        connect_attempt = 1
        while (
            self._telnet is None
            and connect_attempt <= MAX_RETRY_ATTEMPT
            and self._error_count < MAX_ERROR_COUNT
        ):
            _LOGGER.debug(
                f"Telnet connection attempt {connect_attempt} of {MAX_RETRY_ATTEMPT}"
            )
            connect_attempt += 1
            self._telnet = JuiceboxTelnet(
                self._juicebox_host,
                self._telnet_port,
                loglevel=_LOGGER.getEffectiveLevel(),
                timeout=self._telnet_timeout,
            )
            try:
                await self._telnet.open()
            except TimeoutError as e:
                _LOGGER.warning(
                    "JuiceboxUDPCUpdater Telnet Timeout. Reconnecting. "
                    f"({e.__class__.__qualname__}: {e})"
                )
                await self._add_error()
                await self._telnet.close()
                self._telnet = None
                await asyncio.sleep(3)
            except ConnectionResetError as e:
                _LOGGER.warning(
                    "JuiceboxUDPCUpdater Telnet Connection Error. Reconnecting. "
                    f"({e.__class__.__qualname__}: {e})"
                )
                await self._add_error()
                await self._telnet.close()
                self._telnet = None
                await asyncio.sleep(3)
            except OSError as e:
                _LOGGER.warning(
                    "JuiceboxUDPCUpdater Telnet OS Error. Reconnecting. "
                    f"({e.__class__.__qualname__}: {e})"
                )
                await self._add_error()
                await self._telnet.close()
                self._telnet = None
                await asyncio.sleep(3)
        if self._telnet is None:
            _LOGGER.warning(
                "JuiceboxUDPCUpdater: Unable to connect to Telnet. Will retry."
            )
            if self._mqtt_handler:
                await self._mqtt_handler.publish_task_status(
                    "juicebox_udpcupdater",
                    "JuiceboxUDPCUpdater: Unable to connect to Telnet. Will retry.",
                )
            return False
        # Start the UDPC update loop as a background task if not already running
        if self._udpc_update_loop_task is None or self._udpc_update_loop_task.done():
            self._udpc_update_loop_task = asyncio.create_task(
                self._udpc_update_loop(), name="udpc_update_loop"
            )
        _LOGGER.info("JuiceboxUDPCUpdater Connected to Juicebox Telnet")
        if self._mqtt_handler:
            await self._mqtt_handler.publish_task_status(
                "juicebox_udpcupdater", "JuiceboxUDPCUpdater Connected to Telnet"
            )
        return True

    async def _udpc_update_loop(self):
        _LOGGER.debug("Starting JuiceboxUDPCUpdater Loop")
        while not self._stop_event.is_set() and self._error_count < MAX_ERROR_COUNT:
            sleep_interval = self._default_sleep_interval
            if self._telnet is None:
                _LOGGER.warning(
                    "JuiceboxUDPCUpdater Telnet Connection Lost. Reconnecting."
                )
                if self._mqtt_handler:
                    await self._mqtt_handler.publish_task_status(
                        "juicebox_udpcupdater",
                        "JuiceboxUDPCUpdater Telnet lost. Reconnecting.",
                    )
                await self._connect()
                continue
            # Proactively verify connection health each cycle (heartbeat in open())
            try:
                await self._telnet.open()
            except (TimeoutError, ConnectionResetError, OSError) as e:
                _LOGGER.warning(
                    "JuiceboxUDPCUpdater Telnet not healthy; reconnecting. %s: %s",
                    e.__class__.__qualname__,
                    e,
                )
                await self._add_error()
                try:
                    await self._telnet.close()
                except Exception:
                    pass
                self._telnet = None
                if self._mqtt_handler:
                    await self._mqtt_handler.publish_task_status(
                        "juicebox_udpcupdater",
                        "JuiceboxUDPCUpdater Telnet not healthy; reconnecting.",
                    )
                await asyncio.sleep(3)
                await self._connect()
                continue
            try:
                async with asyncio.timeout(UDPC_UPDATE_CHECK_TIMEOUT):
                    sleep_interval = await self._udpc_update_handler(sleep_interval)
            except TimeoutError as e:
                _LOGGER.warning(
                    f"UDPC Update Check timeout after {UDPC_UPDATE_CHECK_TIMEOUT} sec. "
                    f"({e.__class__.__qualname__}: {e})"
                )
                await self._add_error()
                await self._telnet.close()
                self._telnet = None
                sleep_interval = 3
            except Exception as e:
                _LOGGER.warning(
                    "UDPC Update Loop encountered error; will reconnect. %s: %s",
                    e.__class__.__qualname__,
                    e,
                )
                await self._add_error()
                if self._telnet is not None:
                    try:
                        await self._telnet.close()
                    except Exception:
                        pass
                    self._telnet = None
                sleep_interval = 3
            await asyncio.sleep(sleep_interval)
        # When too many errors occur, back off instead of crashing the whole app
        _LOGGER.warning(
            "JuiceboxUDPCUpdater hit error threshold (%d in last %d min). Backing off.",
            self._error_count,
            ERROR_LOOKBACK_MIN,
        )
        if self._mqtt_handler:
            await self._mqtt_handler.publish_task_status(
                "juicebox_udpcupdater",
                "JuiceboxUDPCUpdater hit error threshold. Backing off.",
            )
        # Reset counters and let supervisor retry after a short pause
        self._error_count = 0
        self._error_timestamp_list.clear()

    async def _udpc_update_handler(self, default_sleep_interval):
        sleep_interval = default_sleep_interval
        try:
            _LOGGER.info("JuiceboxUDPCUpdater Check Starting")
            connections = await self._telnet.get_udpc_list()
            update_required = True
            udpc_streams_to_close = {}  # Key = Connection id, Value = list id
            udpc_stream_to_update = 0

            # _LOGGER.debug(f"connections: {connections}")

            for i, connection in enumerate(connections):
                if connection["type"] == "UDPC":
                    udpc_streams_to_close.update({int(connection["id"]): i})
                    if self._jpp_host not in connection["dest"]:
                        udpc_stream_to_update = int(connection["id"])
            # _LOGGER.debug(f"udpc_streams_to_close: {udpc_streams_to_close}")
            if udpc_stream_to_update == 0 and len(udpc_streams_to_close) > 0:
                udpc_stream_to_update = int(max(udpc_streams_to_close, key=int))
            _LOGGER.debug(f"Active UDPC Stream: {udpc_stream_to_update}")

            for stream in list(udpc_streams_to_close):
                if stream < udpc_stream_to_update:
                    udpc_streams_to_close.pop(stream, None)

            if len(udpc_streams_to_close) == 0:
                _LOGGER.info("UDPC IP not found, updating")
            elif (
                self._jpp_host
                not in connections[udpc_streams_to_close[udpc_stream_to_update]]["dest"]
            ):
                _LOGGER.info("UDPC IP incorrect, updating")
                _LOGGER.debug(f"connections: {connections}")
            elif len(udpc_streams_to_close) == 1:
                _LOGGER.info("UDPC IP correct")
                update_required = False

            if update_required:
                for id in udpc_streams_to_close:
                    _LOGGER.debug(f"Closing UDPC stream: {id}")
                    await self._telnet.close_udpc_stream(id)
                await self._telnet.write_udpc_stream(self._jpp_host, self._udpc_port)
                # Save is not recommended https://github.com/snicker/juicepassproxy/issues/96
                # await self._telnet.save_udpc()
                _LOGGER.info("UDPC IP Changed")
        except ConnectionResetError as e:
            _LOGGER.warning(
                "Telnet connection to JuiceBox lost. "
                "Nothing to worry about unless this happens a lot. "
                f"({e.__class__.__qualname__}: {e})"
            )
            await self._add_error()
            await self._telnet.close()
            self._telnet = None
            sleep_interval = 3
        except TimeoutError as e:
            _LOGGER.warning(
                "Telnet connection to JuiceBox has timed out. "
                "Nothing to worry about unless this happens a lot. "
                f"({e.__class__.__qualname__}: {e})"
            )
            await self._add_error()
            await self._telnet.close()
            self._telnet = None
            sleep_interval = 3
        except OSError as e:
            _LOGGER.warning(
                "Could not route Telnet connection to JuiceBox. "
                "Nothing to worry about unless this happens a lot. "
                f"({e.__class__.__qualname__}: {e})"
            )
            await self._add_error()
            await self._telnet.close()
            self._telnet = None
            sleep_interval = 3
        return sleep_interval

    async def _add_error(self):
        self._error_timestamp_list.append(time.time())
        time_cutoff = time.time() - (ERROR_LOOKBACK_MIN * 60)
        temp_list = list(
            filter(lambda el: el > time_cutoff, self._error_timestamp_list)
        )
        self._error_timestamp_list = temp_list
        self._error_count = len(self._error_timestamp_list)
        _LOGGER.debug(f"Errors in last {ERROR_LOOKBACK_MIN} min: {self._error_count}")
