import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workspace_view import WorkspaceView  # noqa: E402


def git(root, *args):
    return subprocess.run(['/usr/bin/git', '-C', str(root), *args], check=True,
                          capture_output=True, env={**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null'})


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-b', 'work')
        git(self.repo, 'config', 'user.name', 'Fixture')
        git(self.repo, 'config', 'user.email', 'fixture@example.com')
        for name in ('edited.txt', 'deleted.txt', 'renamed.txt'):
            (self.repo / name).write_text('before\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'fixture')
        self.view = WorkspaceView({'repo': self.repo})

    def tearDown(self):
        self.temp.cleanup()

    def test_changes_rename_stage_untracked_and_read_only(self):
        (self.repo / 'edited.txt').write_text('after\n')
        (self.repo / 'deleted.txt').unlink()
        git(self.repo, 'mv', 'renamed.txt', 'new name.txt')
        (self.repo / 'untracked.txt').write_text('new\n')
        before = (self.repo / '.git/index').read_bytes()
        result = self.view.changes('repo')
        self.assertTrue(result['available'])
        self.assertEqual(result['branch'], 'work')
        names = {row['path']: row for row in result['files']}
        self.assertEqual(names['edited.txt']['status'], ' M')
        self.assertEqual(names['new name.txt']['previous_path'], 'renamed.txt')
        self.assertEqual(names['untracked.txt']['status'], '??')
        self.assertEqual((self.repo / '.git/index').read_bytes(), before)
        self.assertIn('+after', self.view.diff('repo', 'edited.txt')['text'])
        self.assertIn('-before', self.view.diff('repo', 'deleted.txt')['text'])
        self.assertIn('new name.txt', self.view.diff('repo', 'new name.txt', staged=True)['text'])
        self.assertIn('+new', self.view.diff('repo', 'untracked.txt')['text'])

    def test_paths_secrets_links_and_parent_repo_rejected(self):
        (self.repo / '.env').write_text('secret')
        (self.repo / '.env.local').write_text('secret')
        (self.repo / 'link').symlink_to(self.base)
        result = self.view.changes('repo')
        self.assertFalse(any(row['path'] in ('.env', '.env.local', 'link') for row in result['files']))
        for path in ('../outside', '/etc/passwd', '.env', '.env.local', '.git/config', 'link/anything'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.view.diff('repo', path)
        sub = self.repo / 'sub'
        sub.mkdir()
        self.assertFalse(WorkspaceView({'sub': sub}).changes('sub')['available'])
        with self.assertRaises(ValueError):
            self.view.changes('unknown')

    def test_staged_deletion_has_empty_worktree_and_cached_deletion_diff(self):
        git(self.repo, 'rm', 'deleted.txt')
        self.assertEqual(self.view.diff('repo', 'deleted.txt')['text'], '')
        self.assertIn('-before', self.view.diff('repo', 'deleted.txt', staged=True)['text'])

    def test_preview_bounds_binary_and_non_git(self):
        (self.repo / 'large.txt').write_text('x\n' * 40000)
        result = self.view.diff('repo', 'large.txt')
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['text'].encode()), 70 * 1024)
        (self.repo / 'binary.bin').write_bytes(b'a\0b')
        self.assertEqual(self.view.diff('repo', 'binary.bin')['kind'], 'binary')
        non_git = self.base / 'plain'
        non_git.mkdir()
        self.assertFalse(WorkspaceView({'plain': non_git}).changes('plain')['available'])

    def test_external_git_programs_are_never_executed(self):
        marker = self.base / 'must-not-exist'
        git(self.repo, 'config', 'diff.external', '/usr/bin/touch ' + str(marker))
        (self.repo / 'edited.txt').write_text('after\n')
        self.assertFalse(self.view.changes('repo')['available'])
        with self.assertRaises(ValueError):
            self.view.diff('repo', 'edited.txt')
        self.assertFalse(marker.exists())

    def test_preview_parent_link_swap_cannot_escape_the_repository(self):
        directory = self.repo / 'safe'
        directory.mkdir()
        (directory / 'new.txt').write_text('local')
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'new.txt').write_text('must-not-read')
        original = self.view.path

        def swap_after_validation(root, value):
            target = original(root, value)
            directory.rename(self.repo / 'old-safe')
            directory.symlink_to(outside, target_is_directory=True)
            return target

        with patch.object(self.view, 'path', side_effect=swap_after_validation):
            with self.assertRaises((ValueError, OSError)):
                self.view.diff('repo', 'safe/new.txt')


if __name__ == '__main__':
    unittest.main()
