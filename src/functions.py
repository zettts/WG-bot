import aiohttp
import logging

from config import config

logger = logging.getLogger(__name__)

PANEL_BASE_URL = "http://localhost:51821"
PANEL_PASSWORD = config.PANEL_PASSWORD


class PanelAPI:
    """Клиент для нового API awg-easy-3 (вместо старого 3x-ui)."""

    def __init__(self):
        self.session = None

    async def _ensure_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
        async with self.session.get(f"{PANEL_BASE_URL}/api/v1/session") as resp:
            data = await resp.json()
            if not data.get("authenticated"):
                await self.login()

    async def login(self) -> bool:
        if self.session is None:
            self.session = aiohttp.ClientSession()
        try:
            async with self.session.post(
                f"{PANEL_BASE_URL}/api/v1/session",
                json={"password": PANEL_PASSWORD},
            ) as resp:
                if resp.status != 200:
                    logger.error(f"Panel login failed: {resp.status}")
                    return False
                logger.info("Panel login successful")
                return True
        except Exception as e:
            logger.exception(f"Panel login error: {e}")
            return False

    async def create_client(self, telegram_id: int):
        await self._ensure_session()
        name = f"user_{telegram_id}"
        try:
            async with self.session.post(
                f"{PANEL_BASE_URL}/api/v1/clients",
                json={"name": name, "networkGroup": "guest"},
            ) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    client_id = data["client"]["id"]
                    logger.info(f"Created client {name} ({client_id})")
                    return client_id
                if resp.status == 409:
                    logger.warning(f"Client {name} already exists")
                    return await self.find_client_id_by_name(name)
                logger.error(f"Create client failed: {resp.status}")
                return None
        except Exception as e:
            logger.exception(f"Create client error: {e}")
            return None

    async def find_client_id_by_name(self, name: str):
        await self._ensure_session()
        try:
            async with self.session.get(f"{PANEL_BASE_URL}/api/v1/clients") as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                for client in data:
                    if client.get("name") == name:
                        return client.get("id")
                return None
        except Exception as e:
            logger.exception(f"Find client error: {e}")
            return None

    async def export_client_config(self, client_id: str):
        await self._ensure_session()
        try:
            async with self.session.get(
                f"{PANEL_BASE_URL}/api/v1/clients/{client_id}/export",
                params={"format": "native-config"},
            ) as resp:
                if resp.status != 200:
                    logger.error(f"Export client failed: {resp.status}")
                    return None
                return await resp.text()
        except Exception as e:
            logger.exception(f"Export client error: {e}")
            return None

    async def export_client_vpn_link(self, client_id: str):
        await self._ensure_session()
        try:
            async with self.session.get(
                f"{PANEL_BASE_URL}/api/v1/clients/{client_id}/export",
                params={"format": "vpn-link"},
            ) as resp:
                if resp.status != 200:
                    logger.error(f"Export vpn-link failed: {resp.status}")
                    return None
                return await resp.text()
        except Exception as e:
            logger.exception(f"Export vpn-link error: {e}")
            return None

    async def delete_client(self, client_id: str) -> bool:
        await self._ensure_session()
        try:
            async with self.session.delete(f"{PANEL_BASE_URL}/api/v1/clients/{client_id}") as resp:
                if resp.status == 200:
                    logger.info(f"Deleted client {client_id}")
                    return True
                logger.error(f"Delete client failed: {resp.status}")
                return False
        except Exception as e:
            logger.exception(f"Delete client error: {e}")
            return False

    async def close(self):
        if self.session:
            await self.session.close()


async def create_awg_profile(telegram_id: int):
    api = PanelAPI()
    try:
        client_id = await api.create_client(telegram_id)
        if not client_id:
            return None
        config = await api.export_client_config(client_id)
        if not config:
            return None
        vpn_link = await api.export_client_vpn_link(client_id)
        return {"client_id": client_id, "config": config, "vpn_link": vpn_link}
    finally:
        await api.close()


async def delete_client_by_id(client_id: str) -> bool:
    api = PanelAPI()
    try:
        return await api.delete_client(client_id)
    finally:
        await api.close()


async def get_client_stats(client_id: str) -> dict:
    """Статистика одного клиента: онлайн/офлайн, скорость, время последнего handshake."""
    api = PanelAPI()
    try:
        await api._ensure_session()
        async with api.session.get(f"{PANEL_BASE_URL}/api/v1/diagnostics") as resp:
            if resp.status != 200:
                return {"state": "unknown"}
            data = await resp.json()
            for entry in data:
                if entry.get("id") == client_id:
                    return entry
            return {"state": "not_found"}
    except Exception as e:
        logger.exception(f"Get client stats error: {e}")
        return {"state": "error"}
    finally:
        await api.close()


async def get_online_users() -> int:
    """Количество клиентов, у которых недавний handshake (см. state == 'online')."""
    api = PanelAPI()
    try:
        await api._ensure_session()
        async with api.session.get(f"{PANEL_BASE_URL}/api/v1/diagnostics") as resp:
            if resp.status != 200:
                return 0
            data = await resp.json()
            return sum(1 for entry in data if entry.get("state") == "online")
    except Exception as e:
        logger.exception(f"Get online users error: {e}")
        return 0
    finally:
        await api.close()


async def get_global_stats() -> dict:
    """Суммарная скорость по всем клиентам сейчас (бит/с)."""
    api = PanelAPI()
    try:
        await api._ensure_session()
        async with api.session.get(f"{PANEL_BASE_URL}/api/v1/diagnostics") as resp:
            if resp.status != 200:
                return {"download": 0, "upload": 0}
            data = await resp.json()
            download = sum(e.get("downloadBps") or 0 for e in data)
            upload = sum(e.get("uploadBps") or 0 for e in data)
            return {"download": download, "upload": upload}
    except Exception as e:
        logger.exception(f"Get global stats error: {e}")
        return {"download": 0, "upload": 0}
    finally:
        await api.close()


async def create_static_client(profile_name: str):
    """Создаёт клиента с произвольным именем (для админских/статических профилей)."""
    api = PanelAPI()
    try:
        await api._ensure_session()
        async with api.session.post(
            f"{PANEL_BASE_URL}/api/v1/clients",
            json={"name": profile_name, "networkGroup": "guest"},
        ) as resp:
            if resp.status == 201:
                data = await resp.json()
                client_id = data["client"]["id"]
            elif resp.status == 409:
                client_id = await api.find_client_id_by_name(profile_name)
                if not client_id:
                    return None
            else:
                logger.error(f"Create static client failed: {resp.status}")
                return None
        config = await api.export_client_config(client_id)
        if not config:
            return None
        vpn_link = await api.export_client_vpn_link(client_id)
        return {"client_id": client_id, "config": config, "vpn_link": vpn_link}
    except Exception as e:
        logger.exception(f"Create static client error: {e}")
        return None
    finally:
        await api.close()


async def delete_client_by_name(name: str) -> bool:
    """Находит клиента по имени и удаляет его (для статических профилей)."""
    api = PanelAPI()
    try:
        client_id = await api.find_client_id_by_name(name)
        if not client_id:
            return False
        return await api.delete_client(client_id)
    finally:
        await api.close()


async def set_client_enabled(client_id: str, enabled: bool) -> bool:
    """Включает/отключает клиента в панели, не трогая его ключи/конфиг."""
    api = PanelAPI()
    try:
        await api._ensure_session()
        async with api.session.patch(
            f"{PANEL_BASE_URL}/api/v1/clients/{client_id}",
            json={"enabled": enabled},
        ) as resp:
            if resp.status == 200:
                logger.info(f"Client {client_id} enabled={enabled}")
                return True
            logger.error(f"Set client enabled failed: {resp.status}")
            return False
    except Exception as e:
        logger.exception(f"Set client enabled error: {e}")
        return False
    finally:
        await api.close()
