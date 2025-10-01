import asyncio
import logging

import telnetlib3

_LOGGER = logging.getLogger(__name__)


class JuiceboxTelnet:
    def __init__(self, host, port, timeout=None, loglevel=None):
        if loglevel is not None:
            _LOGGER.setLevel(loglevel)
        self.host = host
        self.port = port
        self.reader = None
        self.writer = None
        self.timeout = timeout

    async def __aenter__(self):
        if await self.open():
            return self
        return None

    async def __aexit__(self, exc_type, exc_value, exc_tb):
        if self.reader:
            self.reader.close()
        self.reader = None
        if self.writer:
            self.writer.close()
        self.writer = None

    async def readuntil(self, match: bytes):
        data = b""
        try:
            async with asyncio.timeout(self.timeout):
                data = await self.reader.readuntil(match)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"readuntil (match: {match}, data: {data})") from e
        except asyncio.IncompleteReadError as e:
            # Underlying stream hit EOF or was closed
            raise ConnectionResetError(
                f"readuntil incomplete (match: {match}, data: {data})"
            ) from e
        except ConnectionResetError as e:
            raise ConnectionResetError(
                f"readuntil (match: {match}, data: {data})"
            ) from e
        return data

    async def write(self, data: bytes):
        try:
            async with asyncio.timeout(self.timeout):
                self.writer.write(data)
                await self.writer.drain()
        except TimeoutError as e:
            raise TimeoutError(f"write (data: {data})") from e
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError) as e:
            raise ConnectionResetError(f"write (data: {data})") from e
        return True

    async def open(self):
        # If we think we have an open connection, heartbeat it; otherwise (re)open.
        if self.reader is not None and self.writer is not None:
            try:
                # quick heartbeat to verify prompt is reachable
                await self.write(b"\n")
                await self.readuntil(b"> ")
            except (TimeoutError, ConnectionResetError, OSError):
                # Underlying transport is gone; reset and reopen
                await self.close()
                self.reader = None
                self.writer = None

        if self.reader is None or self.writer is None:
            try:
                async with asyncio.timeout(self.timeout):
                    self.reader, self.writer = await telnetlib3.open_connection(
                        self.host, self.port, encoding=False
                    )
                # read initial prompt (some devices send ">" or "> ")
                try:
                    await self.readuntil(b"> ")
                except TimeoutError:
                    # fallback if device prompts with just '>'
                    await self.readuntil(b">")
            except TimeoutError as e:
                raise TimeoutError("Telnet Connection Failed") from e
            except ConnectionResetError as e:
                raise ConnectionResetError("Telnet Connection Failed") from e
            except OSError as e:
                # Surface a consistent error for callers while preserving context
                raise OSError(f"Telnet Connection Failed: {e}") from e
        # _LOGGER.debug("Telnet Opened")
        return True

    async def close(self):
        if self.reader:
            self.reader.close()
        self.reader = None
        if self.writer:
            self.writer.close()
        self.writer = None

    async def get_udpc_list(self):
        out = []
        if await self.open():
            await self.write(b"\n")
            await self.readuntil(b"> ")
            await self.write(b"list\n")
            await self.readuntil(b"list\r\n! ")
            res = await self.readuntil(b">")
            lines = str(res[:-3]).split("\\r\\n")
            for line in lines[1:]:
                parts = line.split(" ")
                if len(parts) >= 5:
                    out.append({"id": parts[1], "type": parts[2], "dest": parts[4]})
        return out

    async def get_variable(self, variable) -> bytes:
        if await self.open():
            await self.write(b"\n")
            await self.readuntil(b"> ")
            cmd = f"get {variable}\r\n".encode("ascii")
            await self.write(cmd)
            await self.readuntil(cmd)
            res = await self.readuntil(b">")
            return res[:-1].strip()
        return None

    async def get_all_variables(self):
        vars = {}
        if await self.open():
            await self.write(b"\n")
            await self.readuntil(b">")
            cmd = "get all\r\n".encode("ascii")
            await self.write(cmd)
            await self.readuntil(cmd)
            res = await self.readuntil(b">")
            lines = str(res[:-1]).split("\\r\\n")
            for line in lines:
                parts = line.split(": ")
                if len(parts) == 2:
                    vars[parts[0]] = parts[1]
        return vars

    async def close_udpc_stream(self, id):
        if await self.open():
            await self.write(b"\n")
            await self.readuntil(b">")
            await self.write(f"stream_close {id}\n".encode("ascii"))
            await self.readuntil(b">")

    async def write_udpc_stream(self, host, port):
        if await self.open():
            await self.write(b"\n")
            await self.readuntil(b">")
            await self.write(f"udpc {host} {port}\n".encode("ascii"))
            await self.readuntil(b">")

    async def save_udpc(self):
        if await self.open():
            await self.write(b"\n")
            await self.readuntil(b">")
            await self.write(b"save\n")
            await self.readuntil(b">")

    async def send_command(self, command: str):
        if await self.open():
            await self.write(b"\n")
            await self.readuntil(b"> ")
            cmd = f"{command}\r\n".encode("ascii")
            await self.write(cmd)
            await self.readuntil(b">")
            _LOGGER.info(f"Command '{command}' sent successfully.")

    async def reboot(self):
        """
        Send reboot command. Devices typically respond with 'Success' and close the
        connection immediately; do not wait for a new prompt. Consider connection
        close as a success condition.
        """
        if await self.open():
            # Ensure we're at a prompt (best-effort)
            try:
                await self.write(b"\n")
                try:
                    await self.readuntil(b"> ")
                except TimeoutError:
                    await self.readuntil(b">")
            except Exception:
                # Not fatal for reboot; proceed to send command
                pass
            # Send reboot and return without waiting for prompt
            cmd = b"reboot\r\n"
            try:
                await self.write(cmd)
            except ConnectionResetError:
                # If the device closed immediately after receiving reboot, treat as success
                _LOGGER.info(
                    "Reboot command likely accepted; connection closed by device."
                )
                return True
            except TimeoutError:
                # Treat as best-effort success; many devices close quickly
                _LOGGER.info(
                    "Reboot command sent (timeout while awaiting device response)."
                )
                return True
            _LOGGER.info("Reboot command sent.")
            return True
        return False
