#!/usr/bin/env python3
"""Run official rollout unittests with the standing Yocto exclusions."""
from __future__ import annotations

import argparse
import sys
import unittest


EXCLUDED_TEST_METHODS = (
    "test_installed_bitbake_denies_indirect_shell_commands_without_execution",
    "test_rejects_bitbake_commands_and_yocto_repositories",
    "test_rejects_unambiguous_yocto_cache_markers",
    "test_allows_generic_tmp_downloads_and_stamps_directories",
    "test_rejects_specific_yocto_tmp_layouts_and_executable",
)
_EXCLUDED_SUFFIXES = frozenset(
    "ExecutionPolicyTest." + method for method in EXCLUDED_TEST_METHODS
)
_EXECUTION_MODULES = frozenset({"test_execution", "tests.test_execution"})


def _cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _cases(item)
        else:
            yield item


def _is_excluded(test_id: str) -> bool:
    parts = test_id.rsplit(".", 2)
    return (len(parts) == 3 and parts[0] in _EXECUTION_MODULES
            and ".".join(parts[1:]) in _EXCLUDED_SUFFIXES)


def filtered_suite(suite):
    kept = []
    excluded = []
    for case in _cases(suite):
        test_id = case.id()
        if _is_excluded(test_id):
            excluded.append(test_id)
        else:
            kept.append(case)
    return unittest.TestSuite(kept), tuple(sorted(excluded))


def run_suite(suite, *, stream=sys.stderr, verbosity=1, required_excluded_count=None):
    filtered, excluded = filtered_suite(suite)
    stream.write(f"excluded unittest IDs ({len(excluded)}):\n")
    for test_id in excluded:
        stream.write(f"  {test_id}\n")
    stream.flush()
    count_ok = required_excluded_count is None or len(excluded) == required_excluded_count
    if not count_ok:
        stream.write(
            f"required exclusion count {required_excluded_count}, observed {len(excluded)}\n"
        )
        stream.flush()
    result = unittest.TextTestRunner(stream=stream, verbosity=verbosity).run(filtered)
    result.rollout_exclusion_count_ok = count_ok
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--modules", nargs="+")
    source.add_argument("--discover")
    parser.add_argument("--require-excluded-count", type=int, default=5)
    options = parser.parse_args(argv)
    loader = unittest.defaultTestLoader
    suite = (loader.loadTestsFromNames(options.modules)
             if options.modules is not None
             else loader.discover(options.discover))
    result = run_suite(
        suite,
        required_excluded_count=options.require_excluded_count,
    )
    return 0 if result.wasSuccessful() and result.rollout_exclusion_count_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
