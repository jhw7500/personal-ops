"""Read-only object status needed after a combined schema update."""
import json
from pathlib import Path
import tempfile
import unittest

from host_runtime import (
    PREDECESSOR_FUNCTIONS,
    PREDECESSOR_INDEXES,
    PREDECESSOR_NAMES,
    PREDECESSOR_TABLES,
    SCHEMA_013_TABLES,
)
from tests.test_host_predecessor_migration import (
    GENERATION,
    ReadOnlyRuntime,
    source_fixture,
    source_fixture_checksums,
)


class AppliedRuntime(ReadOnlyRuntime):
    def __init__(self, root, *, include_013, missing_object=None, missing_function=None,
                 unexpected=0, history_override=None):
        super().__init__(root, unexpected=unexpected)
        self.include_013 = include_013
        self.missing_object = missing_object
        self.missing_function = missing_function
        self.history_override = history_override or {}

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        query = argv[-1]
        checksums = source_fixture_checksums(self.root)
        history = {
            name: ("checksum_match" if index < 12 or self.include_013 else "missing")
            for index, name in enumerate(PREDECESSOR_NAMES)
        }
        history.update(self.history_override)
        objects = {
            name: (name in PREDECESSOR_TABLES or name in PREDECESSOR_INDEXES
                   or (self.include_013 and name in SCHEMA_013_TABLES))
            for name in (*PREDECESSOR_TABLES, *PREDECESSOR_INDEXES, *SCHEMA_013_TABLES)
        }
        if self.missing_object is not None:
            objects[self.missing_object] = False
        functions = {name: True for name in PREDECESSOR_FUNCTIONS}
        if self.missing_function is not None:
            functions[self.missing_function] = False
        payload = {
            "history": history,
            "unexpected_history_count": self.unexpected,
            "objects": objects,
            "functions": functions,
            "generation": GENERATION,
        }
        assert all(checksum in query for checksum in checksums.values())
        return ("BEGIN\n" + json.dumps(payload) + "\nCOMMIT\n").encode()


class PredecessorObjectsTest(unittest.TestCase):
    def test_status_reports_indexes_and_functions_independently_of_complete(self):
        with tempfile.TemporaryDirectory(prefix="predecessor-objects-") as folder:
            root = Path(folder)
            source_fixture(root)
            index = PREDECESSOR_INDEXES[0]
            status = ReadOnlyRuntime(root, collision=index).predecessor_status(
                "tossApp", "tarororo-test")
            self.assertIn("indexes_present", status)
            self.assertIn("functions_present", status)
            self.assertTrue(status["indexes_present"][index])
            self.assertFalse(all(status["indexes_present"].values()))
            self.assertFalse(any(status["functions_present"].values()))
            function = PREDECESSOR_FUNCTIONS[0]
            status = ReadOnlyRuntime(root, collision=function).predecessor_status(
                "tossApp", "tarororo-test")
            self.assertTrue(status["functions_present"][function])
            self.assertFalse(any(status["indexes_present"].values()))
            self.assertFalse(status["complete"])

    def test_completed_006_012_is_not_reported_as_object_collisions(self):
        with tempfile.TemporaryDirectory(prefix="predecessor-objects-") as folder:
            root = Path(folder)
            source_fixture(root)

            status = AppliedRuntime(root, include_013=False).predecessor_status(
                "tossApp", "tarororo-test")

            self.assertTrue(status["complete"])
            self.assertEqual(status["completion_scope"], "006-012")
            self.assertEqual(status["object_collisions"], [])
            self.assertEqual(status["function_collisions"], [])
            self.assertFalse(status["ready_to_prepare"])

    def test_completed_006_013_is_not_reported_as_incomplete_or_colliding(self):
        with tempfile.TemporaryDirectory(prefix="predecessor-objects-") as folder:
            root = Path(folder)
            source_fixture(root)

            status = AppliedRuntime(root, include_013=True).predecessor_status(
                "tossApp", "tarororo-test")

            self.assertTrue(status["complete"])
            self.assertEqual(status["completion_scope"], "006-013")
            self.assertEqual(status["object_collisions"], [])
            self.assertEqual(status["function_collisions"], [])
            self.assertFalse(status["ready_to_prepare"])

    def test_completed_history_still_rejects_partial_mismatch_and_unexpected_state(self):
        scenarios = (
            {"missing_object": PREDECESSOR_INDEXES[0]},
            {"missing_function": PREDECESSOR_FUNCTIONS[0]},
            {"history_override": {PREDECESSOR_NAMES[12]: "checksum_mismatch"}},
            {"unexpected": 1},
        )
        with tempfile.TemporaryDirectory(prefix="predecessor-objects-") as folder:
            root = Path(folder)
            source_fixture(root)
            for scenario in scenarios:
                with self.subTest(scenario=scenario):
                    status = AppliedRuntime(root, include_013=True, **scenario).predecessor_status(
                        "tossApp", "tarororo-test")
                    self.assertFalse(status["complete"])
                    self.assertIsNone(status["completion_scope"])
                    self.assertFalse(status["ready_to_prepare"])


if __name__ == "__main__":
    unittest.main()
