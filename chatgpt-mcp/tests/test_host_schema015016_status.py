"""Offline-only tests for the pinned 015/016 READ-ONLY schema inspector.

No real PostgreSQL, Docker daemon, credential, app or network is contacted.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from host_runtime import PREDECESSOR_NAMES
from host_runtime_config import HostRuntimeError
from host_schema014 import EXPECTED_NAMES, FIXED_NAME
from host_schema015016 import (
    DB, DB_USER, FIXED_SOURCES, OBJECTS, expected_sources,
    fixed_query, schema015016_status,
)
from tests.test_host_schema014 import REVIEWED_SQL


SOURCE_015 = """-- Public free-video reading stage unlocks; no advertisement or payment entitlement.
-- Shared DB deployment requires a separately approved and reviewed migration.
CREATE TABLE reading_demo_video_unlocks (
  user_id uuid NOT NULL,
  reading_id uuid NOT NULL,
  stage text NOT NULL CHECK (stage IN ('CARD_2','CARD_3','OVERALL')),
  unlocked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (user_id,reading_id,stage),
  FOREIGN KEY (reading_id,user_id) REFERENCES readings(id,user_id) ON DELETE RESTRICT
);
"""
SOURCE_016 = """-- Rewarded content access from client SDK events; NOT provider-verified SSV receipts.
-- Source registration is not permission to apply to a shared or production database.
CREATE TABLE reading_rewarded_ad_attempts (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL,
  reading_id uuid NOT NULL,
  stage text NOT NULL CHECK (stage IN ('CARD_2','CARD_3','OVERALL')),
  mode text NOT NULL CHECK (mode IN ('sdk-test','live')),
  ad_group_id text NOT NULL,
  policy_hash char(64) NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
  completion_token_hash char(64) NOT NULL CHECK (completion_token_hash ~ '^[0-9a-f]{64}$'),
  state text NOT NULL DEFAULT 'ISSUED' CHECK (state IN ('ISSUED','COMPLETED')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL,
  completed_at timestamptz,
  FOREIGN KEY (reading_id,user_id) REFERENCES readings(id,user_id) ON DELETE RESTRICT,
  CHECK (expires_at > created_at),
  CHECK ((state='COMPLETED') = (completed_at IS NOT NULL)),
  CHECK ((mode='sdk-test' AND ad_group_id='ait-ad-test-rewarded-id') OR
         (mode='live' AND ad_group_id <> 'ait-ad-test-rewarded-id'))
);
CREATE UNIQUE INDEX reading_rewarded_stage_once
  ON reading_rewarded_ad_attempts(user_id,reading_id,mode,stage) WHERE state='COMPLETED';
CREATE INDEX reading_rewarded_attempt_rate ON reading_rewarded_ad_attempts(user_id,created_at);
"""
SQL_FILES = {
    "015_public_demo_video_unlocks.sql": SOURCE_015,
    "016_rewarded_ad_attempts.sql": SOURCE_016,
}
GENERATION = {"database": DB, "user": DB_USER, "postmasterStartEpoch": "1792379543.782441"}


class FakeRuntime:
    def __init__(self, root, expected):
        self.root = root
        self.expected = expected
        self.applied = set()
        self.present = set()
        self.other_history = 0
        self.bad_history = None
        self.malformed = False
        self.calls = []

    def guard(self, project, target):
        if (project, target) != ("tossApp", "tarororo-test"):
            raise HostRuntimeError("UNSUPPORTED_SCOPE")
        return self.root

    def identities(self):
        return {role: {"id": role + "-id", "started_at": role + "-started",
                       "image_id": role + "-image"}
                for role in ("api", "worker", "db")}

    def _verify_identity(self, identities, *, health):
        assert health and identities["db"]["id"] == "db-id"

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        assert argv[:4] == ["docker", "exec", "-i", "tarororo-test-db-1"]
        sql = argv[-1]
        assert "BEGIN READ ONLY;" in sql
        assert "fixed_schema015016_status_v1" in sql
        assert sql.endswith("COMMIT;")
        assert kwargs["timeout"] == 12
        for disallowed in ("DROP TABLE", "INSERT INTO", "UPDATE public", "DELETE FROM", "CREATE TABLE"):
            assert disallowed not in sql, disallowed
        history = {name: "checksum_match" for name in EXPECTED_NAMES}
        history.update({
            name: ("checksum_match" if name in self.applied else "missing")
            for name in FIXED_SOURCES
        })
        if self.bad_history is not None:
            history[self.bad_history] = "checksum_mismatch"
        payload = {
            "history": history,
            "unexpected": self.other_history,
            "objects": {name: name in self.present for name in OBJECTS},
            "generation": GENERATION,
        }
        if self.malformed:
            payload["objects"]["reading_demo_video_unlocks"] = "true"
        return (json.dumps(payload) + "\n").encode()


class Schema015016ReadOnlyTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="schema015016-readonly-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "tossApp"
        migrations = self.root / "server" / "migrations"
        migrations.mkdir(parents=True)
        self.assertEqual(sha256(REVIEWED_SQL.encode()).hexdigest(),
                         "a7b470a05007543be8751aec6d2ea8cd88891b08aa736bc72112b7417d56dac2")
        (migrations / FIXED_NAME).write_text(REVIEWED_SQL)
        expected = {}
        for i, name in enumerate(PREDECESSOR_NAMES):
            value = f"-- fixture {i}\nSELECT 1;\n".encode()
            (migrations / name).write_bytes(value)
            expected[name] = sha256(value).hexdigest()
        expected[FIXED_NAME] = sha256(REVIEWED_SQL.encode()).hexdigest()
        for name, sql in SQL_FILES.items():
            self.assertEqual(sha256(sql.encode()).hexdigest(), FIXED_SOURCES[name][0])
            (migrations / name).write_text(sql)
            expected[name] = FIXED_SOURCES[name][0]
        self.expected = expected
        self.runtime = FakeRuntime(self.root, expected)

    def test_pinned_sql_hashes_and_only_readonly_query(self):
        self.assertEqual(expected_sources(self.root), self.expected)
        query = fixed_query(self.expected)
        self.assertIn("BEGIN READ ONLY;", query)
        self.assertIn("SET LOCAL statement_timeout='8s';", query)
        for name in self.expected:
            self.assertIn(name, query)
        for name in OBJECTS:
            self.assertIn(name, query)
        self.assertNotIn("CREATE TABLE", query)
        self.assertNotIn("INSERT INTO", query)

    def test_missing_history_and_absent_objects_are_a_plan_precondition_only(self):
        response = schema015016_status(self.runtime, "tossApp", "tarororo-test")
        self.assertEqual(response["state"], "missing")
        self.assertTrue(response["ready_for_plan"])
        self.assertFalse(response["history_and_objects_match"])
        self.assertEqual(set(response["history"].values()), {"missing"})
        self.assertTrue(all(not value for value in response["objects_present"].values()))
        self.assertEqual(response["generation"], GENERATION)
        self.assertEqual(response["database_identity"]["container_id"], "db-id")
        self.assertIn("not DDL constraint", response["limitations"])
        self.assertEqual(len(self.runtime.calls), 1)

    def test_recorded_both_and_objects_is_bounded_not_full_ddl_proof(self):
        self.runtime.applied.update(FIXED_SOURCES)
        self.runtime.present.update(OBJECTS)
        response = schema015016_status(self.runtime, "tossApp", "tarororo-test")
        self.assertEqual(response["state"], "recorded_objects_present")
        self.assertTrue(response["history_and_objects_match"])
        self.assertFalse(response["ready_for_plan"])
        self.assertEqual(set(response["history"].values()), {"checksum_match"})

    def test_partial_or_collision_or_unexpected_history_fail_closed(self):
        for category in ("partial", "collision", "extra", "bad_previous", "bad_new"):
            with self.subTest(category=category):
                self.runtime.applied.clear()
                self.runtime.present.clear()
                self.runtime.other_history = 0
                self.runtime.bad_history = None
                if category == "partial":
                    self.runtime.applied.add(next(iter(FIXED_SOURCES)))
                    self.runtime.present.add(OBJECTS[0])
                elif category == "collision":
                    self.runtime.present.add(OBJECTS[0])
                elif category == "extra":
                    self.runtime.other_history = 1
                elif category == "bad_previous":
                    self.runtime.bad_history = FIXED_NAME
                elif category == "bad_new":
                    self.runtime.bad_history = next(iter(FIXED_SOURCES))
                response = schema015016_status(self.runtime, "tossApp", "tarororo-test")
                self.assertEqual(response["state"], "inconsistent")
                self.assertFalse(response["ready_for_plan"])

    def test_invalid_response_fails_closed(self):
        self.runtime.malformed = True
        with self.assertRaises(HostRuntimeError) as caught:
            schema015016_status(self.runtime, "tossApp", "tarororo-test")
        self.assertEqual(caught.exception.code, "SCHEMA015016_STATUS_UNKNOWN")

    def test_tampered_source_and_symlink_must_not_contact_database(self):
        source = self.root / "server/migrations/015_public_demo_video_unlocks.sql"
        source.write_text("DROP TABLE readings;\n")
        with self.assertRaises(HostRuntimeError) as caught:
            schema015016_status(self.runtime, "tossApp", "tarororo-test")
        self.assertEqual(caught.exception.code, "SCHEMA015016_SOURCE_NOT_APPROVED")
        self.assertEqual(self.runtime.calls, [])
        source.unlink()
        source.symlink_to(self.root / "server/migrations/016_rewarded_ad_attempts.sql")
        with self.assertRaises(HostRuntimeError):
            schema015016_status(self.runtime, "tossApp", "tarororo-test")
        self.assertEqual(self.runtime.calls, [])

    def test_scope_denied_before_source_and_database(self):
        with self.assertRaises(HostRuntimeError) as caught:
            schema015016_status(self.runtime, "another", "tarororo-test")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_SCOPE")
        self.assertEqual(self.runtime.calls, [])


if __name__ == "__main__":
    unittest.main()
