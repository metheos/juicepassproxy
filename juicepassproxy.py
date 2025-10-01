#!/usr/bin/env python3

import argparse
import asyncio
import ipaddress
import logging
import socket
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import dns
from aiorun import run
from const import (
    DAYS_TO_KEEP_LOGS,
    DEFAULT_DEVICE_NAME,
    DEFAULT_ENELX_IP,
    DEFAULT_ENELX_PORT,
    DEFAULT_ENELX_SERVER,
    DEFAULT_LOCAL_IP,
    DEFAULT_LOGLEVEL,
    DEFAULT_MQTT_DISCOVERY_PREFIX,
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    DEFAULT_TELNET_PORT,
    DEFAULT_TELNET_TIMEOUT,
    EXTERNAL_DNS,
    LOG_DATE_FORMAT,
    LOG_FORMAT,
    LOGFILE,
    MAX_JPP_LOOP,
    VERSION,
)
from ha_mqtt_discoverable import Settings
from juicebox_mitm import JuiceboxMITM
from juicebox_mqtthandler import JuiceboxMQTTHandler
from juicebox_telnet import JuiceboxTelnet
from juicebox_udpcupdater import JuiceboxUDPCUpdater
from juicebox_config import JuiceboxConfig
from croniter import croniter
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)


AP_DESCRIPTION = """
JuicePass Proxy - by snicker
Publish JuiceBox data from a UDP Man in the Middle Proxy to MQTT discoverable by HomeAssistant.

https://github.com/snicker/juicepassproxy

To get the destination IP:Port of the EnelX server, telnet to your Juicenet device:

$ telnet 192.168.x.x 2000 and type the list command: list

! # Type  Info
# 0 FILE  webapp/index.html-1.4.0.24 (1995, 0)
# 1 UDPC  juicenet-udp-prod3-usa.enelx.com:8047 (26674)

The address is in the UDPC line.
Run ping, nslookup, or similar command to determine the IP.

As of November, 2023: juicenet-udp-prod3-usa.enelx.com = 54.161.185.130.
"""


async def get_local_ip():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            remote_addr=("10.254.254.254", 80),
            family=socket.AF_INET,
        )
        local_ip = transport.get_extra_info("sockname")[0]
    except Exception as e:
        _LOGGER.warning(f"Unable to get Local IP. ({e.__class__.__qualname__}: {e})")
        local_ip = None
    finally:
        transport.close()
    return local_ip


async def resolve_ip_external_dns(address, use_dns=EXTERNAL_DNS):
    # res = await dns.asyncresolver.Resolver()
    res = dns.resolver.Resolver()
    res.nameservers = [use_dns]
    try:
        # answers = await res.resolve(
        answers = res.resolve(address, rdtype=dns.rdatatype.A, raise_on_no_answer=True)
    except (
        dns.resolver.LifetimeTimeout,
        dns.resolver.NoNameservers,
        dns.resolver.NoAnswer,
    ) as e:
        _LOGGER.warning(
            f"Unable to resolve {address}. ({e.__class__.__qualname__}: {e})"
        )
        return None

    if len(answers) > 0:
        return answers[0].address
    return None


async def is_valid_ip(test_ip):
    try:
        ipaddress.ip_address(test_ip)
    except ValueError:
        return False
    return True


async def get_enelx_server_port(juicebox_host, telnet_port, telnet_timeout=None):
    try:
        async with JuiceboxTelnet(
            juicebox_host,
            telnet_port,
            loglevel=_LOGGER.getEffectiveLevel(),
            timeout=telnet_timeout,
        ) as tn:
            connections = await tn.get_udpc_list()
            # _LOGGER.debug(f"connections: {connections}")
            for connection in connections:
                if connection["type"] == "UDPC" and not await is_valid_ip(
                    connection["dest"].split(":")[0]
                ):
                    return connection["dest"]
    except TimeoutError as e:
        _LOGGER.warning(
            "Error in getting EnelX Server and Port via Telnet. "
            f"({e.__class__.__qualname__}: {e})"
        )
        return None
    except ConnectionResetError as e:
        _LOGGER.warning(
            "Error in getting EnelX Server and Port via Telnet. "
            f"({e.__class__.__qualname__}: {e})"
        )
        return None
    except OSError as e:
        _LOGGER.warning(
            "Error in getting EnelX Server and Port via Telnet. "
            f"({e.__class__.__qualname__}: {e})"
        )
        return None
    return None


async def get_juicebox_id(juicebox_host, telnet_port, telnet_timeout=None):
    try:
        async with JuiceboxTelnet(
            juicebox_host,
            telnet_port,
            loglevel=_LOGGER.getEffectiveLevel(),
            timeout=telnet_timeout,
        ) as tn:
            juicebox_id = (await tn.get_variable("email.name_address")).decode("utf-8")
            return juicebox_id
    except TimeoutError as e:
        _LOGGER.warning(
            "Error in getting JuiceBox ID via Telnet. "
            f"({e.__class__.__qualname__}: {e})"
        )
        return None
    except ConnectionResetError as e:
        _LOGGER.warning(
            "Error in getting JuiceBox ID via Telnet. "
            f"({e.__class__.__qualname__}: {e})"
        )
        return None
    except OSError as e:
        _LOGGER.warning(
            "Error in getting JuiceBox ID via Telnet. "
            f"({e.__class__.__qualname__}: {e})"
        )
        return None
    return None


async def send_reboot_command(
    juicebox_host, telnet_port, mqtt_handler, telnet_timeout, udpc_updater
):
    try:
        entity_values = mqtt_handler.get_entity_values()
        _LOGGER.info(
            "send_reboot_command invoked; current status=%s",
            entity_values.get("status"),
        )

        # Validate the 'status' of the juicebox is 'Unplugged'
        if entity_values.get("status") == "Unplugged":
            # Pause UDPC updater to free Telnet, resume after reboot window
            # We pause the UDPC updater to free the single Telnet session used by the box.
            # The updater keeps trying to heartbeat/open the same session, preventing the reboot command
            # from acquiring the prompt reliably. Pause here and resume shortly after issuing reboot.
            # Pause UDPC indefinitely; we'll resume after we successfully send reboot (or on failure)
            if udpc_updater is not None:
                try:
                    await udpc_updater.pause()
                    _LOGGER.info("Paused UDPC updater before reboot (no auto-resume)")
                except Exception as e:
                    _LOGGER.warning(
                        "Failed to pause UDPC updater prior to reboot: %s: %s",
                        e.__class__.__qualname__,
                        e,
                    )
            # Give the device a brief moment after releasing the prior Telnet
            await asyncio.sleep(2)

            # Try to open Telnet and send reboot with short retries (device may refuse new connections briefly)
            reboot_sent = False
            max_attempts = 20  # ~20s window with 1s backoff
            for attempt in range(1, max_attempts + 1):
                _LOGGER.info(
                    "Reboot attempt %d/%d: opening Telnet...", attempt, max_attempts
                )
                try:
                    async with JuiceboxTelnet(
                        juicebox_host,
                        telnet_port,
                        loglevel=_LOGGER.getEffectiveLevel(),
                        timeout=telnet_timeout,
                    ) as tn:
                        await tn.reboot()
                        _LOGGER.info(
                            "Reboot command issued to JuiceBox (attempt %d)", attempt
                        )
                        reboot_sent = True
                        break
                except (TimeoutError, ConnectionResetError, OSError) as e:
                    _LOGGER.warning(
                        "Reboot attempt %d failed: %s: %s",
                        attempt,
                        e.__class__.__qualname__,
                        e,
                    )
                    await asyncio.sleep(1)

            if reboot_sent:
                # Record and publish the time of the scheduled reboot
                ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                _LOGGER.info(f"Scheduled reboot command sent at {ts}")
                try:
                    _LOGGER.info("Publishing last_reboot timestamp: %s", ts)
                    await mqtt_handler.get_entity("last_reboot").set_state(ts)
                except Exception as e:
                    _LOGGER.debug(
                        f"Failed to publish last_reboot timestamp: {e.__class__.__qualname__}: {e}"
                    )
                try:
                    await mqtt_handler.publish_task_status(
                        "reboot", f"Scheduled reboot command sent at {ts}"
                    )
                except Exception:
                    pass
                # Resume UDPC a bit after reboot command to avoid reconnect thrash during device restart
                if udpc_updater is not None:
                    try:
                        asyncio.create_task(udpc_updater.delayed_resume(10))
                        _LOGGER.info("Scheduled UDPC updater to resume in 10s")
                    except Exception as e:
                        _LOGGER.warning(
                            "Failed to schedule UDPC updater resume: %s: %s",
                            e.__class__.__qualname__,
                            e,
                        )
                return True
            else:
                _LOGGER.warning("All reboot attempts failed; resuming UDPC updater")
                if udpc_updater is not None:
                    try:
                        await udpc_updater.resume()
                    except Exception:
                        pass
                return False
        else:
            _LOGGER.warning(
                "Scheduled reboot skipped: Juicebox status is not 'Unplugged'."
            )
            return False
    except TimeoutError as e:
        _LOGGER.warning(
            "Error in sending reboot command via Telnet. "
            f"({e.__class__.__qualname__}: {e})"
        )
        return False
    except ConnectionResetError as e:
        _LOGGER.warning(
            "Error in sending reboot command via Telnet. "
            f"({e.__class__.__qualname__}: {e})"
        )
        return False
    except OSError as e:
        _LOGGER.warning(
            "Error in sending reboot command via Telnet. "
            f"({e.__class__.__qualname__}: {e})"
        )
        return False


def ip_to_tuple(ip):
    if isinstance(ip, tuple):
        return ip
    ip, port = ip.split(":")
    return (ip, int(port))


async def scheduled_task(cron_schedule, task_function, *args):
    base_time = datetime.now()
    cron = croniter(cron_schedule, base_time)
    while True:
        next_run = cron.get_next(datetime)
        sleep_duration = (next_run - datetime.now()).total_seconds()
        await asyncio.sleep(sleep_duration)
        await task_function(*args)


async def scheduled_reboot_task(
    cron_schedule,
    task_function,
    juicebox_host,
    telnet_port,
    mqtt_handler,
    telnet_timeout,
    udpc_updater,
):
    """
    Run the reboot on the provided cron schedule. If the reboot is skipped
    because the JuiceBox is not Unplugged, re-check every hour until safe,
    perform the reboot, then resume using the defined cron schedule.
    """
    base_time = datetime.now()
    cron = croniter(cron_schedule, base_time)
    while True:
        next_run = cron.get_next(datetime)
        sleep_duration = (next_run - datetime.now()).total_seconds()
        await asyncio.sleep(max(0, sleep_duration))

        # Attempt reboot at scheduled time
        success = await task_function(
            juicebox_host, telnet_port, mqtt_handler, telnet_timeout, udpc_updater
        )
        if success:
            # Reboot sent; continue with the next cron time
            continue

        # Not safe to reboot now; poll hourly until Unplugged
        _LOGGER.info(
            "Scheduled reboot deferred; will re-check every 3600s until status is 'Unplugged'."
        )
        while True:
            await asyncio.sleep(3600)
            # Check current status
            try:
                status = mqtt_handler.get_entity_values().get("status")
            except Exception:
                status = None
            if status == "Unplugged":
                success = await task_function(
                    juicebox_host,
                    telnet_port,
                    mqtt_handler,
                    telnet_timeout,
                    udpc_updater,
                )
                if success:
                    _LOGGER.info(
                        "Deferred reboot completed; resuming defined schedule."
                    )
                    break
            else:
                _LOGGER.info(
                    f"Reboot still not safe (status={status}); will re-check in 3600s."
                )


async def parse_args():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=AP_DESCRIPTION,
    )

    parser.add_argument(
        "--juicebox_host",
        type=str,
        metavar="HOST",
        help="Host or IP address of the JuiceBox. Required for --update_udpc or if --enelx_ip not defined.",
    )

    parser.add_argument(
        "--update_udpc",
        action="store_true",
        help="Update UDPC on the JuiceBox. Requires --juicebox_host",
    )

    parser.add_argument(
        "--jpp_host",
        "--juicepass_proxy_host",
        dest="jpp_host",
        type=str,
        metavar="HOST",
        help="EXTERNAL host or IP address of the machine running JuicePass "
        "Proxy. Optional: only necessary when using --update_udpc and "
        "it will be inferred from the address in --local_ip if omitted.",
    )

    parser.add_argument(
        "-H",
        "--mqtt_host",
        type=str,
        metavar="HOST",
        default=DEFAULT_MQTT_HOST,
        help="MQTT Hostname to connect to (default: %(default)s)",
    )

    parser.add_argument(
        "-p",
        "--mqtt_port",
        type=int,
        metavar="PORT",
        default=DEFAULT_MQTT_PORT,
        help="MQTT Port (default: %(default)s)",
    )

    parser.add_argument(
        "-u", "--mqtt_user", type=str, help="MQTT Username", metavar="USER"
    )

    parser.add_argument(
        "-P", "--mqtt_password", type=str, help="MQTT Password", metavar="PASSWORD"
    )

    parser.add_argument(
        "-D",
        "--mqtt_discovery_prefix",
        type=str,
        metavar="PREFIX",
        dest="mqtt_discovery_prefix",
        default=DEFAULT_MQTT_DISCOVERY_PREFIX,
        help="Home Assistant MQTT topic prefix (default: %(default)s)",
    )

    parser.add_argument(
        "--config_loc",
        type=str,
        metavar="LOC",
        default=Path.home().joinpath(".juicepassproxy"),
        help="The location to store the config file (default: %(default)s)",
    )

    parser.add_argument(
        "--log_loc",
        type=str,
        metavar="LOC",
        default=str(Path.home()),
        help="The location to store the log files (default: %(default)s)",
    )

    parser.add_argument(
        "--name",
        type=str,
        default=DEFAULT_DEVICE_NAME,
        help="Home Assistant Device Name (default: %(default)s)",
        dest="device_name",
    )

    parser.add_argument(
        "--debug", action="store_true", help="Show Debug level logging. (default: Info)"
    )

    parser.add_argument(
        "--disable_reuse_port",
        action="store_true",
        help="Disable port reuse for server socket (default: reuse_port)",
    )

    parser.add_argument(
        "--experimental",
        action="store_true",
        help="Enables additional entities in Home Assistant that are in in development or can be used toward developing the ability to send commands to a JuiceBox.",
    )

    parser.add_argument(
        "--ignore_enelx",
        action="store_true",
        help="If set, will not send commands received from EnelX to the JuiceBox nor send outgoing information from the JuiceBox to EnelX",
    )

    parser.add_argument(
        "--tp",
        "--telnet_port",
        dest="telnet_port",
        required=False,
        type=int,
        metavar="PORT",
        default=DEFAULT_TELNET_PORT,
        help="Telnet PORT (default: %(default)s)",
    )

    parser.add_argument(
        "--telnet_timeout",
        type=int,
        metavar="SECONDS",
        default=DEFAULT_TELNET_TIMEOUT,
        help="Timeout in seconds for Telnet operations (default: %(default)s)",
    )

    parser.add_argument(
        "--juicebox_id",
        type=str,
        metavar="ID",
        help="JuiceBox ID. If not defined, will obtain it automatically.",
        dest="juicebox_id",
    )

    parser.add_argument(
        "--local_ip",
        "-s",
        "--src",
        dest="local_ip",
        required=False,
        type=str,
        metavar="IP",
        help="Local IP (and optional port). If not defined, will obtain it automatically. (Ex. 127.0.0.1:8047) [Deprecated: -s --src]",
    )

    parser.add_argument(
        "--local_port",
        dest="local_port",
        required=False,
        type=int,
        metavar="PORT",
        help="Local Port for JPP to listen on.",
    )

    parser.add_argument(
        "--enelx_ip",
        "-d",
        "--dst",
        dest="enelx_ip",
        required=False,
        type=str,
        metavar="IP",
        help="Destination IP (and optional port) of EnelX Server. If not defined, --juicebox_host required and then will obtain it automatically. (Ex. 54.161.185.130:8047) [Deprecated: -d --dst]",
    )

    parser.add_argument(
        "--cron_reboot_schedule",
        type=str,
        metavar="CRON",
        help="Cron-like schedule for executing a task (e.g., '*/5 * * * *' for every 5 minutes).",
    )

    return parser.parse_args()


async def main():
    args = await parse_args()
    log_handlers = [logging.StreamHandler()]
    enable_file_log = (len(args.log_loc) > 0) and (args.log_loc != "none")
    log_loc = Path(args.log_loc)
    if enable_file_log:
        log_loc.mkdir(parents=True, exist_ok=True)
        log_loc = log_loc.joinpath(LOGFILE)
        log_loc.touch(exist_ok=True)
        log_handlers.append(
            TimedRotatingFileHandler(
                log_loc,
                when="midnight",
                backupCount=DAYS_TO_KEEP_LOGS,
                encoding="utf-8",
            )
        )
    # Ensure handler levels are consistent
    for h in log_handlers:
        h.setLevel(DEFAULT_LOGLEVEL)
    logging.basicConfig(
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        level=DEFAULT_LOGLEVEL,
        handlers=log_handlers,
        force=True,
    )
    # Capture warnings into logging
    logging.captureWarnings(True)
    if args.debug:
        _LOGGER.setLevel(logging.DEBUG)
    _LOGGER.warning(
        f"Starting JuicePass Proxy {VERSION} "
        f"(Log Level: {logging.getLevelName(_LOGGER.getEffectiveLevel())}, log_handlers={len(log_handlers)})"
    )
    if enable_file_log:
        _LOGGER.info(f"log_loc: {log_loc}")
    else:
        _LOGGER.info("not logging to file")
    if len(sys.argv) == 1:
        _LOGGER.error(
            "Exiting: no command-line arguments given. Run with --help to see options."
        )
        sys.exit(1)

    if len(sys.argv) > 1 and args.update_udpc and not args.juicebox_host:
        _LOGGER.error(
            "Exiting: --update_udpc is set, thus --juicebox_host is required.",
        )
        sys.exit(1)

    if len(sys.argv) > 1 and not args.enelx_ip and not args.juicebox_host:
        _LOGGER.error(
            "Exiting: --enelx_ip is not set, thus --juicebox_host is required.",
        )
        sys.exit(1)

    config = JuiceboxConfig(args.config_loc)
    await config.load()

    telnet_port = int(args.telnet_port)
    _LOGGER.info(f"telnet port: {telnet_port}")
    if telnet_port == 0:
        telnet_port = 2000

    telnet_timeout = int(args.telnet_timeout)
    _LOGGER.info(f"telnet timeout: {telnet_timeout}")
    if telnet_timeout == 0:
        telnet_timeout = None

    ignore_enelx = args.ignore_enelx
    _LOGGER.info(f"ignore_enelx: {ignore_enelx}")

    enelx_server_port = None
    if not ignore_enelx:
        enelx_server_port = await get_enelx_server_port(
            args.juicebox_host, args.telnet_port, telnet_timeout=telnet_timeout
        )

    if enelx_server_port:
        _LOGGER.debug(f"enelx_server_port: {enelx_server_port}")
        enelx_server = enelx_server_port.split(":")[0]
        enelx_port = enelx_server_port.split(":")[1]
    else:
        enelx_server = config.get("ENELX_SERVER", DEFAULT_ENELX_SERVER)
        enelx_port = config.get("ENELX_PORT", DEFAULT_ENELX_PORT)
    config.update_value("ENELX_SERVER", enelx_server)
    config.update_value("ENELX_PORT", enelx_port)
    _LOGGER.info(f"enelx_server: {enelx_server}")
    _LOGGER.info(f"enelx_port: {enelx_port}")

    if (
        args.local_port
        and args.local_ip
        and ":" in args.local_ip
        and int(args.local_ip.split(":")[1]) != args.local_port
    ):
        _LOGGER.error(
            "Exiting: Local port conflict: --local_ip with port "
            f"{args.local_ip.split(':')[1]} and --local_port of {args.local_port}"
        )
        sys.exit(1)

    if args.local_port:
        local_port = args.local_port
    else:
        local_port = enelx_port
    if args.local_ip:
        if ":" in args.local_ip:
            local_addr = ip_to_tuple(args.local_ip)
        else:
            local_addr = ip_to_tuple(f"{args.local_ip}:{local_port}")
    elif local_ip := await get_local_ip():
        local_addr = ip_to_tuple(f"{local_ip}:{local_port}")
    else:
        local_addr = ip_to_tuple(
            f"{config.get('LOCAL_IP', config.get('SRC', DEFAULT_LOCAL_IP))}:"
            f"{local_port}"
        )
    config.update_value("LOCAL_IP", local_addr[0])
    _LOGGER.info(f"local_addr: {local_addr[0]}:{local_addr[1]}")

    localhost_check = (
        local_addr[0].startswith("0.")
        or local_addr[0].startswith("127")
        or "localhost" in local_addr[0]
    )
    if args.update_udpc and localhost_check and not args.jpp_host:
        _LOGGER.error(
            "Exiting: when --update_udpc is set, --local_ip must not be a localhost address (ex. 127.0.0.1) or "
            "--jpp_host must also be set.",
        )
        sys.exit(1)

    if args.enelx_ip:
        if ":" in args.enelx_ip:
            enelx_addr = ip_to_tuple(args.enelx_ip)
        else:
            enelx_addr = ip_to_tuple(f"{args.enelx_ip}:{enelx_port}")
    elif enelx_server_ip := await resolve_ip_external_dns(enelx_server):
        enelx_addr = ip_to_tuple(f"{enelx_server_ip}:{enelx_port}")
    else:
        enelx_addr = ip_to_tuple(
            f"{config.get('ENELX_IP', config.get('DST', DEFAULT_ENELX_IP))}:"
            f"{enelx_port}"
        )
    config.update_value("ENELX_IP", enelx_addr[0])
    _LOGGER.info(f"enelx_addr: {enelx_addr[0]}:{enelx_addr[1]}")
    _LOGGER.info(f"telnet_addr: {args.juicebox_host}:{args.telnet_port}")

    if juicebox_id := args.juicebox_id:
        pass
    elif juicebox_id := await get_juicebox_id(
        args.juicebox_host, args.telnet_port, telnet_timeout=telnet_timeout
    ):
        pass
    else:
        juicebox_id = config.get("JUICEBOX_ID", None)
    if juicebox_id:
        config.update_value("JUICEBOX_ID", juicebox_id)
        _LOGGER.info(f"juicebox_id: {juicebox_id}")
    else:
        _LOGGER.error(
            "Cannot get JuiceBox ID from Telnet and not in Config. If a JuiceBox ID is later set or is obtained via Telnet, it will likely create a new JuiceBox Device with new Entities in Home Assistant."
        )

    experimental = args.experimental
    _LOGGER.info(f"experimental: {experimental}")

    # Remove DST and SRC from Config as they have been replaced by ENELX_IP and LOCAL_IP respectively
    config.pop("DST")
    config.pop("SRC")

    await config.write_if_changed()

    mqtt_settings = Settings.MQTT(
        host=args.mqtt_host,
        port=args.mqtt_port,
        username=args.mqtt_user,
        password=args.mqtt_password,
        discovery_prefix=args.mqtt_discovery_prefix,
    )

    jpp_loop_count = 1
    while jpp_loop_count <= MAX_JPP_LOOP:
        if jpp_loop_count != 1:
            _LOGGER.error(f"Restarting JuicePass Proxy Loop ({jpp_loop_count})")
        jpp_loop_count += 1
        jpp_task_list = []
        udpc_updater = None
        mqtt_handler = JuiceboxMQTTHandler(
            mqtt_settings=mqtt_settings,
            device_name=args.device_name,
            juicebox_id=juicebox_id,
            config=config,
            experimental=experimental,
            loglevel=_LOGGER.getEffectiveLevel(),
        )
        jpp_task_list.append(
            asyncio.create_task(mqtt_handler.start(), name="mqtt_handler")
        )

        mitm_handler = JuiceboxMITM(
            jpp_addr=local_addr,  # Local/Docker IP
            enelx_addr=enelx_addr,  # EnelX IP
            ignore_enelx=ignore_enelx,
            loglevel=_LOGGER.getEffectiveLevel(),
            # windows users are having trouble with reuse_port=True
            # TODO find a safe way to detect windows and change the default value
            reuse_port=config.get("reuse_port", not args.disable_reuse_port),
        )
        await mitm_handler.set_local_mitm_handler(mqtt_handler.local_mitm_handler)
        await mitm_handler.set_remote_mitm_handler(mqtt_handler.remote_mitm_handler)
        jpp_task_list.append(
            asyncio.create_task(mitm_handler.start(), name="mitm_handler")
        )

        await mqtt_handler.set_mitm_handler(mitm_handler)
        await mitm_handler.set_mqtt_handler(mqtt_handler)

        if args.update_udpc:
            jpp_host = args.jpp_host or local_addr[0]
            udpc_updater = JuiceboxUDPCUpdater(
                juicebox_host=args.juicebox_host,
                jpp_host=jpp_host,
                telnet_port=telnet_port,
                udpc_port=local_addr[1],
                telnet_timeout=telnet_timeout,
                loglevel=_LOGGER.getEffectiveLevel(),
                mqtt_handler=mqtt_handler,
            )
            jpp_task_list.append(
                asyncio.create_task(udpc_updater.start(), name="udpc_updater")
            )

        if args.cron_reboot_schedule:
            jpp_task_list.append(
                asyncio.create_task(
                    scheduled_reboot_task(
                        args.cron_reboot_schedule,
                        send_reboot_command,
                        args.juicebox_host,
                        args.telnet_port,
                        mqtt_handler,
                        telnet_timeout,
                        udpc_updater,
                    ),
                    name="scheduled_reboot_task",
                )
            )

        try:
            results = await asyncio.gather(*jpp_task_list, return_exceptions=True)
            for idx, result in enumerate(results):
                if isinstance(result, Exception):
                    _LOGGER.warning(
                        f"Task {jpp_task_list[idx].get_name()} ended with error: {result.__class__.__qualname__}: {result}"
                    )
        except Exception as e:
            _LOGGER.exception(
                f"A JuicePass Proxy supervise block caught: {e.__class__.__qualname__}: {e}"
            )
        finally:
            # Ensure graceful shutdown of components before restarting loop
            try:
                await mqtt_handler.close()
            except Exception:
                pass
            try:
                await mitm_handler.close()
            except Exception:
                pass
            if udpc_updater is not None:
                try:
                    await udpc_updater.close()
                except Exception:
                    pass
            # Cancel any leftover tasks
            for task in jpp_task_list:
                if not task.done():
                    task.cancel()
            await asyncio.sleep(5)
        await asyncio.sleep(5)

    _LOGGER.error("JuicePass Proxy Exiting")
    sys.exit(1)


if __name__ == "__main__":
    run(main(), stop_on_unhandled_errors=True)
