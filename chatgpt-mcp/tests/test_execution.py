import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from execution import ExecutionPolicy, MINIMAL_ENV  # noqa: E402


class ExecutionPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chatgpt-mcp-execution-")
        self.root = Path(self.temporary.name)
        self.alpha = self.root / "alpha"
        self.beta = self.root / "beta"
        self.alpha.mkdir()
        self.beta.mkdir()
        self.policy = ExecutionPolicy({"alpha": self.alpha, "beta": self.beta},
                                      config_path=self.root / "missing.json")

    def tearDown(self):
        self.temporary.cleanup()

    def run_spec(self, spec):
        return subprocess.run(
            spec.command,
            cwd=spec.cwd,
            env=spec.env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def write_config(self, content):
        config = self.root / "execution.json"
        config.write_text(json.dumps(content), encoding="utf-8")
        config.chmod(0o600)
        return config

    def make_fake_node_prefix(self, name="node-prefix"):
        prefix = self.root / name
        prefix.joinpath("bin").mkdir(parents=True)
        prefix.joinpath("lib/node_modules/npm/bin").mkdir(parents=True)
        node = prefix / "bin/node"
        node.write_text("#!/bin/sh\n", encoding="utf-8")
        node.chmod(0o755)
        npm_cli = prefix / "lib/node_modules/npm/bin/npm-cli.js"
        npm_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        npm_cli.chmod(0o755)
        npm = prefix / "bin/npm"
        npm.symlink_to(Path("../lib/node_modules/npm/bin/npm-cli.js"))
        return prefix

    def make_fake_chromium_prefix(self, name="chromium-prefix"):
        prefix = self.root / name
        prefix.mkdir()
        prefix.joinpath("chrome").write_bytes(b"\x7fELFtest-fixture")
        prefix.joinpath("chrome").chmod(0o755)
        for name in ("icudtl.dat", "resources.pak"):
            prefix.joinpath(name).write_text("browser-resource", encoding="utf-8")
        return prefix

    def make_fake_postgresql_prefix(self, name="postgresql-prefix"):
        prefix = self.root / name
        prefix.joinpath("bin").mkdir(parents=True)
        prefix.joinpath("share/postgresql").mkdir(parents=True)
        prefix.joinpath("share/postgresql/postgres.bki").write_text("fixture", encoding="utf-8")
        for name in ("postgres", "initdb", "pg_ctl", "createdb"):
            executable = prefix / "bin" / name
            executable.write_bytes(b"\x7fELFtest-fixture")
            executable.chmod(0o755)
        return prefix

    def test_actual_sandbox_isolates_repo_home_env_git_and_network_namespace(self):
        if not self.policy._bwrap:
            self.skipTest("bubblewrap is not installed")
        self.alpha.joinpath("visible.txt").write_text("alpha", encoding="utf-8")
        self.alpha.joinpath(".env").write_text("secret-env", encoding="utf-8")
        self.alpha.joinpath(".git").mkdir()
        self.alpha.joinpath(".git/config").write_text("secret-git", encoding="utf-8")
        self.beta.joinpath("hidden.txt").write_text("beta", encoding="utf-8")
        host_secret = self.root / "host-secret"
        host_secret.write_text("host", encoding="utf-8")
        script = (
            "set -eu; "
            "test \"$(cat visible.txt)\" = alpha; "
            "test ! -s .env; "
            "test ! -e .git/config; "
            "test \"$HOME\" = /home/sandbox; "
            "test -z \"${OPENAI_API_KEY-}\"; "
            f"test ! -e {self.beta / 'hidden.txt'}; "
            f"test ! -e {host_secret}; "
            "printf sandbox-ok"
        )
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "must-not-leak"}):
            result = self.run_spec(self.policy.prepare("alpha", argv=["bash", "-c", script]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "sandbox-ok")

    def test_sandbox_writes_selected_repo_only(self):
        if not self.policy._bwrap:
            self.skipTest("bubblewrap is not installed")
        spec = self.policy.prepare(
            "alpha", argv=["bash", "-c", "printf changed > result.txt"])
        result = self.run_spec(spec)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.alpha.joinpath("result.txt").read_text(), "changed")
        self.assertEqual(list(self.beta.iterdir()), [])

    def test_configured_node_24_and_npm_run_inside_sandbox_read_only(self):
        prefix = Path("/home/jhw/.nvm/versions/node/v24.12.0")
        if not self.policy._bwrap or not prefix.joinpath("bin/npm").exists():
            self.skipTest("the configured host Node 24 toolchain is unavailable")
        config = self.write_config({
            "version": 1,
            "toolchains": {"alpha": {"node": str(prefix)}},
        })
        policy = ExecutionPolicy({"alpha": self.alpha, "beta": self.beta},
                                 config_path=config)
        script = (
            "set -eu; "
            "test \"$(node --version)\" = v24.12.0; "
            "npm --version >/tmp/npm-version; test -s /tmp/npm-version; "
            "test ! -e /home/jhw/.nvm; "
            "if touch /opt/node/chatgpt-mcp-write-probe 2>/dev/null; then exit 91; fi; "
            "printf node-sandbox-ok"
        )
        spec = policy.prepare("alpha", argv=["bash", "-c", script])
        self.assertTrue(spec.env["PATH"].startswith("/opt/node/bin:"))
        result = self.run_spec(spec)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "node-sandbox-ok")
        self.assertFalse(prefix.joinpath("chatgpt-mcp-write-probe").exists())

    def test_node_toolchain_is_project_specific(self):
        prefix = self.make_fake_node_prefix()
        config = self.write_config({
            "version": 1,
            "toolchains": {"alpha": {"node": str(prefix)}},
        })
        policy = ExecutionPolicy({"alpha": self.alpha, "beta": self.beta},
                                 config_path=config)
        alpha = policy.prepare("alpha", argv=["true"])
        beta = policy.prepare("beta", argv=["true"])
        self.assertIn("/opt/node/bin", alpha.env["PATH"])
        self.assertNotIn("/opt/node/bin", beta.env["PATH"])
        self.assertIn("--ro-bind", alpha.command)
        self.assertNotIn(str(prefix), beta.command)

    def test_chromium_is_read_only_project_specific_and_does_not_expose_host_cache(self):
        if not self.policy._bwrap:
            self.skipTest("bubblewrap is not installed")
        prefix = self.make_fake_chromium_prefix()
        sibling = self.root / "host-secret"
        sibling.write_text("must-not-leak", encoding="utf-8")
        config = self.write_config({
            "version": 1,
            "toolchains": {"alpha": {"chromium": str(prefix)}},
        })
        policy = ExecutionPolicy({"alpha": self.alpha, "beta": self.beta}, config_path=config)
        script = (
            "set -eu; "
            "test \"$PLAYWRIGHT_CHROMIUM_PATH\" = /opt/chromium/chrome; "
            "test -x \"$PLAYWRIGHT_CHROMIUM_PATH\"; "
            "test \"$(cat /opt/chromium/resources.pak)\" = browser-resource; "
            f"test ! -e {sibling}; "
            "test ! -e /home/jhw; "
            "if touch /opt/chromium/write-probe 2>/dev/null; then exit 91; fi; "
            "printf browser-sandbox-ok"
        )
        with mock.patch.dict(os.environ, {"PLAYWRIGHT_CHROMIUM_PATH": "/host/must-not-inherit"}):
            result = self.run_spec(policy.prepare("alpha", argv=["bash", "-c", script]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "browser-sandbox-ok")
        self.assertFalse(prefix.joinpath("write-probe").exists())
        beta = policy.prepare("beta", argv=["true"])
        self.assertNotIn("PLAYWRIGHT_CHROMIUM_PATH", beta.env)
        self.assertNotIn(str(prefix), beta.command)

    def test_node_and_chromium_toolchains_can_be_configured_together(self):
        node = self.make_fake_node_prefix()
        chromium = self.make_fake_chromium_prefix()
        config = self.write_config({
            "version": 1,
            "toolchains": {"alpha": {"node": str(node), "chromium": str(chromium)}},
        })
        spec = ExecutionPolicy({"alpha": self.alpha}, config_path=config).prepare(
            "alpha", argv=["true"])
        self.assertTrue(spec.env["PATH"].startswith("/opt/node/bin:"))
        self.assertEqual(spec.env["PLAYWRIGHT_CHROMIUM_PATH"], "/opt/chromium/chrome")
        self.assertIn(str(node), spec.command)
        self.assertIn(str(chromium), spec.command)

    def test_rejects_unsafe_or_unusable_chromium_toolchains(self):
        escaping = self.make_fake_chromium_prefix("escaping-chromium")
        outside = self.root / "outside-chrome"
        outside.write_bytes(b"\x7fELFoutside")
        escaping.joinpath("chrome").unlink()
        escaping.joinpath("chrome").symlink_to(outside)
        missing = self.make_fake_chromium_prefix("missing-resource")
        missing.joinpath("resources.pak").unlink()
        wrapper = self.make_fake_chromium_prefix("wrapper-chromium")
        wrapper.joinpath("chrome").write_text("#!/bin/sh\nexec snap chromium\n")
        nonexecutable = self.make_fake_chromium_prefix("nonexecutable-chromium")
        nonexecutable.joinpath("chrome").chmod(0o644)
        link = self.root / "linked-chromium"
        link.symlink_to(wrapper, target_is_directory=True)
        cases = (
            (None, "absolute path"),
            ("relative/browser", "absolute path"),
            (str(self.root / "missing-browser"), "does not exist"),
            (str(self.alpha), "separate from writable projects"),
            (str(self.root), "separate from writable projects"),
            (str(Path.home()), "host home"),
            (str(link), "non-symlink directory"),
            (str(escaping), "inside its prefix"),
            (str(missing), "missing resources.pak"),
            (str(wrapper), "native ELF binary"),
            (str(nonexecutable), "not executable"),
        )
        for prefix, message in cases:
            with self.subTest(prefix=prefix):
                config = self.write_config({
                    "version": 1, "toolchains": {"alpha": {"chromium": prefix}},
                })
                policy = ExecutionPolicy({"alpha": self.alpha}, config_path=config)
                with self.assertRaisesRegex(ValueError, message):
                    policy.prepare("alpha", argv=["true"])
                with self.assertRaisesRegex(ValueError, message):
                    policy.list_profiles("alpha")

    def test_discovers_e2e_script_without_running_it(self):
        self.alpha.joinpath("package.json").write_text(json.dumps({
            "scripts": {"test:e2e": "playwright test"},
        }))
        profiles = self.policy.list_profiles("alpha")
        self.assertEqual([profile["name"] for profile in profiles], ["npm:test:e2e"])
        self.assertEqual(profiles[0]["argv"], ["npm", "run", "test:e2e"])

    def test_installed_bitbake_denies_indirect_shell_commands_without_execution(self):
        tools = self.root / "system-tools"
        tools.mkdir()
        binary = tools / "bitbake"
        binary.write_text("#!/bin/sh\nexit 99\n")
        binary.chmod(0o755)
        with mock.patch.dict(MINIMAL_ENV, {"PATH": str(tools)}):
            with self.assertRaisesRegex(ValueError, "while BitBake is installed"):
                self.policy.prepare("alpha", argv=["bash", "-c", "printf ordinary"])

    def test_project_cwd_argv_and_timeout_guards(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.alpha.joinpath("escape").symlink_to(outside, target_is_directory=True)
        for kwargs in (
            {"project": "unknown", "argv": ["true"]},
            {"project": "alpha", "argv": []},
            {"project": "alpha", "argv": ["true"], "preset": "x"},
            {"project": "alpha", "argv": ["true"], "relative_cwd": "../beta"},
            {"project": "alpha", "argv": ["true"], "relative_cwd": "escape"},
            {"project": "alpha", "argv": ["true"], "timeout_seconds": 0},
            {"project": "alpha", "argv": ["true"], "timeout_seconds": 3601},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, RuntimeError)):
                self.policy.prepare(**kwargs)

    def test_rejects_overlapping_projects_and_config_inside_writable_repo(self):
        nested = self.alpha / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            ExecutionPolicy({"alpha": self.alpha, "nested": nested})
        with self.assertRaisesRegex(ValueError, "outside all writable projects"):
            ExecutionPolicy({"alpha": self.alpha}, config_path=self.alpha / "execution.json")

    def test_no_fallback_when_bwrap_is_unavailable(self):
        self.policy._bwrap = None
        with self.assertRaisesRegex(RuntimeError, "bubblewrap is required"):
            self.policy.prepare("alpha", argv=["true"])

    def test_rejects_bitbake_commands_and_yocto_repositories(self):
        with self.assertRaisesRegex(ValueError, "BitBake"):
            self.policy.prepare("alpha", argv=["bash", "-c", "bitbake core-image-minimal"])
        self.alpha.joinpath("conf").mkdir()
        self.alpha.joinpath("conf/bblayers.conf").write_text("BBLAYERS = ''", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Yocto/BitBake"):
            self.policy.prepare("alpha", argv=["true"])
        with self.assertRaisesRegex(ValueError, "Yocto/BitBake"):
            self.policy.list_profiles("alpha")

    def test_rejects_unambiguous_yocto_cache_markers(self):
        for index, marker in enumerate((".bitbake", "sstate-cache")):
            with self.subTest(marker=marker):
                repo = self.root / f"repo-{index}"
                repo.mkdir()
                repo.joinpath(marker).mkdir()
                policy = ExecutionPolicy({"repo": repo}, config_path=self.root / "missing.json")
                with self.assertRaisesRegex(ValueError, "Yocto/BitBake"):
                    policy.prepare("repo", argv=["true"])

    def test_allows_generic_tmp_downloads_and_stamps_directories(self):
        for marker in ("tmp", "downloads", "stamps"):
            self.alpha.joinpath(marker).mkdir()
        profiles = self.policy.list_profiles("alpha")
        self.assertEqual(profiles, [])
        spec = self.policy.prepare("alpha", argv=["true"], relative_cwd="tmp")
        self.assertEqual(spec.relative_cwd, "tmp")

    def test_rejects_specific_yocto_tmp_layouts_and_executable(self):
        cases = {
            "tmp-work": ("tmp/work",),
            "tmp-output": ("tmp/stamps", "tmp/deploy"),
            "bitbake-executable": ("bitbake",),
        }
        for name, paths in cases.items():
            with self.subTest(name=name):
                repo = self.root / name
                repo.mkdir()
                for relative in paths:
                    target = repo / relative
                    if target.name == "bitbake":
                        target.write_text("#!/bin/sh\n", encoding="utf-8")
                        target.chmod(0o755)
                    else:
                        target.mkdir(parents=True)
                policy = ExecutionPolicy({"repo": repo}, config_path=self.root / "missing.json")
                with self.assertRaisesRegex(ValueError, "Yocto/BitBake"):
                    policy.prepare("repo", argv=["true"])

    def test_discovers_only_bounded_package_scripts(self):
        self.alpha.joinpath("package.json").write_text(json.dumps({"scripts": {
            "build": "vite build", "test": "vitest", "deploy": "ship",
        }}), encoding="utf-8")
        app = self.alpha / "app"
        app.mkdir()
        app.joinpath("package.json").write_text(json.dumps({"scripts": {
            "lint": "eslint .", "db:migrate": "migrate",
        }}), encoding="utf-8")
        names = {item["name"] for item in self.policy.list_profiles("alpha")}
        self.assertEqual(names, {"npm:build", "npm:test", "app:lint"})


    def test_host_owned_config_profiles_and_permissions(self):
        config = self.write_config({
            "version": 1,
            "profiles": {"alpha": {"quick": {
                "argv": ["bash", "-c", "printf ok"],
                "relative_cwd": ".",
                "timeout_seconds": 12,
            }}},
        })
        policy = ExecutionPolicy({"alpha": self.alpha}, config_path=config)
        profile = policy.list_profiles("alpha")[0]
        self.assertEqual(profile["name"], "quick")
        spec = policy.prepare("alpha", preset="quick")
        self.assertEqual(spec.argv, ["bash", "-c", "printf ok"])
        self.assertEqual(spec.timeout_seconds, 12)

        config.chmod(0o666)
        with self.assertRaisesRegex(ValueError, "permissions"):
            policy.list_profiles("alpha")

        config.unlink()
        config.symlink_to(self.root / "does-not-exist")
        with self.assertRaisesRegex(ValueError, "non-symlink"):
            policy.list_profiles("alpha")

    def test_postgresql_profile_is_opt_in_and_keeps_visible_argv(self):
        prefix = self.make_fake_postgresql_prefix()
        config = self.write_config({
            "version": 1,
            "toolchains": {"alpha": {"postgresql": str(prefix)}},
            "profiles": {"alpha": {
                "db-test": {
                    "argv": ["npm", "run", "test:integration"],
                    "test_database": "tarororo_test",
                },
                "unit": {"argv": ["npm", "run", "test:unit"]},
            }},
        })
        policy = ExecutionPolicy({"alpha": self.alpha, "beta": self.beta}, config_path=config)

        db_spec = policy.prepare("alpha", preset="db-test")
        self.assertEqual(db_spec.argv, ["npm", "run", "test:integration"])
        self.assertEqual(db_spec.execution_argv, [
            "/usr/bin/python3", "/opt/mcp/postgres_job.py", "tarororo_test", "--",
            "npm", "run", "test:integration",
        ])
        self.assertIn(str(prefix), db_spec.command)
        self.assertIn("/opt/postgresql", db_spec.command)
        self.assertIn("/opt/mcp/postgres_job.py", db_spec.command)
        self.assertIn("--tmpfs", db_spec.command)
        self.assertIn("/etc", db_spec.command)

        unit_spec = policy.prepare("alpha", preset="unit")
        direct_spec = policy.prepare("alpha", argv=["true"])
        for spec in (unit_spec, direct_spec):
            self.assertEqual(spec.execution_argv, spec.argv)
            self.assertNotIn("/opt/postgresql", spec.command)
            self.assertNotIn("/opt/mcp/postgres_job.py", spec.command)
            self.assertNotIn("MCP_EPHEMERAL_POSTGRES", spec.env)

    def test_postgresql_profile_requires_safe_project_specific_toolchain(self):
        prefix = self.make_fake_postgresql_prefix()
        cases = (
            ("missing prefix", {}, "requires a PostgreSQL toolchain"),
            ("wrong project", {"beta": {"postgresql": str(prefix)}},
             "requires a PostgreSQL toolchain"),
            ("unsupported database", {"alpha": {"postgresql": str(prefix)}},
             "test_database"),
        )
        for name, toolchains, message in cases:
            with self.subTest(name=name):
                database = "other_database" if name == "unsupported database" else "tarororo_test"
                config = self.write_config({
                    "version": 1,
                    "toolchains": toolchains,
                    "profiles": {"alpha": {"db": {
                        "argv": ["true"], "test_database": database,
                    }}},
                })
                policy = ExecutionPolicy(
                    {"alpha": self.alpha, "beta": self.beta}, config_path=config)
                with self.assertRaisesRegex(ValueError, message):
                    policy.prepare("alpha", preset="db")

    def test_rejects_unsafe_postgresql_prefix(self):
        prefix = self.make_fake_postgresql_prefix()
        outside = self.root / "outside-postgres"
        outside.write_bytes(b"\x7fELFoutside")
        outside.chmod(0o755)
        prefix.joinpath("bin/postgres").unlink()
        prefix.joinpath("bin/postgres").symlink_to(outside)
        config = self.write_config({
            "version": 1,
            "toolchains": {"alpha": {"postgresql": str(prefix)}},
        })
        policy = ExecutionPolicy({"alpha": self.alpha}, config_path=config)
        with self.assertRaisesRegex(ValueError, "escapes its prefix"):
            policy.prepare("alpha", argv=["true"])

    def test_rejects_malformed_or_unsafe_node_toolchains(self):
        outside = self.root / "outside-node"
        outside.write_text("#!/bin/sh\n", encoding="utf-8")
        outside.chmod(0o755)
        cases = []

        missing = self.root / "missing-node-prefix"
        missing.joinpath("bin").mkdir(parents=True)
        missing.joinpath("lib/node_modules/npm").mkdir(parents=True)
        cases.append(("missing node", {"node": str(missing)}, "missing node"))

        escaping = self.make_fake_node_prefix("escaping-prefix")
        escaping.joinpath("bin/node").unlink()
        escaping.joinpath("bin/node").symlink_to(outside)
        cases.append(("escaping node", {"node": str(escaping)}, "escapes its prefix"))

        cases.extend((
            ("relative", {"node": "relative/node"}, "absolute path"),
            ("inside repo", {"node": str(self.alpha)}, "separate from writable projects"),
            ("extra setting", {"node": str(self.make_fake_node_prefix("extra")),
                               "env": {}}, "only node, chromium and postgresql prefixes"),
        ))

        for name, toolchain, message in cases:
            with self.subTest(name=name):
                config = self.write_config({
                    "version": 1,
                    "toolchains": {"alpha": toolchain},
                })
                policy = ExecutionPolicy({"alpha": self.alpha}, config_path=config)
                with self.assertRaisesRegex(ValueError, message):
                    policy.prepare("alpha", argv=["true"])

        config = self.write_config({"version": 1, "toolchains": []})
        policy = ExecutionPolicy({"alpha": self.alpha}, config_path=config)
        with self.assertRaisesRegex(ValueError, "toolchains must be an object"):
            policy.list_profiles("alpha")

    def test_secret_symlink_fails_closed(self):
        secret = self.root / "secret"
        secret.write_text("secret", encoding="utf-8")
        self.alpha.joinpath(".env").symlink_to(secret)
        with self.assertRaisesRegex(ValueError, "symbolic secret path"):
            self.policy.prepare("alpha", argv=["true"])


if __name__ == "__main__":
    unittest.main()
