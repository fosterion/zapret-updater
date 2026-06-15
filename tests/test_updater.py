"""
Unit tests for updater.py — focused on the logic where a bug would be expensive
(wiping the wrong folder, losing user lists, restarting the wrong config,
overlapping runs, updating when we shouldn't). The destructive/system calls
themselves (stop_zapret, launch_config, real network, WinAPI enumeration) are
mocked out — we test the orchestration and decisions around them, not the
subprocess/WinAPI plumbing.

Run from the repo root:   python -m unittest discover -s tests -v
                     or:  python -m unittest tests.test_updater -v
"""

import argparse
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import updater  # noqa: E402


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)


# --------------------------------------------------------------------------- #
class TestConfigFromTitle(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(updater._config_from_title("zapret: general"), "general")

    def test_with_parens_and_spaces(self):
        self.assertEqual(
            updater._config_from_title("zapret: general (ALT9)"), "general (ALT9)")

    def test_trailing_whitespace_stripped(self):
        self.assertEqual(updater._config_from_title("zapret: general  "), "general")

    def test_non_matching_titles(self):
        for t in ("", "Notepad", "zapret:", "zapret: ", "something zapret: x", None):
            self.assertIsNone(updater._config_from_title(t), repr(t))


# --------------------------------------------------------------------------- #
class TestReadInstalledVersion(TempDirTest):
    def test_parses_local_version(self):
        write(os.path.join(self.tmp, "service.bat"),
              '@echo off\nset "LOCAL_VERSION=1.9.9c"\n:: rest\n')
        self.assertEqual(updater.read_installed_version(self.tmp), "1.9.9c")

    def test_unquoted_form(self):
        write(os.path.join(self.tmp, "service.bat"), "set LOCAL_VERSION=2.0.0\n")
        self.assertEqual(updater.read_installed_version(self.tmp), "2.0.0")

    def test_missing_service_bat(self):
        self.assertIsNone(updater.read_installed_version(self.tmp))

    def test_no_version_line(self):
        write(os.path.join(self.tmp, "service.bat"), "@echo off\necho hi\n")
        self.assertIsNone(updater.read_installed_version(self.tmp))


# --------------------------------------------------------------------------- #
class TestGetLatestRelease(unittest.TestCase):
    def _patch_http(self, returns):
        original = updater._http_get
        updater._http_get = lambda url, headers, timeout, binary=False: returns
        self.addCleanup(lambda: setattr(updater, "_http_get", original))

    def test_builds_version_and_url(self):
        self._patch_http("1.9.9c\n")
        version, url, name = updater.get_latest_release()
        self.assertEqual(version, "1.9.9c")
        self.assertEqual(name, "zapret-discord-youtube-1.9.9c.zip")
        self.assertEqual(
            url,
            "https://github.com/Flowseal/zapret-discord-youtube/releases/"
            "download/1.9.9c/zapret-discord-youtube-1.9.9c.zip")

    def test_empty_version_raises(self):
        self._patch_http("   \n")
        with self.assertRaises(RuntimeError):
            updater.get_latest_release()


# --------------------------------------------------------------------------- #
class TestLooksLikeZapretDir(TempDirTest):
    def test_nonexistent_is_ok(self):
        self.assertTrue(updater.looks_like_zapret_dir(os.path.join(self.tmp, "nope")))

    def test_empty_is_ok(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        self.assertTrue(updater.looks_like_zapret_dir(empty))

    def test_recognized_by_markers(self):
        for marker in ("service.bat", "general.bat"):
            d = os.path.join(self.tmp, marker.replace(".", "_"))
            write(os.path.join(d, marker), "x")
            self.assertTrue(updater.looks_like_zapret_dir(d))
        with_bin = os.path.join(self.tmp, "withbin")
        os.makedirs(os.path.join(with_bin, "bin"))
        self.assertTrue(updater.looks_like_zapret_dir(with_bin))

    def test_foreign_nonempty_dir_rejected(self):
        foreign = os.path.join(self.tmp, "documents")
        write(os.path.join(foreign, "thesis.docx"), "important")
        write(os.path.join(foreign, "photo.jpg"), "data")
        self.assertFalse(updater.looks_like_zapret_dir(foreign))


# --------------------------------------------------------------------------- #
class TestPreserveRoundTrip(TempDirTest):
    def test_user_lists_survive_wipe(self):
        install = os.path.join(self.tmp, "zapret")
        backup = os.path.join(self.tmp, "backup")
        os.makedirs(backup)

        write(os.path.join(install, "lists", "list-general-user.txt"), "mydomain.com\n")
        write(os.path.join(install, "lists", "ipset-exclude-user.txt"), "1.2.3.4/32\n")
        write(os.path.join(install, "utils", "check_updates.enabled"), "ENABLED\n")
        # These are NOT preserved:
        write(os.path.join(install, "lists", "list-general.txt"), "shipped\n")
        write(os.path.join(install, "service.bat"), 'set "LOCAL_VERSION=1.0.0"\n')

        globs = ["lists/*-user.txt", "utils/*.enabled"]
        saved = updater.backup_preserved(install, globs, backup)

        as_posix = {s.replace(os.sep, "/") for s in saved}
        self.assertEqual(as_posix, {
            "lists/list-general-user.txt",
            "lists/ipset-exclude-user.txt",
            "utils/check_updates.enabled",
        })

        updater.wipe_dir_contents(install)
        self.assertEqual(os.listdir(install), [])

        updater.restore_preserved(install, backup, saved)
        with open(os.path.join(install, "lists", "list-general-user.txt")) as f:
            self.assertEqual(f.read(), "mydomain.com\n")
        with open(os.path.join(install, "utils", "check_updates.enabled")) as f:
            self.assertEqual(f.read(), "ENABLED\n")
        # Non-preserved shipped file must be gone after wipe (not restored).
        self.assertFalse(os.path.exists(os.path.join(install, "lists", "list-general.txt")))


# --------------------------------------------------------------------------- #
class TestExtractInto(TempDirTest):
    def _make_zip(self, path, entries):
        with zipfile.ZipFile(path, "w") as zf:
            for name, content in entries.items():
                zf.writestr(name, content)

    def test_flat_archive(self):
        zip_path = os.path.join(self.tmp, "flat.zip")
        self._make_zip(zip_path, {
            "general.bat": "@echo off\n",
            "bin/winws.exe": "binary",
            "lists/list-general.txt": "a\n",
        })
        install = os.path.join(self.tmp, "install")
        os.makedirs(install)
        updater.extract_into(zip_path, install)
        self.assertTrue(os.path.isfile(os.path.join(install, "general.bat")))
        self.assertTrue(os.path.isfile(os.path.join(install, "bin", "winws.exe")))
        self.assertTrue(os.path.isfile(os.path.join(install, "lists", "list-general.txt")))

    def test_single_top_level_folder_is_unwrapped(self):
        zip_path = os.path.join(self.tmp, "wrapped.zip")
        self._make_zip(zip_path, {
            "zapret-1.0/service.bat": 'set "LOCAL_VERSION=1.0"\n',
            "zapret-1.0/bin/winws.exe": "binary",
        })
        install = os.path.join(self.tmp, "install")
        os.makedirs(install)
        updater.extract_into(zip_path, install)
        # Wrapper folder must be stripped.
        self.assertTrue(os.path.isfile(os.path.join(install, "service.bat")))
        self.assertTrue(os.path.isfile(os.path.join(install, "bin", "winws.exe")))
        self.assertFalse(os.path.exists(os.path.join(install, "zapret-1.0")))


# --------------------------------------------------------------------------- #
class TestLock(TempDirTest):
    def setUp(self):
        super().setUp()
        orig = updater.LOCK_PATH
        updater.LOCK_PATH = os.path.join(self.tmp, "test.lock")
        self.addCleanup(lambda: setattr(updater, "LOCK_PATH", orig))

    def test_second_acquire_is_blocked(self):
        a = updater.acquire_lock()
        self.assertIsNotNone(a)
        try:
            self.assertIsNone(updater.acquire_lock(),
                              "a second concurrent run must not get the lock")
        finally:
            updater.release_lock(a)

    def test_reacquire_after_release(self):
        a = updater.acquire_lock()
        updater.release_lock(a)
        c = updater.acquire_lock()
        self.assertIsNotNone(c, "lock must be free again after release")
        updater.release_lock(c)


# --------------------------------------------------------------------------- #
class TestPerformUpdateOrchestration(TempDirTest):
    def _make_zip(self, path, entries):
        with zipfile.ZipFile(path, "w") as zf:
            for name, content in entries.items():
                zf.writestr(name, content)

    def test_full_flow_preserves_user_file_and_restarts(self):
        install = os.path.join(self.tmp, "zapret")
        write(os.path.join(install, "service.bat"), 'set "LOCAL_VERSION=1.0.0"\n')
        write(os.path.join(install, "lists", "list-general-user.txt"), "mydomain.com\n")
        write(os.path.join(install, "old-junk.txt"), "remove me\n")

        fixture = os.path.join(self.tmp, "new.zip")
        self._make_zip(fixture, {
            "service.bat": 'set "LOCAL_VERSION=2.0.0"\n',
            "bin/winws.exe": "newbin",
            "lists/list-general-user.txt": "domain.example.abc\n",  # shipped default
        })

        calls = []
        cfg = {"install_path": install, "preserve_globs": ["lists/*-user.txt"],
               "restart_after_update": True}
        with mock.patch.object(updater, "download",
                               side_effect=lambda url, dest: shutil.copy2(fixture, dest)), \
             mock.patch.object(updater, "stop_zapret",
                               side_effect=lambda: calls.append("stop")), \
             mock.patch.object(updater, "launch_config",
                               side_effect=lambda p, c: calls.append(("launch", c))):
            updater.perform_update(cfg, "2.0.0", "http://x", "new.zip",
                                   was_running=True, restart_config="general (ALT9)")

        # new version is in place
        self.assertEqual(updater.read_installed_version(install), "2.0.0")
        self.assertTrue(os.path.isfile(os.path.join(install, "bin", "winws.exe")))
        # user file kept its content (restored over the shipped default)
        with open(os.path.join(install, "lists", "list-general-user.txt")) as f:
            self.assertEqual(f.read(), "mydomain.com\n")
        # stale file from the old install is gone
        self.assertFalse(os.path.exists(os.path.join(install, "old-junk.txt")))
        # stopped first, then relaunched the SAME config
        self.assertEqual(calls, ["stop", ("launch", "general (ALT9)")])

    def test_foreign_dir_is_refused_before_any_destruction(self):
        foreign = os.path.join(self.tmp, "documents")
        write(os.path.join(foreign, "thesis.docx"), "important")
        cfg = {"install_path": foreign, "preserve_globs": [],
               "restart_after_update": True}
        with mock.patch.object(updater, "download") as dl, \
             mock.patch.object(updater, "stop_zapret") as stop:
            with self.assertRaises(RuntimeError):
                updater.perform_update(cfg, "2.0.0", "u", "a.zip",
                                       was_running=True, restart_config=None)
            dl.assert_not_called()
            stop.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(foreign, "thesis.docx")),
                        "foreign folder must be left untouched")

    def test_not_running_skips_stop_and_launch(self):
        install = os.path.join(self.tmp, "zapret")
        write(os.path.join(install, "service.bat"), 'set "LOCAL_VERSION=1.0.0"\n')
        fixture = os.path.join(self.tmp, "new.zip")
        self._make_zip(fixture, {"service.bat": 'set "LOCAL_VERSION=2.0.0"\n'})

        calls = []
        cfg = {"install_path": install, "preserve_globs": [],
               "restart_after_update": True}
        with mock.patch.object(updater, "download",
                               side_effect=lambda url, dest: shutil.copy2(fixture, dest)), \
             mock.patch.object(updater, "stop_zapret",
                               side_effect=lambda: calls.append("stop")), \
             mock.patch.object(updater, "launch_config",
                               side_effect=lambda p, c: calls.append("launch")):
            updater.perform_update(cfg, "2.0.0", "u", "a.zip",
                                   was_running=False, restart_config="general")

        self.assertEqual(calls, [], "nothing should be killed or launched if it wasn't running")
        self.assertEqual(updater.read_installed_version(install), "2.0.0")


# --------------------------------------------------------------------------- #
class TestUpdateDecision(unittest.TestCase):
    """The 'do we wipe the folder or not' decision in _check_and_update."""

    def _run(self, installed, latest="1.9.9c", force=False,
             running_cfg=None, state=None, default_config=None, winws=False):
        cfg = {"install_path": "X:/zapret", "preserve_globs": [],
               "default_config": default_config, "restart_after_update": True}
        saved = {}
        perform = mock.Mock()
        with ExitStack() as es:
            p = es.enter_context
            p(mock.patch.object(updater, "load_config", return_value=cfg))
            p(mock.patch.object(updater, "load_state", return_value=dict(state or {})))
            p(mock.patch.object(updater, "save_state",
                                side_effect=lambda s: saved.update(s)))
            p(mock.patch.object(updater, "detect_running_config", return_value=running_cfg))
            p(mock.patch.object(updater, "get_latest_release",
                                return_value=(latest, "http://u", "a.zip")))
            p(mock.patch.object(updater, "read_installed_version", return_value=installed))
            p(mock.patch.object(updater, "winws_running", return_value=winws))
            p(mock.patch.object(updater, "perform_update", perform))
            args = argparse.Namespace(config="ignored", force=force)
            rc = updater._check_and_update(args)
        return rc, perform, saved

    def test_equal_versions_skip(self):
        rc, perform, _ = self._run(installed="1.9.9c", latest="1.9.9c")
        self.assertEqual(rc, 0)
        perform.assert_not_called()

    def test_different_versions_update(self):
        rc, perform, _ = self._run(installed="1.9.9a", latest="1.9.9c")
        self.assertEqual(rc, 0)
        perform.assert_called_once()
        self.assertEqual(perform.call_args[0][1], "1.9.9c")  # version arg

    def test_force_reinstalls_even_if_equal(self):
        _, perform, _ = self._run(installed="1.9.9c", latest="1.9.9c", force=True)
        perform.assert_called_once()

    def test_not_installed_triggers_update(self):
        _, perform, _ = self._run(installed=None, latest="1.9.9c")
        perform.assert_called_once()

    def test_restart_config_taken_from_running_window(self):
        _, perform, saved = self._run(installed="1.9.9a", running_cfg="general (ALT9)")
        self.assertEqual(perform.call_args[0][5], "general (ALT9)")   # restart_config
        self.assertEqual(saved.get("last_config"), "general (ALT9)")

    def test_restart_config_falls_back_to_default(self):
        _, perform, _ = self._run(installed="1.9.9a", running_cfg=None,
                                   state={}, default_config="general")
        self.assertEqual(perform.call_args[0][5], "general")

    def test_was_running_flag_propagated(self):
        _, perform, _ = self._run(installed="1.9.9a", winws=True)
        self.assertTrue(perform.call_args[0][4])  # was_running arg


if __name__ == "__main__":
    unittest.main(verbosity=2)
