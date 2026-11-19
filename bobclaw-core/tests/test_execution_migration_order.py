"""First boot applies migrations in sorted filename order; core applies its own list.

A dependent migration that sorts before its parent breaks every fresh packaged install.
"""
from pathlib import Path

from core.execution.store import EXECUTION_MIGRATIONS, MIGRATION

ROOT = MIGRATION.parent


def test_execution_migrations_exist_and_sort_in_dependency_order():
    for name in EXECUTION_MIGRATIONS:
        assert (ROOT / name).is_file(), name
    firstboot_order = [p.name for p in sorted(ROOT.glob("*.sql")) if p.name in EXECUTION_MIGRATIONS]
    assert firstboot_order == list(EXECUTION_MIGRATIONS)


def test_every_execution_migration_is_applied_by_core():
    names = {p.name for p in ROOT.glob("2026-11-*.sql")}
    assert names == set(EXECUTION_MIGRATIONS)
