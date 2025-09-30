import sys
import types
import unittest
import asyncio


def _install_stub_modules():
    # Stub for telnetlib3 with open_connection that raises OSError
    async def _open_connection(*args, **kwargs):
        raise OSError(113, f"Connect call failed {args}")

    telnetlib3_stub = types.SimpleNamespace(open_connection=_open_connection)
    sys.modules.setdefault("telnetlib3", telnetlib3_stub)

    # Minimal stubs for other external imports used by juicepassproxy on import
    dns_stub = types.ModuleType("dns")
    resolver_mod = types.ModuleType("resolver")

    class _Answers(list):
        @property
        def address(self):
            return "127.0.0.1"

    class _Resolver:
        def __init__(self):
            self.nameservers = []

        def resolve(self, *args, **kwargs):
            return _Answers([types.SimpleNamespace(address="127.0.0.1")])

    class _LifetimeTimeout(Exception):
        pass

    class _NoNameservers(Exception):
        pass

    class _NoAnswer(Exception):
        pass

    resolver_mod.Resolver = _Resolver
    resolver_mod.LifetimeTimeout = _LifetimeTimeout
    resolver_mod.NoNameservers = _NoNameservers
    resolver_mod.NoAnswer = _NoAnswer
    dns_stub.resolver = resolver_mod
    rdatatype_mod = types.ModuleType("rdatatype")
    rdatatype_mod.A = object()
    dns_stub.rdatatype = rdatatype_mod
    sys.modules.setdefault("dns", dns_stub)
    sys.modules.setdefault("dns.resolver", resolver_mod)
    sys.modules.setdefault("dns.rdatatype", rdatatype_mod)

    # aiorun stub
    aiorun_stub = types.ModuleType("aiorun")
    def _run(*args, **kwargs):
        pass
    aiorun_stub.run = _run
    sys.modules.setdefault("aiorun", aiorun_stub)

    # ha_mqtt_discoverable.Settings stub
    had_stub = types.ModuleType("ha_mqtt_discoverable")
    class _Settings:
        class MQTT:
            def __init__(self, host, port, username=None, password=None, discovery_prefix=None):
                self.host = host
                self.port = port
                self.username = username
                self.password = password
                self.discovery_prefix = discovery_prefix
    class _DeviceInfo:
        pass
    had_stub.Settings = _Settings
    had_stub.DeviceInfo = _DeviceInfo
    sys.modules.setdefault("ha_mqtt_discoverable", had_stub)

    # ha_mqtt_discoverable.sensors stub
    sensors_mod = types.ModuleType("ha_mqtt_discoverable.sensors")
    class _BaseInfo:
        __fields__ = {"name": None, "unique_id": None}
        @classmethod
        def parse_obj(cls, obj):
            return obj
    class SensorInfo(_BaseInfo):
        pass
    class NumberInfo(_BaseInfo):
        pass
    class SwitchInfo(_BaseInfo):
        pass
    class ButtonInfo(_BaseInfo):
        pass
    class TextInfo(_BaseInfo):
        pass
    class Sensor:
        def __init__(self, *args, **kwargs):
            self.mqtt = None
        def set_state(self, *args, **kwargs):
            pass
        def set_attributes(self, *args, **kwargs):
            pass
    class Number(Sensor):
        pass
    class Switch(Sensor):
        def on(self):
            pass
        def off(self):
            pass
    class Button(Sensor):
        pass
    class Text(Sensor):
        pass
    sensors_mod.SensorInfo = SensorInfo
    sensors_mod.NumberInfo = NumberInfo
    sensors_mod.SwitchInfo = SwitchInfo
    sensors_mod.ButtonInfo = ButtonInfo
    sensors_mod.TextInfo = TextInfo
    sensors_mod.Sensor = Sensor
    sensors_mod.Number = Number
    sensors_mod.Switch = Switch
    sensors_mod.Button = Button
    sensors_mod.Text = Text
    sys.modules.setdefault("ha_mqtt_discoverable.sensors", sensors_mod)

    # croniter stub
    croniter_stub = types.ModuleType("croniter")
    def _croniter(expr, base_time):
        class _C:
            def get_next(self, _):
                return base_time
        return _C()
    croniter_stub.croniter = _croniter
    sys.modules.setdefault("croniter", croniter_stub)

    # asyncio_dgram stub
    ad_stub = types.ModuleType("asyncio_dgram")
    class _Transport:
        def __init__(self, *args, **kwargs):
            pass
        def close(self):
            pass
        async def recv(self):
            await asyncio.sleep(0)
            raise type("TransportClosed", (), {})
    async def bind(*args, **kwargs):
        return _Transport()
    ad_stub.bind = bind
    ad_stub.TransportClosed = type("TransportClosed", (Exception,), {})
    sys.modules.setdefault("asyncio_dgram", ad_stub)

    # paho.mqtt.client stub
    paho_mod = types.ModuleType("paho")
    mqtt_mod = types.ModuleType("paho.mqtt")
    client_mod = types.ModuleType("paho.mqtt.client")
    class _Client:
        def disconnect(self):
            pass
    class _MQTTMessage:
        payload = b""
    client_mod.Client = _Client
    client_mod.MQTTMessage = _MQTTMessage
    sys.modules.setdefault("paho", paho_mod)
    sys.modules.setdefault("paho.mqtt", mqtt_mod)
    sys.modules.setdefault("paho.mqtt.client", client_mod)


class TestTelnetFailure(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _install_stub_modules()

    async def test_get_enelx_server_port_handles_oserror(self):
        from juicepassproxy import get_enelx_server_port
        # should not raise; should return None on connection failure
        result = await get_enelx_server_port("192.0.2.1", 2000, telnet_timeout=1)
        self.assertIsNone(result)

    async def test_get_juicebox_id_handles_oserror(self):
        from juicepassproxy import get_juicebox_id
        result = await get_juicebox_id("192.0.2.1", 2000, telnet_timeout=1)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
