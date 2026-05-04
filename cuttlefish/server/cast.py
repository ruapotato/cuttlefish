"""In-memory pub/sub bus for the casting feature.

Each connected device is one WebSocket. It registers itself with a unique
client_id, role ('target' or 'controller'), and label (e.g. 'Living Room
TV'). Events flow per-user: a controller's commands fan out only to the
targets owned by that same user.

Intentionally in-process and stateless across restarts. On reconnect,
clients re-announce themselves. Persistence + presence tracking is
explicitly NOT a feature here — see docs/casting.md.
"""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Optional

from starlette.websockets import WebSocket


@dataclass
class CastDevice:
    client_id: str
    user_id: int
    role: str          # 'target' | 'controller'
    label: str
    socket: WebSocket


@dataclass
class CastBus:
    """Per-process map of {user_id: {client_id: CastDevice}}."""
    devices: dict[int, dict[str, CastDevice]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def register(self, user_id: int, role: str, label: str, socket: WebSocket) -> CastDevice:
        client_id = secrets.token_hex(8)
        dev = CastDevice(client_id=client_id, user_id=user_id, role=role,
                         label=label, socket=socket)
        async with self._lock:
            self.devices.setdefault(user_id, {})[client_id] = dev
        if role == "target":
            await self._broadcast_user(
                user_id,
                {
                    "type": "target_available",
                    "client_id": client_id,
                    "label": label,
                },
                except_id=client_id,
            )
        return dev

    async def unregister(self, dev: CastDevice) -> None:
        async with self._lock:
            user_devs = self.devices.get(dev.user_id) or {}
            user_devs.pop(dev.client_id, None)
            if not user_devs:
                self.devices.pop(dev.user_id, None)
        if dev.role == "target":
            await self._broadcast_user(
                dev.user_id,
                {"type": "target_gone", "client_id": dev.client_id},
                except_id=dev.client_id,
            )

    def list_for(
        self, user_id: int, role: Optional[str] = None,
        except_id: Optional[str] = None,
    ) -> list[dict]:
        out = []
        for d in (self.devices.get(user_id) or {}).values():
            if role and d.role != role:
                continue
            if except_id and d.client_id == except_id:
                continue
            out.append({"client_id": d.client_id, "role": d.role, "label": d.label})
        return out

    async def send_to(self, user_id: int, client_id: str, message: dict) -> bool:
        async with self._lock:
            dev = (self.devices.get(user_id) or {}).get(client_id)
        if dev is None:
            return False
        try:
            await dev.socket.send_json(message)
            return True
        except Exception:
            return False

    async def _broadcast_user(
        self, user_id: int, message: dict, except_id: Optional[str] = None
    ) -> None:
        async with self._lock:
            sockets = [
                d.socket
                for d in (self.devices.get(user_id) or {}).values()
                if d.client_id != except_id
            ]
        for s in sockets:
            try:
                await s.send_json(message)
            except Exception:
                continue
