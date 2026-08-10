"""
Universe preference migration test — the critical case: a database that
already exists from BEFORE this migration was written, with a real user
account already in it (exactly the situation on the live Railway
deployment). A naive schema change would silently fail to reach an
already-created table; this proves the ALTER TABLE migration actually
runs and existing data survives intact.
"""

from __future__ import annotations
import sqlite3
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.app_storage import AppStorage
from opportunity_scanner.auth_utils import hash_password


def main():
    db_path = "/tmp/test_universe_migration.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    # 1. Simulate a database created by the OLD schema (pre-migration) —
    # raw SQL matching exactly what existed before this session, with a
    # real user row already in it, same as the live deployment.
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            stripe_customer_id TEXT,
            subscription_status TEXT NOT NULL DEFAULT 'none',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO users (email, password_hash, plan, subscription_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("existinguser@example.com", hash_password("password123"), "pro", "active", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()
    print("1. Simulated a pre-migration database with a real existing user, exactly like the live deployment: OK")

    # 2. Open it with the NEW AppStorage — the migration must run without
    # crashing or losing the existing row
    storage = AppStorage(db_path)
    existing_user = storage.get_user_by_email("existinguser@example.com")
    assert existing_user is not None, "The existing user must survive the migration"
    assert existing_user.plan.value == "pro", "Existing user's real data (plan) must be preserved exactly"
    print(f"2. Migration ran without crashing, existing user's data survived intact (plan={existing_user.plan.value}): OK")

    # 3. New columns exist with sensible defaults for a pre-existing row
    assert existing_user.last_universe_preset == "high_liquidity", f"Expected default 'high_liquidity', got {existing_user.last_universe_preset}"
    assert existing_user.last_universe_custom is None
    print("3. New columns correctly default to 'high_liquidity' / None for a row that existed before the migration: OK")

    # 4. save_universe_preference() works correctly on the migrated table
    saved = storage.save_universe_preference(existing_user.id, "majors", None)
    assert saved is True
    updated_user = storage.get_user_by_id(existing_user.id)
    assert updated_user.last_universe_preset == "majors"
    print("4. save_universe_preference() correctly updates the migrated column: OK")

    # 5. Custom preset with a symbol list persists correctly
    storage.save_universe_preference(existing_user.id, "custom", "BTC,ETH,SOL,PEPE")
    custom_user = storage.get_user_by_id(existing_user.id)
    assert custom_user.last_universe_preset == "custom"
    assert custom_user.last_universe_custom == "BTC,ETH,SOL,PEPE"
    print("5. Custom preset with a saved symbol list persists correctly: OK")

    # 6. Running the migration AGAIN (e.g. app restart) on an already-migrated
    # database is a safe no-op, not an error
    storage2 = AppStorage(db_path)
    still_there = storage2.get_user_by_email("existinguser@example.com")
    assert still_there is not None
    assert still_there.last_universe_preset == "custom"
    print("6. Re-running the migration on an already-migrated database is a safe no-op (app restart doesn't break anything): OK")

    os.remove(db_path)
    print("\n✅ Universe migration test passed: the critical pre-existing-database case works correctly — no data loss, safe to re-run, existing users get sensible defaults.")


if __name__ == "__main__":
    main()
