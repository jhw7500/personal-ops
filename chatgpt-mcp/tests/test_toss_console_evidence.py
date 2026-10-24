"""Data-only, fail-closed tests for the future Toss console evidence adapter."""
from __future__ import annotations

import unittest

from toss_console_evidence import (
    ConsoleEvidenceError, DEPLOYMENT_ID, MAX_BUNDLES, project_bundle_list,
)


def result(**changes):
    entry = {
        "deploymentId": DEPLOYMENT_ID,
        "reviewStatus": "CREATED",
        "versionName": "20261016-unknown",
        "isTested": True,
        "deployed": False,
        "appName": "tarororo",
        "authorizationToken": "do-not-echo",
        "rawAdminResponse": "private",
    }
    entry.update(changes)
    return {"bundles": [entry]}


def project(response=None, **kw):
    return project_bundle_list(
        result() if response is None else response,
        tool_name=kw.pop("tool_name", "bundle_list"),
        workspace_id=kw.pop("workspace_id", "93125"),
        miniapp_id=kw.pop("miniapp_id", "75257"),
        deployment_id=kw.pop("deployment_id", DEPLOYMENT_ID),
        **kw,
    )


class EvidenceProjectionTest(unittest.TestCase):
    def test_observed_contents_page_zero_is_a_complete_nonempty_page(self):
        # Authenticated Console response observed on 2026-10-19. totalPage is
        # the last zero-based page index, not the number of pages or bundles.
        page = {
            "contents": result(miniAppId=75257, versionName="20261016-26")["bundles"],
            "currentPage": 0,
            "totalPage": 0,
        }
        out = project(page)
        self.assertTrue(out["found"])
        self.assertTrue(out["test_bundle_ready"])
        self.assertEqual(out["version_name"], "20261016-26")
        self.assertFalse(out["remote_digest_verified"])
        self.assertNotIn("authorizationToken", str(out))
        self.assertNotIn("provider_verified", out)

    def test_empty_complete_contents_page_does_not_claim_ready(self):
        out = project({"contents": [], "currentPage": 0, "totalPage": 0})
        self.assertFalse(out["found"])
        self.assertFalse(out["test_bundle_ready"])
        self.assertEqual(out["evidence"], "BUNDLE_NOT_CONFIRMED_IN_PAGE")

    def test_contents_requires_the_canonical_miniapp_scope_field(self):
        page = {"contents": result(miniappId=75257)["bundles"],
                "currentPage": 0, "totalPage": 0}
        with self.assertRaisesRegex(ConsoleEvidenceError, "WRONG_CONSOLE_TARGET"):
            project(page)

    def test_incomplete_console_pages_never_claim_ready_or_final_absence(self):
        for contents in ([], result(miniAppId=75257)["bundles"]):
            for current, last in ((0, 1), (1, 1), (1, 2)):
                with self.subTest(current=current, last=last, empty=not contents):
                    with self.assertRaisesRegex(ConsoleEvidenceError, "INCOMPLETE_CONSOLE_PAGE"):
                        project({"contents": contents, "currentPage": current, "totalPage": last})

    def test_missing_or_malformed_page_metadata_is_refused(self):
        page = {"contents": result(miniAppId=75257)["bundles"],
                "currentPage": 0, "totalPage": 0}
        for key in ("currentPage", "totalPage"):
            incomplete = dict(page)
            del incomplete[key]
            with self.subTest(missing=key), self.assertRaisesRegex(
                    ConsoleEvidenceError, "UNVERIFIED_CONSOLE_RESPONSE"):
                project(incomplete)
            for bad in (True, False, -1, 0.0, "0", None):
                with self.subTest(key=key, value=bad), self.assertRaisesRegex(
                        ConsoleEvidenceError, "UNVERIFIED_CONSOLE_RESPONSE"):
                    project({**page, key: bad})
        with self.assertRaisesRegex(ConsoleEvidenceError, "UNVERIFIED_CONSOLE_RESPONSE"):
            project({**page, "currentPage": 2, "totalPage": 1})

    def test_page_metadata_requires_the_contents_envelope(self):
        for key in ("currentPage", "totalPage"):
            with self.subTest(key=key), self.assertRaisesRegex(
                    ConsoleEvidenceError, "UNVERIFIED_CONSOLE_RESPONSE"):
                project({**result(), key: 0})

    def test_mixed_envelope_keys_are_ambiguous(self):
        page = {"contents": result(miniAppId=75257)["bundles"],
                "currentPage": 0, "totalPage": 0}
        for other_key in ("bundles", "items"):
            with self.subTest(key=other_key), self.assertRaisesRegex(
                    ConsoleEvidenceError, "AMBIGUOUS_CONSOLE_RESPONSE"):
                project({**page, other_key: []})

    def test_cursor_metadata_cannot_override_a_complete_page_claim(self):
        page = {"contents": result(miniAppId=75257)["bundles"],
                "currentPage": 0, "totalPage": 0}
        for key in ("nextCursor", "next_cursor", "hasMore", "has_more"):
            with self.subTest(key=key), self.assertRaisesRegex(
                    ConsoleEvidenceError, "INCOMPLETE_CONSOLE_PAGE"):
                project({**page, key: False})

    def test_exact_entry_has_no_remote_hash_claim(self):
        out = project()
        self.assertTrue(out["found"])
        self.assertTrue(out["test_bundle_ready"])
        self.assertEqual(out["review_status"], "CREATED")
        self.assertFalse(out["remote_digest_verified"])
        self.assertEqual(out["evidence"], "MATCHED_CONSOLE_ENTRY")
        self.assertNotIn("authorizationToken", str(out))
        self.assertNotIn("rawAdminResponse", str(out))
        self.assertNotIn("provider_verified", out)

    def test_published_bundle_is_not_test_ready(self):
        out = project(result(deployed=True))
        self.assertTrue(out["found"])
        self.assertFalse(out["test_bundle_ready"])

    def test_build_failed_is_not_test_ready(self):
        out = project(result(reviewStatus="BUILD_FAILED"))
        self.assertEqual(out["evidence"], "BUNDLE_NOT_READY")
        self.assertFalse(out["test_bundle_ready"])

    def test_test_flag_must_be_explicit_boolean(self):
        for flag in (None, 1, "true", "false", 0):
            with self.subTest(flag=flag), self.assertRaises(ConsoleEvidenceError):
                project(result(isTested=flag))

    def test_missing_target_returns_not_found_not_success(self):
        item = result()["bundles"][0]
        item["deploymentId"] = "another-deployment"
        out = project([item])
        self.assertFalse(out["found"])
        self.assertFalse(out["test_bundle_ready"])
        self.assertFalse(out["remote_digest_verified"])
        self.assertEqual(out["evidence"], "BUNDLE_NOT_CONFIRMED_IN_PAGE")

    def test_provider_error_cannot_be_downgraded_to_missing_bundle(self):
        with self.assertRaisesRegex(ConsoleEvidenceError, "PROVIDER_RESPONSE_ERROR"):
            project({"bundles": result()["bundles"], "error": "not authorized"})

    def test_duplicate_deployment_id_is_refused(self):
        item = result()["bundles"][0]
        with self.assertRaisesRegex(ConsoleEvidenceError, "DUPLICATE_DEPLOYMENT_ID"):
            project([item, dict(item)])

    def test_wrong_project_target_and_tool_are_refused(self):
        for kw in (
            {"tool_name": "bundle_upload"},
            {"tool_name": "miniapp_update"},
            {"workspace_id": "93126"},
            {"miniapp_id": 75258},
            {"deployment_id": "not-this-deployment"},
            {"workspace_id": True},
        ):
            with self.subTest(kw=kw), self.assertRaises(ConsoleEvidenceError):
                project(**kw)

    def test_mismatched_explicit_provider_workspace_or_miniapp_is_refused(self):
        cases = (
            {"workspaceId": "93126"},
            {"workspace_id": True},
            {"miniappId": "75258"},
            {"miniAppId": "75258"},
            {"miniAppId": True},
            {"miniAppId": None},
            {"miniapp_id": "different-app"},
            {"workspaceId": "93125", "workspace_id": "93126"},
            {"miniAppId": 75257, "miniappId": "75258"},
            {"miniappId": "75257", "miniAppId": 75258},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(
                    ConsoleEvidenceError, "WRONG_CONSOLE_TARGET"):
                project(result(**kwargs))

    def test_matching_explicit_provider_workspace_and_miniapp_allowed(self):
        out = project(result(workspaceId=93125, miniAppId=75257, miniapp_id="75257"))
        self.assertTrue(out["test_bundle_ready"])
        self.assertTrue(out["found"])

    def test_wrong_app_is_refused(self):
        with self.assertRaisesRegex(ConsoleEvidenceError, "WRONG_CONSOLE_TARGET"):
            project(result(appName="other-app"))

    def test_incomplete_or_unknown_response_is_refused(self):
        bad = (
            {}, {"unexpected": []}, {"bundles": "not-a-list"},
            {"bundles": [], "items": []}, {"bundles": [None]},
            {"bundles": [result(reviewStatus=None)["bundles"][0]]},
            {"bundles": [result(versionName=None)["bundles"][0]]},
            [1], {"bundles": [result(isTested=None)["bundles"][0]]},
            [{}] * (MAX_BUNDLES + 1),
        )
        for entry in bad:
            with self.subTest(entry=str(entry)[:64]), self.assertRaises(ConsoleEvidenceError):
                project(entry)

    def test_items_array_and_int_scope_are_accepted(self):
        out = project({"items": result()["bundles"]}, workspace_id=93125, miniapp_id=75257)
        self.assertTrue(out["test_bundle_ready"])

    def test_provider_data_never_carries_unknown_columns_to_public_projection(self):
        out = project(result(sourceEmail="private@test.example", extraData={"token": "secret"}))
        self.assertNotIn("private@test.example", str(out))
        self.assertNotIn("secret", str(out))


if __name__ == "__main__":
    unittest.main()
