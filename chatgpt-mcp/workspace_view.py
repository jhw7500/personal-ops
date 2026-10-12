"""Read-only, bounded views of an explicitly allowlisted Git worktree."""

from datetime import datetime, timezone
import os
from pathlib import Path
import stat

from git_workflows import _decode, _git, _reject_dangerous_git_config

MAX_FILES = 500
MAX_PREVIEW = 64 * 1024


def observed_at():
    return datetime.now(timezone.utc).isoformat()


def read_git(root, *args, check=True):
    # Prevent status from refreshing the index while another tool may own it.
    return _git(root, '--no-optional-locks', *args, check=check)


def open_preview(root, path):
    """Anchor each path component so a concurrent link swap cannot escape."""
    parts = Path(path).parts
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


class WorkspaceView:
    def __init__(self, projects):
        self.projects = {name: Path(root).resolve(strict=True) for name, root in projects.items()}

    def root(self, project):
        if not isinstance(project, str) or project not in self.projects:
            raise ValueError('Allowed project required')
        return self.projects[project]

    def repository(self, project):
        root = self.root(project)
        if read_git(root, 'rev-parse', '--is-inside-work-tree', check=False).stdout.strip() != b'true':
            raise ValueError('Not a Git repository')
        _reject_dangerous_git_config(root)
        top = Path(_decode(read_git(root, 'rev-parse', '--show-toplevel').stdout).strip()).resolve()
        if top != root:
            raise ValueError('Project must be the exact repository root')
        return root

    def path(self, root, value):
        if not isinstance(value, str) or not value or '\0' in value or len(value) > 4096:
            raise ValueError('Invalid file path')
        relative = Path(value)
        if relative.is_absolute() or '..' in relative.parts or any(
                p == '.git' or p == '.env' or p.startswith('.env.') for p in relative.parts):
            raise ValueError('File path is excluded')
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValueError('Symbolic links are excluded')
        if not current.resolve(strict=False).is_relative_to(root):
            raise ValueError('File path must remain inside the project')
        if current.is_dir():
            raise ValueError('Select a file')
        return current

    def changes(self, project):
        self.root(project)  # Invalid allowlist selection is never disguised as an empty repo.
        result = {'project': project, 'available': False, 'branch': None, 'head': None,
                  'files': [], 'observed_at': observed_at(), 'truncated': False}
        try:
            root = self.repository(project)
            branch = read_git(root, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False)
            result['branch'] = _decode(branch.stdout).strip() or '(detached)'
            head = read_git(root, 'rev-parse', '--verify', 'HEAD', check=False)
            result['head'] = _decode(head.stdout).strip() if not head.returncode else None
            records = read_git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all').stdout.split(b'\0')
            cursor = 0
            while cursor < len(records):
                entry = records[cursor]
                cursor += 1
                if not entry:
                    continue
                if len(entry) < 4 or entry[2:3] != b' ':
                    raise ValueError('Invalid Git status')
                status_text = entry[:2].decode('ascii')
                previous = None
                if 'R' in status_text or 'C' in status_text:
                    if cursor >= len(records):
                        raise ValueError('Incomplete rename')
                    previous = records[cursor].decode('utf-8')
                    cursor += 1
                path = entry[3:].decode('utf-8')
                try:
                    self.path(root, path)
                    if previous:
                        self.path(root, previous)
                except ValueError:
                    continue
                if len(result['files']) >= MAX_FILES:
                    result['truncated'] = True
                    continue
                result['files'].append({'path': path, 'previous_path': previous, 'status': status_text})
            result['available'] = True
        except (ValueError, OSError):
            result.update(files=[], error='Git 정보를 읽을 수 없습니다. 저장소 루트와 호스트 Git 설정을 확인하세요.')
        return result

    def diff(self, project, path, staged=False):
        if type(staged) is not bool:
            raise ValueError('staged must be boolean')
        root = self.repository(project)
        target = self.path(root, path)
        result = {'project': project, 'path': path, 'staged': staged, 'text': '',
                  'kind': 'diff', 'truncated': False, 'observed_at': observed_at()}
        tracked = read_git(root, 'ls-files', '--error-unmatch', '--', path, check=False)
        if tracked.returncode and not staged and target.exists():
            descriptor = open_preview(root, path)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('Preview requires a regular file')
                data = os.read(descriptor, MAX_PREVIEW)
            finally:
                os.close(descriptor)
            result['truncated'] = info.st_size > len(data)
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                if result['truncated']:
                    text = data.decode('utf-8', errors='replace')
                else:
                    text = ''
                    result['kind'] = 'binary'
            if b'\0' in data or result['kind'] == 'binary':
                result.update(kind='binary', text='바이너리 파일은 텍스트 미리보기를 제공하지 않습니다.')
                return result
            result['kind'] = 'untracked'
            output = (f'--- /dev/null\n+++ b/{path}\n' + ''.join('+' + line + '\n' for line in text.splitlines())).encode()
        else:
            command = ['diff', '--no-ext-diff', '--no-textconv', '--no-color']
            if staged:
                command.append('--cached')
            command.extend(['--', path])
            try:
                output = read_git(root, *command).stdout
            except ValueError as exc:
                if '512 KiB safety limit' not in str(exc):
                    raise
                result.update(kind='too_large', truncated=True,
                              text='변경 내용이 미리보기 한도를 초과했습니다. 호스트에서 파일을 확인하세요.')
                return result
            if b'Binary files ' in output:
                result['kind'] = 'binary'
        result['truncated'] = result['truncated'] or len(output) > MAX_PREVIEW
        result['text'] = output[:MAX_PREVIEW].decode('utf-8', errors='replace')
        return result
