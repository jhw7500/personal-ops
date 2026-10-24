"""Policy-aware implementations of the six ordinary source file tools."""

from __future__ import annotations

import fnmatch
import multiprocessing
import os
from pathlib import Path
import re
import stat

import pathspec

from workspace_paths import WorkspaceError, is_sensitive, open_directory, read_file, write_file


MAX_FILES = 2000
MAX_RESULTS = 2000
MAX_FILE_BYTES = 1024 * 1024
MAX_ANSWER_CHARS = 65536
REGEX_START_TIMEOUT = 2.0
REGEX_MATCH_TIMEOUT = 0.25
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CODE_SUFFIXES = frozenset({
    ".c", ".cc", ".cpp", ".css", ".go", ".h", ".hpp", ".html", ".java",
    ".js", ".jsx", ".json", ".kt", ".mjs", ".py", ".rb", ".rs", ".sh",
    ".sql", ".swift", ".toml", ".ts", ".tsx", ".xml", ".yaml", ".yml",
})


def _compile_regex(pattern):
    if (not isinstance(pattern, str) or not 0 < len(pattern) <= 256
            or re.search(r"\\[1-9]|\(\?", pattern)
            or re.search(r"\([^)]*[*+?{][^)]*\)\s*(?:[*+?]|\{)", pattern)
            or len(re.findall(r"[*+?]|\{\d", pattern)) > 8):
        raise WorkspaceError("WORKSPACE_PATTERN_INVALID")
    try:
        return re.compile(pattern)
    except re.error:
        raise WorkspaceError("WORKSPACE_PATTERN_INVALID") from None


def _regex_worker(connection, pattern):
    try:
        expression = re.compile(pattern)
        connection.send(("ready", None))
        while True:
            request = connection.recv()
            if request is None:
                return
            operation, text, replacement, multiple = request
            if operation == "search":
                matches = []
                for index, line in enumerate(text.splitlines()):
                    if expression.search(line):
                        matches.append(index)
                        if len(matches) > MAX_RESULTS:
                            break
                connection.send(("ok", matches))
            elif operation == "replace":
                count = sum(1 for _, _ in zip(expression.finditer(text), range(2)))
                updated = expression.sub(
                    replacement, text, count=0 if multiple else 1,
                ) if count else text
                connection.send(("ok", (count, updated)))
            else:
                connection.send(("failed", None))
    except re.error:
        try:
            connection.send(("invalid", None))
        except (BrokenPipeError, EOFError, OSError):
            pass
    except (BrokenPipeError, EOFError, OSError):
        pass
    except BaseException:
        try:
            connection.send(("failed", None))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


class _RegexSession:
    def __init__(self, pattern):
        self.pattern = pattern
        self.connection = None
        self.process = None

    def _receive(self, timeout):
        if not self.connection.poll(timeout):
            self._stop()
            raise WorkspaceError("WORKSPACE_PATTERN_TIMEOUT")
        try:
            status, payload = self.connection.recv()
        except (EOFError, OSError):
            self._stop()
            raise WorkspaceError("WORKSPACE_PATTERN_FAILED") from None
        if status == "invalid":
            raise WorkspaceError("WORKSPACE_PATTERN_INVALID")
        if status != "ok" and status != "ready":
            raise WorkspaceError("WORKSPACE_PATTERN_FAILED")
        return payload

    def __enter__(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self.connection = parent
        self.process = context.Process(
            target=_regex_worker, args=(child, self.pattern), daemon=True,
        )
        self.process.start()
        child.close()
        self._receive(REGEX_START_TIMEOUT)
        return self

    def request(self, operation, text, replacement=None, multiple=False):
        try:
            self.connection.send((operation, text, replacement, multiple))
        except (BrokenPipeError, EOFError, OSError):
            self._stop()
            raise WorkspaceError("WORKSPACE_PATTERN_FAILED") from None
        return self._receive(REGEX_MATCH_TIMEOUT)

    def _stop(self):
        if self.process is not None and self.process.is_alive():
            self.process.terminate()
            self.process.join(0.2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join()

    def __exit__(self, *_):
        if self.connection is not None:
            if self.process is not None and self.process.is_alive():
                try:
                    self.connection.send(None)
                except (BrokenPipeError, EOFError, OSError):
                    pass
                self.process.join(0.2)
            self._stop()
            self.connection.close()
        if self.process is not None:
            self.process.close()


def _ignore_spec(root: Path, directory: str):
    relative = f"{directory}/.gitignore" if directory else ".gitignore"
    try:
        data = read_file(root, relative, max_bytes=65536).data.decode("utf-8")
    except WorkspaceError as exc:
        if exc.code == "FILE_NOT_FOUND":
            return None
        raise
    except UnicodeError:
        raise WorkspaceError("WORKSPACE_IGNORE_INVALID") from None
    try:
        return pathspec.GitIgnoreSpec.from_lines(data.splitlines())
    except (TypeError, ValueError, RecursionError):
        raise WorkspaceError("WORKSPACE_IGNORE_INVALID") from None


def is_ignored(root: Path, relative_path: str, *, is_directory=False) -> bool:
    """Evaluate root and nested gitignore files without invoking Git."""
    if not isinstance(relative_path, str) or relative_path in ("", "."):
        return False
    if type(is_directory) is not bool:
        return True
    parts = relative_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return True
    loaded = []

    def decision(path):
        ignored = False
        for directory, spec in loaded:
            prefix = directory + "/" if directory else ""
            if prefix and not path.startswith(prefix):
                continue
            result = spec.check_file(path[len(prefix):])
            if result.include is not None:
                ignored = bool(result.include)
        return ignored

    try:
        for depth in range(len(parts)):
            directory = "/".join(parts[:depth])
            if depth and decision(directory + "/"):
                return True
            spec = _ignore_spec(Path(root), directory)
            if spec is not None:
                loaded.append((directory, spec))
        query = relative_path + "/" if is_directory else relative_path
        return decision(query)
    except WorkspaceError:
        return True


class WorkspaceFiles:
    def __init__(self, tools):
        self.tools = tools

    @staticmethod
    def _result(payload, *, error=False):
        from workspace_tools import result
        return result(payload, error=error)

    def _registered(self, snapshot, project, relative):
        try:
            self.tools.policy.artifact_rule(snapshot, project, relative)
            return True
        except WorkspaceError as exc:
            if exc.code == "ARTIFACT_NOT_REGISTERED":
                return False
            raise

    def _source(self, snapshot, project, relative, operation):
        target = self.tools.policy.check(project, relative, operation, snapshot=snapshot)
        self.tools.check_permanent(target, snapshot)
        if relative != "." and (is_ignored(
                self.tools.projects[project], relative, is_directory=operation == "list")
                                or self._registered(snapshot, project, relative)):
            raise WorkspaceError("WORKSPACE_SOURCE_EXCLUDED")
        return target

    def _walk(self, snapshot, project, relative, *, recursive=True,
              include_directories=False):
        root = Path(self.tools.projects[project])
        target = self._source(snapshot, project, relative, "list")
        root_fd = open_directory(target)
        rows = []

        def visit(fd, prefix):
            try:
                entries = sorted(os.scandir(fd), key=lambda item: item.name)
            except OSError:
                raise WorkspaceError("WORKSPACE_SOURCE_UNAVAILABLE") from None
            for entry in entries:
                if len(rows) >= MAX_FILES:
                    raise WorkspaceError("WORKSPACE_FILE_LIMIT")
                display = f"{prefix}/{entry.name}" if prefix else entry.name
                full_relative = display if relative == "." else f"{relative}/{display}"
                if is_sensitive(full_relative):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    is_directory = stat.S_ISDIR(info.st_mode)
                    if is_ignored(root, full_relative, is_directory=is_directory):
                        continue
                    try:
                        self.tools.check_permanent(root / full_relative, snapshot)
                    except WorkspaceError as exc:
                        if exc.code == "PROTECTED_PATH":
                            continue
                        raise
                    if self._registered(snapshot, project, full_relative):
                        continue
                except OSError:
                    raise WorkspaceError("WORKSPACE_SOURCE_UNAVAILABLE") from None
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    rows.append({"path": full_relative, "size": info.st_size,
                                 "mode": stat.S_IMODE(info.st_mode), "type": "file"})
                elif stat.S_ISDIR(info.st_mode):
                    if include_directories:
                        rows.append({"path": full_relative, "size": info.st_size,
                                     "mode": stat.S_IMODE(info.st_mode),
                                     "type": "directory"})
                    if not recursive:
                        continue
                    try:
                        child = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=fd)
                    except OSError:
                        raise WorkspaceError("WORKSPACE_SOURCE_UNAVAILABLE") from None
                    try:
                        visit(child, display)
                    finally:
                        os.close(child)

        try:
            visit(root_fd, "")
        finally:
            os.close(root_fd)
        return rows

    @staticmethod
    def _text(file):
        try:
            return file.data.decode("utf-8")
        except UnicodeError:
            raise WorkspaceError("WORKSPACE_BINARY_FILE") from None

    @staticmethod
    def _answer_limit(args):
        value = args.get("max_answer_chars", MAX_ANSWER_CHARS)
        if type(value) is not int or not 1 <= value <= MAX_ANSWER_CHARS:
            raise WorkspaceError("WORKSPACE_ARGUMENTS_INVALID")
        return value

    def _read(self, snapshot, project, args):
        relative = args.get("relative_path")
        target = self._source(snapshot, project, relative, "read")
        file = read_file(target.parent, target.name, max_bytes=MAX_FILE_BYTES)
        text = self._text(file)
        lines = text.splitlines(keepends=True)
        start = args.get("start_line", 0)
        end = args.get("end_line", len(lines) - 1)
        if (type(start) is not int or type(end) is not int or start < 0 or end < start):
            raise WorkspaceError("WORKSPACE_ARGUMENTS_INVALID")
        selected = "".join(lines[start:end + 1])[:self._answer_limit(args)]
        return {"status": "ok", "project": project, "path": relative, "content": selected,
                "sha256": file.sha256, "size": file.size, "mode": file.mode}

    def _list(self, snapshot, project, args):
        relative = args.get("relative_path", ".")
        if type(args.get("recursive", False)) is not bool:
            raise WorkspaceError("WORKSPACE_ARGUMENTS_INVALID")
        rows = self._walk(snapshot, project, relative, recursive=args.get("recursive", False),
                          include_directories=True)
        budget = self._answer_limit(args)
        visible = []
        for row in rows:
            cost = len(row["path"]) + 32
            if cost > budget:
                break
            visible.append(row)
            budget -= cost
        return {"status": "ok", "project": project, "path": relative, "entries": visible,
                "file_count": len(visible), "truncated": len(visible) < len(rows)}

    def _find(self, snapshot, project, args):
        mask = args.get("file_mask")
        if not isinstance(mask, str) or not 0 < len(mask) <= 256:
            raise WorkspaceError("WORKSPACE_ARGUMENTS_INVALID")
        rows = self._walk(snapshot, project, args.get("relative_path", "."))
        matches = [row["path"] for row in rows
                   if fnmatch.fnmatchcase(Path(row["path"]).name, mask)][:MAX_RESULTS]
        return {"status": "ok", "project": project, "files": matches,
                "match_count": len(matches), "truncated": False}

    def _search(self, snapshot, project, args):
        pattern = args.get("substring_pattern")
        expression = _compile_regex(pattern)
        include = args.get("paths_include_glob")
        exclude = args.get("paths_exclude_glob")
        before = args.get("context_lines_before", 0)
        after = args.get("context_lines_after", 0)
        code_only = args.get("restrict_search_to_code_files", False)
        if (include is not None and not isinstance(include, str)
                or exclude is not None and not isinstance(exclude, str)
                or type(before) is not int or type(after) is not int
                or not 0 <= before <= 20 or not 0 <= after <= 20
                or type(code_only) is not bool):
            raise WorkspaceError("WORKSPACE_ARGUMENTS_INVALID")
        results = []
        budget = self._answer_limit(args)
        truncated = False
        root = Path(self.tools.projects[project])
        with _RegexSession(expression.pattern) as regex:
            for row in self._walk(snapshot, project, args.get("relative_path", ".")):
                relative = row["path"]
                if code_only and Path(relative).suffix.lower() not in _CODE_SUFFIXES:
                    continue
                if include and not fnmatch.fnmatchcase(relative, include):
                    continue
                if exclude and fnmatch.fnmatchcase(relative, exclude):
                    continue
                file = read_file(root, relative, max_bytes=MAX_FILE_BYTES)
                text = self._text(file)
                lines = text.splitlines()
                indices = regex.request("search", text)
                if len(indices) > MAX_RESULTS:
                    raise WorkspaceError("WORKSPACE_RESULT_LIMIT")
                for index in indices:
                    start, end = max(0, index - before), min(len(lines), index + after + 1)
                    context = "\n".join(lines[start:end])
                    if budget <= 0:
                        truncated = True
                        break
                    clipped = context[:budget]
                    truncated = truncated or len(clipped) < len(context)
                    results.append({"path": relative, "line": index + 1, "text": clipped})
                    budget -= len(clipped)
                    if len(results) >= MAX_RESULTS:
                        raise WorkspaceError("WORKSPACE_RESULT_LIMIT")
                if budget <= 0:
                    truncated = True
                    break
        return {"status": "ok", "project": project, "matches": results,
                "match_count": len(results), "truncated": truncated}

    def _create(self, snapshot, project, args):
        relative = args.get("relative_path")
        target = self._source(snapshot, project, relative, "write")
        content = args.get("content")
        if not isinstance(content, str):
            raise WorkspaceError("WORKSPACE_ARGUMENTS_INVALID")
        data = content.encode()
        try:
            current = read_file(target.parent, target.name, max_bytes=MAX_FILE_BYTES)
        except WorkspaceError as exc:
            if exc.code != "FILE_NOT_FOUND":
                raise
            receipt = write_file(target.parent, target.name, data, create=True)
        else:
            receipt = write_file(target.parent, target.name, data,
                                 expected_sha256=current.sha256)
        return {"status": "ok", "project": project, "path": relative, **receipt}

    def _replace(self, snapshot, project, args):
        relative = args.get("relative_path")
        target = self._source(snapshot, project, relative, "write")
        needle, replacement, mode = args.get("needle"), args.get("repl"), args.get("mode")
        multiple = args.get("allow_multiple_occurrences", False)
        if (not isinstance(needle, str) or not needle or not isinstance(replacement, str)
                or mode not in ("literal", "regex") or type(multiple) is not bool):
            raise WorkspaceError("WORKSPACE_ARGUMENTS_INVALID")
        current = read_file(target.parent, target.name, max_bytes=MAX_FILE_BYTES)
        text = self._text(current)
        try:
            if mode == "literal":
                count = text.count(needle)
                if count == 0 or (count != 1 and not multiple):
                    raise WorkspaceError("WORKSPACE_OCCURRENCE_MISMATCH")
                updated = text.replace(needle, replacement, -1 if multiple else 1)
            else:
                expression = _compile_regex(needle)
                with _RegexSession(expression.pattern) as regex:
                    count, updated = regex.request(
                        "replace", text, replacement, multiple,
                    )
                if count == 0 or (count != 1 and not multiple):
                    raise WorkspaceError("WORKSPACE_OCCURRENCE_MISMATCH")
        except re.error:
            raise WorkspaceError("WORKSPACE_PATTERN_INVALID") from None
        receipt = write_file(target.parent, target.name, updated.encode(),
                             expected_sha256=current.sha256)
        return {"status": "ok", "project": project, "path": relative, **receipt}

    def call(self, name, args):
        try:
            if name not in {"read_file", "list_dir", "find_file", "search_for_pattern",
                            "create_text_file", "replace_content"} or not isinstance(args, dict):
                raise WorkspaceError("WORKSPACE_ARGUMENTS_INVALID")
            project = args.get("project")
            if project not in self.tools.projects:
                raise WorkspaceError("UNKNOWN_PROJECT")
            snapshot, _ = self.tools._context()
            writes = name in {"create_text_file", "replace_content"}
            if writes:
                with self.tools.authority.project_lock(project):
                    with self.tools.authority.admission(snapshot.digest):
                        self.tools._current(snapshot)
                        payload = getattr(self, "_" + ("create" if name == "create_text_file"
                                                       else "replace"))(snapshot, project, args)
                        self.tools._current(snapshot)
            else:
                with self.tools.authority.admission(snapshot.digest):
                    self.tools._current(snapshot)
                    method = {"read_file": self._read, "list_dir": self._list,
                              "find_file": self._find,
                              "search_for_pattern": self._search}[name]
                    payload = method(snapshot, project, args)
                    self.tools._current(snapshot)
            return self._result(payload)
        except WorkspaceError as exc:
            return self._result({"status": "denied", "code": exc.code}, error=True)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return self._result({"status": "denied", "code": "WORKSPACE_OPERATION_FAILED"},
                                error=True)
