"""
Tapo P110M smart plug interface.

Uses the `tapo` library (NOT python-kasa).
The P110M firmware uses TPAP encryption which python-kasa does not support.
Third-Party Services must be enabled in the Tapo app (Me > Third-Party Services).
"""
import asyncio
import logging
from typing import Optional

log = logging.getLogger("pumpastic.plug")

_RETRY_ATTEMPTS = 3
_RETRY_DELAY_S = 1.5


class PlugControl:
    """
    Async interface to the Tapo P110M smart plug.
    Auto-reconnects on failure. All public methods retry up to 3 times.
    """

    def __init__(self, host: str, username: str, password: str):
        self._host = host
        self._username = username
        self._password = password
        self._device = None

    async def connect(self) -> bool:
        """Authenticate and open a session to the plug."""
        try:
            from tapo import ApiClient
            client = ApiClient(self._username, self._password)
            self._device = await client.p110(self._host)
            info = await self._device.get_device_info()
            log.info(
                "Plug connected: %s (%s) — %s",
                info.nickname or info.model,
                info.model,
                "ON" if info.device_on else "OFF",
            )
            return True
        except Exception as e:
            log.error("Plug connection failed: %s", e)
            self._device = None
            return False

    async def turn_on(self) -> bool:
        """Send ON command. Returns True if successful."""
        return await self._with_retry("on", lambda d: d.on())

    async def turn_off(self) -> bool:
        """
        Send OFF command. Returns True if successful.
        Retries up to 3 times — important for safety.
        """
        return await self._with_retry("off", lambda d: d.off())

    async def is_online(self) -> bool:
        """Return True if the plug is reachable and responding."""
        try:
            await self._ensure_connected()
            await self._device.get_device_info()
            return True
        except Exception:
            self._device = None
            return False

    async def get_power_watts(self) -> Optional[float]:
        """
        Return current power draw in watts, or None if unavailable.
        The P110M reports 0 W when no load is connected.
        """
        try:
            await self._ensure_connected()
            power = await self._device.get_current_power()
            return float(power.current_power)
        except Exception as e:
            log.debug("Power read failed: %s", e)
            return None

    async def get_device_status(self) -> dict:
        """
        Return a status dict for the dashboard Devices panel.
        Always returns a dict — sets reachable=False on any error.
        """
        try:
            await self._ensure_connected()
            info = await self._device.get_device_info()
            power = await self._device.get_current_power()
            return {
                "plug_reachable": True,
                "plug_on": info.device_on,
                "plug_power_w": float(power.current_power),
                "plug_model": info.model,
                "plug_nickname": getattr(info, "nickname", None),
            }
        except Exception as e:
            self._device = None
            return {
                "plug_reachable": False,
                "plug_on": False,
                "plug_power_w": None,
                "plug_model": None,
                "plug_error": str(e),
            }

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _ensure_connected(self):
        if self._device is None:
            if not await self.connect():
                raise RuntimeError("Plug offline — reconnect failed")

    async def _with_retry(self, action_name: str, action) -> bool:
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                await self._ensure_connected()
                await action(self._device)
                log.info("Plug %s OK", action_name)
                return True
            except Exception as e:
                log.warning(
                    "Plug %s attempt %d/%d failed: %s",
                    action_name, attempt, _RETRY_ATTEMPTS, e,
                )
                self._device = None
                if attempt < _RETRY_ATTEMPTS:
                    await asyncio.sleep(_RETRY_DELAY_S)
        log.error("Plug %s failed after %d attempts", action_name, _RETRY_ATTEMPTS)
        return False
