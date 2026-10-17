"""Exercise staged systemd installation without invoking systemctl."""

import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    sys.platform.startswith("linux") and shutil.which("make"), "requires Linux and make"
)
class InstallationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="meo-install-")
        self.addCleanup(temporary.cleanup)
        self.staging = pathlib.Path(temporary.name)

    def make(self, target, *variables, env=None):
        return subprocess.run(
            [
                "make",
                "--no-print-directory",
                target,
                f"DESTDIR={self.staging}",
                "SYSTEMCTL=systemctl-must-not-be-invoked",
                *variables,
            ],
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    def environment_for_uid(self, uid):
        commands = self.staging / "commands"
        commands.mkdir(exist_ok=True)
        identity = commands / "id"
        identity.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$MEO_TEST_EUID"\n', encoding="utf-8"
        )
        identity.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(commands) + os.pathsep + env.get("PATH", "")
        env["MEO_TEST_EUID"] = str(uid)
        return env

    def test_staged_install_preserves_configuration_and_uninstalls(self):
        variables = ("install_scope=system", "prefix=/opt/meo", "sysconfdir=/etc/meo")
        result = self.make("install", *variables)
        self.assertEqual(result.returncode, 0, result.stdout)
        command = self.staging / "opt/meo/bin/meo-ipv6-fix"
        units = self.staging / "opt/meo/lib/systemd/system"
        service = units / "meo-ipv6-fix.service"
        timer = units / "meo-ipv6-fix.timer"
        config = self.staging / "etc/meo/default/meo-ipv6-fix"
        self.assertEqual(stat.S_IMODE(command.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(service.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertIn("\nMEO_DEVICES=\n", config.read_text(encoding="utf-8"))
        text = service.read_text(encoding="utf-8")
        self.assertIn('ExecStart="/opt/meo/bin/meo-ipv6-fix" $MEO_DEVICES', text)
        self.assertIn('EnvironmentFile="/etc/meo/default/meo-ipv6-fix"', text)
        self.assertNotIn(str(self.staging), text)
        self.assertTrue(timer.is_file())
        subprocess.run([str(command), "--help"], check=True, stdout=subprocess.DEVNULL)

        config.write_text("MEO_DEVICES=custom\n", encoding="utf-8")
        result = self.make("install", *variables)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(config.read_text(encoding="utf-8"), "MEO_DEVICES=custom\n")

        result = self.make("uninstall", *variables)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(command.exists())
        self.assertFalse(service.exists())
        self.assertFalse(timer.exists())
        self.assertEqual(config.read_text(encoding="utf-8"), "MEO_DEVICES=custom\n")

    def test_defaults_follow_effective_user_and_respect_overrides(self):
        cases = (
            (0, None, None),
            (1000, None, None),
            (1000, "/meo/config", None),
            (1000, None, "/opt/meo-user"),
        )
        for uid, xdg_config, override in cases:
            with self.subTest(uid=uid, xdg_config=xdg_config, prefix=override):
                env = self.environment_for_uid(uid)
                env.pop("XDG_CONFIG_HOME", None)
                if xdg_config:
                    env["XDG_CONFIG_HOME"] = xdg_config
                variables = (f"prefix={override}",) if override else ()
                result = self.make("install", *variables, env=env)
                self.assertEqual(result.returncode, 0, result.stdout)
                if uid == 0:
                    prefix = pathlib.Path("/usr/local")
                    unitdir = prefix / "lib/systemd/system"
                    config = pathlib.Path("/etc/default/meo-ipv6-fix")
                else:
                    prefix = (
                        pathlib.Path(override)
                        if override
                        else pathlib.Path.home() / ".local"
                    )
                    configdir = (
                        pathlib.Path(xdg_config)
                        if xdg_config
                        else pathlib.Path.home() / ".config"
                    )
                    unitdir = configdir / "systemd/user"
                    config = configdir / "meo-ipv6-fix.env"
                command = self.staging / prefix.relative_to("/") / "bin/meo-ipv6-fix"
                service = (
                    self.staging / unitdir.relative_to("/") / "meo-ipv6-fix.service"
                )
                configuration = self.staging / config.relative_to("/")
                self.assertTrue(command.is_file())
                self.assertTrue(configuration.is_file())
                text = service.read_text(encoding="utf-8")
                self.assertIn(
                    f'ExecStart="{prefix}/bin/meo-ipv6-fix" $MEO_DEVICES', text
                )
                self.assertIn(f'EnvironmentFile="{config}"', text)
                self.assertEqual("DynamicUser=yes" in text, uid == 0)
                self.assertEqual("ProtectHome=yes" in text, uid == 0)
                self.assertNotIn(str(self.staging), text)

                result = self.make("uninstall", *variables, env=env)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertFalse(command.exists())
                self.assertFalse(service.exists())
                self.assertTrue(configuration.is_file())

    def test_activation_uses_matching_systemd_manager(self):
        systemctl = self.staging / "systemctl"
        systemctl.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$MEO_TEST_SYSTEMCTL_LOG"\n',
            encoding="utf-8",
        )
        systemctl.chmod(0o755)
        log = self.staging / "systemctl.log"
        for uid in (0, 1000):
            with self.subTest(uid=uid):
                env = self.environment_for_uid(uid)
                env["MEO_TEST_SYSTEMCTL_LOG"] = str(log)
                log.unlink(missing_ok=True)
                for target in ("enable", "disable"):
                    result = self.make(
                        target, "DESTDIR=", f"SYSTEMCTL={systemctl}", env=env
                    )
                    self.assertEqual(result.returncode, 0, result.stdout)
                flag = "--user " if uid else ""
                self.assertEqual(
                    log.read_text(encoding="utf-8").splitlines(),
                    [
                        f"{flag}daemon-reload",
                        f"{flag}enable --now meo-ipv6-fix.timer",
                        f"{flag}disable --now meo-ipv6-fix.timer",
                    ],
                )

    def test_staging_rejects_service_activation(self):
        for target in ("enable", "disable"):
            with self.subTest(target=target):
                result = self.make(target)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires an empty DESTDIR", result.stdout)
                self.assertNotIn("systemctl-must-not-be-invoked", result.stdout)


if __name__ == "__main__":
    unittest.main()
