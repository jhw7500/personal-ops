"""Pure registration/verification contracts, no rollout or approval effects."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE))

def load(name,filename):
    spec=importlib.util.spec_from_file_location(name,HERE/filename)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module);return module

class CommitAccessRegistrationTest(unittest.TestCase):
    def test_explicit_tool_and_test_contracts_include_commit_access(self):
        rollout=load('commit_access_rollout_contract','rollout-host-runtime.py')
        required={'open_git_commit_access_approval','get_git_commit_access','confirm_git_commit_access','cancel_git_commit_access','revoke_git_commit_access'}
        self.assertTrue(required.issubset(rollout.TOOL_NAMES))
        self.assertTrue({'tests.test_git_commit_access','tests.test_git_commit_access_router','tests.test_git_commit_access_concurrency'}.issubset(rollout.TESTS))

    def test_browser_is_mandatory_in_official_preflight(self):
        rollout=load('commit_access_rollout_browser','rollout-host-runtime.py')
        calls=[]
        def collect(argv,*,stage,**kwargs):
            calls.append((stage,argv));return subprocess.CompletedProcess(argv,0)
        with mock.patch.object(rollout,'runtime_interpreter',return_value='/usr/bin/python3'), mock.patch.object(rollout,'runtime_node',return_value='/usr/bin/node'), mock.patch.object(rollout,'run_command',side_effect=collect):
            rollout.run_checks()
        stages=[stage for stage,args in calls]
        self.assertIn('checks_git_commit_access_browser',stages)
        args=dict(calls)['checks_git_commit_access_browser']
        self.assertIn(str(HERE/'tests/test_git_commit_access_browser.py'),args)
        self.assertLess(stages.index('checks_git_commit_access_browser'),stages.index('checks_targeted'))

if __name__=='__main__':unittest.main()
