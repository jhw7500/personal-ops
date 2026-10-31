"""Source-only tests for NEW exact TEST upload console evidence, without OAuth/network."""
import unittest
from toss_console_evidence import ConsoleEvidenceError
from toss_console_new_test_evidence import (
    from_owner_verified_evidence, reviewed_bundle_query, project_new_test_bundle,
)
NEW = "01a17791-4979-736a-a132-6b0973fe7587"
OLD = "01a151bd-4015-75cf-8275-fa6b6aef9a47"

def upload_evidence():
    op={"project":"tossApp","profile_id":"tossapp-upload-20261026","effect":"external_publish",
        "state":"succeeded","phase":"COMPLETE","termination_verified":True,
        "result":{"exit_code":0},"operation_id":"a"*32,"source_digest":"c"*64}
    receipt={"app":"tarororo","deployment_id":NEW,"status":"cli_reported_complete",
             "external_attempted":True,"cli_exit_code":0,"artifact_sha256":"d"*64}
    return op,receipt

def bundle(deployment=NEW,**changes):
    return {"contents":[{"miniAppId":75257,"workspaceId":93125,
        "appName":"tarororo","deploymentId":deployment,"reviewStatus":"CREATED",
        "versionName":"20990101-01","isTested":True,"deployed":False,
        "sensitiveExtra":"never output this",**changes}],
        "currentPage":0,"totalPage":0}

class NewTestConsoleEvidence(unittest.TestCase):
    def target(self):
        return from_owner_verified_evidence(*upload_evidence())

    def test_exact_owner_upload_allows_only_exact_search(self):
        t=self.target()
        self.assertEqual(reviewed_bundle_query(t),{"workspaceId":93125,"miniAppId":75257,
                          "page":0,"size":20,"search":NEW})

    def test_correct_bundle_projected_without_raw_provider_secret(self):
        r=project_new_test_bundle(bundle(),target=self.target())
        self.assertTrue(r["test_bundle_ready"])
        self.assertFalse(r["provider_verified"])
        self.assertFalse(r["remote_digest_verified"])
        self.assertNotIn("never output this",repr(r))

    def test_wrong_prior_bundle_does_not_match(self):
        r=project_new_test_bundle(bundle(OLD),target=self.target())
        self.assertFalse(r["found"])
        self.assertEqual(r["deployment_id"],NEW)

    def test_reused_old_or_unverified_operation_rejected(self):
        op,receipt=upload_evidence()
        receipt["deployment_id"]=OLD
        with self.assertRaises(ConsoleEvidenceError):
            from_owner_verified_evidence(op,receipt)
        op,receipt=upload_evidence()
        for changes in ({"state":"pending_host_approval"},{"profile_id":"tossapp-upload-ux019"},
                        {"effect":"read_only"},{"termination_verified":False},
                        {"result":{"exit_code":1}}):
            with self.subTest(changes=changes),self.assertRaises(ConsoleEvidenceError):
                from_owner_verified_evidence({**op,**changes},receipt)

    def test_forged_receipt_missing_identity_rejected(self):
        op,receipt=upload_evidence()
        for changes in ({"status":"failed"},{"external_attempted":False},
                        {"artifact_sha256":"wrong"},{"deployment_id":"not-a-uuid"},
                        {"app":"wrong"}):
            with self.subTest(changes=changes),self.assertRaises(ConsoleEvidenceError):
                from_owner_verified_evidence(op,{**receipt,**changes})

    def test_wrong_scope_or_duplicate_denied(self):
        t=self.target()
        for raw in (bundle(miniAppId=1),bundle(workspaceId=1),bundle(appName="other"),
                    {**bundle(),"totalPage":1},{**bundle(),"contents":bundle()["contents"]*2}):
            with self.subTest(raw=raw),self.assertRaises(ConsoleEvidenceError):
                project_new_test_bundle(raw,target=t)

    def test_unready_or_wrong_type_fail_closed(self):
        t=self.target()
        r=project_new_test_bundle(bundle(isTested=False),target=t)
        self.assertFalse(r["test_bundle_ready"])
        with self.assertRaises(ConsoleEvidenceError):
            project_new_test_bundle(bundle(isTested="true"),target=t)

if __name__=="__main__": unittest.main()
