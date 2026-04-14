from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ncoai_shared.controlplane import ControlPlaneRepository
from ncoai_shared.security import hash_password
from QPS_test.common import ManagedUserCredential, RunConfig, serialize_datetime, utcnow


class ManagedUserBatchManager:
    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self._engine: AsyncEngine = create_async_engine(config.database_async_url)
        self._repo = ControlPlaneRepository(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    def _inventory_path(self) -> Path:
        return self.config.paths.users_json

    def _load_existing_inventory(self) -> list[ManagedUserCredential] | None:
        path = self._inventory_path()
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return None
        return [ManagedUserCredential(**item) for item in payload]

    async def _all_credentials_active(
        self,
        credentials: list[ManagedUserCredential],
    ) -> bool:
        if not credentials:
            return False

        semaphore = asyncio.Semaphore(self.config.login_batch_size)

        async def check_one(item: ManagedUserCredential) -> bool:
            async with semaphore:
                user = await self._repo.get_managed_user(
                    item.identifier,
                    include_deleted=True,
                )
            return bool(user and user.get("is_active") and not user.get("is_deleted"))

        results = await asyncio.gather(*(check_one(item) for item in credentials))
        return all(results)

    def _build_credentials(
        self,
        *,
        user_count: int,
        shared_password: str,
    ) -> list[ManagedUserCredential]:
        created_at = serialize_datetime(utcnow()) or ""
        credentials: list[ManagedUserCredential] = []
        for index in range(1, user_count + 1):
            identifier = f"qps-test-{self.config.run_id}-u{index:04d}"
            credentials.append(
                ManagedUserCredential(
                    identifier=identifier,
                    password=shared_password,
                    display_name=identifier,
                    run_id=self.config.run_id,
                    created_at=created_at,
                )
            )
        return credentials

    async def create_users(self, *, user_count: int) -> list[ManagedUserCredential]:
        existing = self._load_existing_inventory()
        if existing and len(existing) == user_count and await self._all_credentials_active(existing):
            return existing

        shared_password = f"QpsTest!{self.config.run_id[:12]}"
        password_hash = hash_password(shared_password)
        credentials = self._build_credentials(
            user_count=user_count,
            shared_password=shared_password,
        )

        semaphore = asyncio.Semaphore(self.config.login_batch_size)

        async def create_one(item: ManagedUserCredential) -> None:
            async with semaphore:
                await self._repo.create_managed_user(
                    identifier=item.identifier,
                    display_name=item.display_name,
                    password_hash=password_hash,
                    is_active=True,
                )

        await asyncio.gather(*(create_one(item) for item in credentials))
        self._inventory_path().write_text(
            json.dumps([asdict(item) for item in credentials], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return credentials

    async def list_prefixed_users(
        self,
        *,
        identifier_prefix: str = "qps-test-",
        include_deleted: bool = True,
    ) -> list[dict[str, object]]:
        users = await self._repo.list_managed_users(include_deleted=include_deleted)
        return [
            item
            for item in users
            if str(item.get("identifier") or "").startswith(identifier_prefix)
        ]

    async def soft_delete_users(
        self,
        credentials: list[ManagedUserCredential],
    ) -> int:
        deleted = 0
        semaphore = asyncio.Semaphore(self.config.login_batch_size)

        async def delete_one(item: ManagedUserCredential) -> None:
            nonlocal deleted
            async with semaphore:
                await self._repo.soft_delete_managed_user(item.identifier)
                deleted += 1

        await asyncio.gather(*(delete_one(item) for item in credentials))
        return deleted

    async def soft_delete_prefixed_users(
        self,
        *,
        identifier_prefix: str = "qps-test-",
    ) -> int:
        users = await self.list_prefixed_users(
            identifier_prefix=identifier_prefix,
            include_deleted=True,
        )
        active_users = [
            item
            for item in users
            if not bool(item.get("is_deleted"))
        ]
        deleted = 0
        semaphore = asyncio.Semaphore(self.config.login_batch_size)

        async def delete_one(item: dict[str, object]) -> None:
            nonlocal deleted
            identifier = str(item.get("identifier") or "")
            if not identifier:
                return
            async with semaphore:
                await self._repo.soft_delete_managed_user(identifier)
                deleted += 1

        await asyncio.gather(*(delete_one(item) for item in active_users))
        return deleted
