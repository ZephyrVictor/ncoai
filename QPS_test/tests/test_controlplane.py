from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from QPS_test.common import ManagedUserCredential
from QPS_test.controlplane import ManagedUserBatchManager


class _FakeRepo:
    def __init__(self, *, users_by_identifier=None, listed_users=None) -> None:
        self.users_by_identifier = users_by_identifier or {}
        self.listed_users = listed_users or []
        self.created: list[str] = []
        self.deleted: list[str] = []

    async def get_managed_user(self, identifier: str, *, include_deleted: bool = False):
        return self.users_by_identifier.get(identifier)

    async def create_managed_user(
        self,
        identifier: str,
        display_name: str,
        password_hash: str,
        is_active: bool,
    ) -> None:
        self.created.append(identifier)

    async def list_managed_users(self, *, include_deleted: bool = False):
        return list(self.listed_users)

    async def soft_delete_managed_user(self, identifier: str) -> None:
        self.deleted.append(identifier)


def _manager(tmp_path: Path, *, run_id: str = "test-run", repo: _FakeRepo) -> ManagedUserBatchManager:
    manager = object.__new__(ManagedUserBatchManager)
    manager.config = SimpleNamespace(
        run_id=run_id,
        login_batch_size=4,
        paths=SimpleNamespace(users_json=tmp_path / "users.json"),
    )
    manager._repo = repo
    manager._engine = None
    return manager


def _write_inventory(path: Path, identifiers: list[str]) -> None:
    payload = [
        {
            "identifier": identifier,
            "password": "secret",
            "display_name": identifier,
            "run_id": "test-run",
            "created_at": "2026-04-11T00:00:00+00:00",
        }
        for identifier in identifiers
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_create_users_reuses_inventory_when_accounts_are_active(tmp_path: Path) -> None:
    identifiers = ["qps-test-test-run-u0001", "qps-test-test-run-u0002"]
    inventory_path = tmp_path / "users.json"
    _write_inventory(inventory_path, identifiers)
    repo = _FakeRepo(
        users_by_identifier={
            identifier: {"identifier": identifier, "is_active": True, "is_deleted": False}
            for identifier in identifiers
        }
    )
    manager = _manager(tmp_path, repo=repo)

    credentials = asyncio.run(manager.create_users(user_count=2))

    assert [item.identifier for item in credentials] == identifiers
    assert repo.created == []


def test_create_users_recreates_inventory_when_accounts_were_deleted(tmp_path: Path) -> None:
    identifiers = ["qps-test-test-run-u0001", "qps-test-test-run-u0002"]
    inventory_path = tmp_path / "users.json"
    _write_inventory(inventory_path, identifiers)
    repo = _FakeRepo(
        users_by_identifier={
            identifier: {"identifier": identifier, "is_active": False, "is_deleted": True}
            for identifier in identifiers
        }
    )
    manager = _manager(tmp_path, repo=repo)

    credentials = asyncio.run(manager.create_users(user_count=2))

    assert len(credentials) == 2
    assert repo.created == identifiers


def test_soft_delete_prefixed_users_ignores_deleted_and_non_matching(tmp_path: Path) -> None:
    repo = _FakeRepo(
        listed_users=[
            {"identifier": "qps-test-a", "is_deleted": False},
            {"identifier": "qps-test-b", "is_deleted": True},
            {"identifier": "someone-else", "is_deleted": False},
        ]
    )
    manager = _manager(tmp_path, repo=repo)

    deleted = asyncio.run(manager.soft_delete_prefixed_users())

    assert deleted == 1
    assert repo.deleted == ["qps-test-a"]
