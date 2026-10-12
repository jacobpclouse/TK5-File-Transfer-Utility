import tempfile
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
test_directory = Path(__file__).resolve().parent
project_root = test_directory.parent if test_directory.name == "tests" else test_directory
sys.path.insert(0, str(project_root))

from tk5_file_transfer import (
    FtpFiles,
    file_sha256,
    filter_sort_remote_entries,
    load_quick_sync_mappings,
    push_quick_sync_mappings,
    save_quick_sync_mappings,
)


class LocalFtpStandIn:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.current = "/"

    def pwd(self) -> str:
        return self.current

    def cwd(self, path: str) -> None:
        resolved = (self.root / path.lstrip("/")).resolve()
        if not resolved.is_dir() or self.root.resolve() not in resolved.parents and resolved != self.root.resolve():
            raise OSError(f"Not a directory: {path}")
        self.current = "/" + resolved.relative_to(self.root.resolve()).as_posix()
        if self.current == "/.":
            self.current = "/"

    def mkd(self, name: str) -> None:
        (self.root / self.current.lstrip("/") / name).mkdir()

    def storlines(self, _command: str, source) -> None:
        target = self.root / self.current.lstrip("/") / _command.removeprefix("STOR ")
        target.write_bytes(source.read().replace(b"\r\n", b"\n"))

    def storbinary(self, _command: str, source) -> None:
        target = self.root / self.current.lstrip("/") / _command.removeprefix("STOR ")
        target.write_bytes(source.read())

    def voidresp(self) -> str:
        return "226 Transfer complete"

    def retrlines(self, _command: str, callback) -> None:
        if _command == "LIST":
            for child in (self.root / self.current.lstrip("/")).iterdir():
                permissions = "dr-xr-xr-x" if child.is_dir() else "-r-xr-xr-x"
                callback(f"{permissions} 1 USER GROUP {child.stat().st_size} Jan 01 2026 {child.name}")
            return
        target = self.root / self.current.lstrip("/") / _command.removeprefix("RETR ")
        for line in target.read_bytes().decode("latin-1").splitlines():
            callback(line)

    def retrbinary(self, _command: str, callback) -> None:
        target = self.root / self.current.lstrip("/") / _command.removeprefix("RETR ")
        callback(target.read_bytes())


class FtpTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "remote"
        self.root.mkdir()
        self.client = FtpFiles(LocalFtpStandIn(self.root))
        self.host = Path(self.temp.name) / "host"
        self.host.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_declining_upload_overwrite_preserves_remote_file(self) -> None:
        source = self.host / "HELLO.cbl"
        source.write_text("new source\n", encoding="utf-8")
        remote = self.root / source.name
        remote.write_text("old source\n", encoding="utf-8")
        prompts = []

        self.client.upload([source], "/", "text", lambda *args: prompts.append(args) or False, lambda _text: None)

        self.assertEqual(remote.read_text(encoding="utf-8"), "old source\n")
        self.assertEqual(len(prompts), 1)

    def test_approved_upload_replaces_remote_file(self) -> None:
        source = self.host / "HELLO.cbl"
        source.write_text("new source\n", encoding="utf-8")
        remote = self.root / source.name
        remote.write_text("old source\n", encoding="utf-8")

        self.client.upload([source], "/", "text", lambda *_args: True, lambda _text: None)

        self.assertEqual(remote.read_text(encoding="utf-8"), "new source\n")

    def test_upload_can_override_host_extension_for_pds_member(self) -> None:
        source = self.host / "HELLO.cbl"
        source.write_text("DISPLAY 'HELLO'.\n", encoding="utf-8")

        self.client.upload([source], "/", "text", lambda *_args: True, lambda _text: None, "HELLO")

        self.assertTrue((self.root / "HELLO").is_file())
        self.assertFalse((self.root / "HELLO.cbl").exists())

    def test_upload_to_pds_uses_host_file_stem_by_default(self) -> None:
        pds = self.root / "HERC01.TEST.CNTL"
        pds.mkdir()
        source = self.host / "HELLO.cbl"
        source.write_text("DISPLAY 'HELLO'.\n", encoding="utf-8")

        self.client.upload([source], "HERC01.TEST.CNTL", "text", lambda *_args: True, lambda _text: None)

        self.assertTrue((pds / "HELLO").is_file())
        self.assertFalse((pds / "HELLO.cbl").exists())

    def test_download_from_relative_pds_path(self) -> None:
        pds = self.root / "HERC01.TEST.CNTL"
        pds.mkdir()
        (pds / "TEST").write_text("member contents\n", encoding="utf-8")
        destination = self.host / "downloads"

        self.client.download(["TEST"], "HERC01.TEST.CNTL", destination, "text", lambda *_args: True, lambda _text: None)

        self.assertEqual((destination / "TEST").read_text(encoding="latin-1"), "member contents\n")

    def test_quick_sync_mapping_round_trip_persists_no_credentials(self) -> None:
        config = Path(self.temp.name) / "config" / "quick_sync.json"
        mappings = [{
            "id": "mapping-1",
            "local_path": str(self.host / "HELLO.cbl"),
            "remote_directory": "/HERC01.SOURCE",
            "member_name": "HELLO",
            "last_uploaded_hash": "abc123",
            "last_uploaded_at": "2026-10-12T12:00:00+00:00",
        }]

        save_quick_sync_mappings(config, mappings)

        self.assertEqual(load_quick_sync_mappings(config), mappings)
        saved = config.read_text(encoding="utf-8")
        self.assertNotIn("password", saved.casefold())
        self.assertNotIn("CUL8TR", saved)

    def test_quick_sync_pushes_changed_file_then_skips_unchanged(self) -> None:
        pds = self.root / "HERC01.SOURCE"
        pds.mkdir()
        source = self.host / "HELLO.cbl"
        source.write_text("DISPLAY 'HELLO'.\n", encoding="utf-8")
        mapping = {
            "id": "mapping-1",
            "local_path": str(source),
            "remote_directory": "/HERC01.SOURCE",
            "member_name": "HELLO",
            "last_uploaded_hash": "",
            "last_uploaded_at": "",
        }
        reports = []

        updates = push_quick_sync_mappings(self.client, [mapping], "text", lambda *_args: True, reports.append)

        self.assertEqual((pds / "HELLO").read_text(encoding="latin-1"), "DISPLAY 'HELLO'.\n")
        self.assertEqual(updates[0]["last_uploaded_hash"], file_sha256(source))
        unchanged = push_quick_sync_mappings(
            self.client,
            [{**mapping, **updates[0]}],
            "text",
            lambda *_args: self.fail("unchanged files must not ask for overwrite"),
            reports.append,
        )
        self.assertEqual(unchanged, [])
        self.assertIn("Unchanged: HELLO.cbl", reports)

    def test_quick_sync_declined_overwrite_does_not_update_hash(self) -> None:
        pds = self.root / "HERC01.SOURCE"
        pds.mkdir()
        source = self.host / "HELLO.cbl"
        source.write_text("changed\n", encoding="utf-8")
        (pds / "HELLO").write_text("original\n", encoding="utf-8")
        mapping = {
            "id": "mapping-1",
            "local_path": str(source),
            "remote_directory": "/HERC01.SOURCE",
            "member_name": "HELLO",
            "last_uploaded_hash": "old-hash",
            "last_uploaded_at": "",
        }

        updates = push_quick_sync_mappings(self.client, [mapping], "text", lambda *_args: False, lambda _text: None)

        self.assertEqual(updates, [])
        self.assertEqual((pds / "HELLO").read_text(encoding="utf-8"), "original\n")

    def test_quick_sync_force_push_confirms_remote_only_change(self) -> None:
        pds = self.root / "HERC01.SOURCE"
        pds.mkdir()
        source = self.host / "HELLO.cbl"
        source.write_text("host source\n", encoding="utf-8")
        remote = pds / "HELLO"
        remote.write_text("mainframe edit\n", encoding="utf-8")
        mapping = {
            "id": "one",
            "local_path": str(source),
            "remote_directory": "/HERC01.SOURCE",
            "member_name": "HELLO",
            "last_uploaded_hash": file_sha256(source),
            "last_uploaded_at": "2026-10-12T12:00:00+00:00",
        }
        prompts = []

        updates = push_quick_sync_mappings(
            self.client,
            [mapping],
            "text",
            lambda *args: prompts.append(args) or True,
            lambda _text: None,
            force=True,
        )

        self.assertEqual(remote.read_text(encoding="utf-8"), "host source\n")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(updates[0]["last_uploaded_hash"], file_sha256(source))

    def test_quick_sync_mapping_config_round_trips_without_credentials(self) -> None:
        config = Path(self.temp.name) / "settings" / "quick_sync.json"
        mappings = [{
            "id": "one",
            "local_path": str(self.host / "HELLO.cbl"),
            "remote_directory": "/HERC01.SOURCE",
            "member_name": "HELLO",
            "last_uploaded_hash": "digest",
            "last_uploaded_at": "2026-10-12T12:00:00+00:00",
        }]

        save_quick_sync_mappings(config, mappings)

        self.assertEqual(load_quick_sync_mappings(config), mappings)
        saved_text = config.read_text(encoding="utf-8")
        self.assertNotIn("password", saved_text.casefold())
        self.assertNotIn("CUL8TR", saved_text)

    def test_quick_sync_pushes_only_changed_host_content(self) -> None:
        pds = self.root / "HERC01.SOURCE"
        pds.mkdir()
        source = self.host / "HELLO.cbl"
        source.write_text("DISPLAY 'HELLO'.\n", encoding="utf-8")
        mapping = {
            "id": "one",
            "local_path": str(source),
            "remote_directory": "/HERC01.SOURCE",
            "member_name": "HELLO",
            "last_uploaded_hash": "",
            "last_uploaded_at": "",
        }
        reports = []

        updates = push_quick_sync_mappings(self.client, [mapping], "text", lambda *_args: True, reports.append)

        self.assertEqual((pds / "HELLO").read_text(encoding="latin-1"), "DISPLAY 'HELLO'.\n")
        self.assertEqual(updates[0]["last_uploaded_hash"], file_sha256(source))
        second = push_quick_sync_mappings(
            self.client,
            [{**mapping, **updates[0]}],
            "text",
            lambda *_args: self.fail("unchanged content should not prompt for overwrite"),
            reports.append,
        )
        self.assertEqual(second, [])
        self.assertIn("Unchanged: HELLO.cbl", reports)

    def test_download_can_restore_host_extension(self) -> None:
        (self.root / "HELLO").write_text("DISPLAY 'HELLO'.\n", encoding="utf-8")
        destination = self.host / "downloads"

        self.client.download(["HELLO"], "/", destination, "text", lambda *_args: True, lambda _text: None, "HELLO.cbl")

        self.assertEqual((destination / "HELLO.cbl").read_text(encoding="utf-8"), "DISPLAY 'HELLO'.\n")

    def test_recursive_upload_and_download(self) -> None:
        source_dir = self.host / "programs"
        source_dir.mkdir()
        (source_dir / "HELLO.cbl").write_text("DISPLAY 'HELLO'.\n", encoding="utf-8")
        (self.root / "programs").mkdir()
        reports = []

        self.client.upload([source_dir], "/", "text", lambda *_args: True, reports.append)
        destination = Path(self.temp.name) / "download"
        self.client.download(["programs"], "/", destination, "text", lambda *_args: True, reports.append)

        self.assertEqual((destination / "programs" / "HELLO").read_text(encoding="utf-8"), "DISPLAY 'HELLO'.\n")
        self.assertIn("Uploaded HELLO.cbl", reports)
        self.assertIn("Downloaded HELLO", reports)

    def test_upload_folder_requires_preallocated_remote_pds(self) -> None:
        source_dir = self.host / "new-pds"
        source_dir.mkdir()

        with self.assertRaisesRegex(NotImplementedError, "Allocate the PDS new-pds"):
            self.client.upload([source_dir], "/", "text", lambda *_args: True, lambda _text: None)

    def test_allocate_pds_submits_constrained_jcl_to_internal_reader(self) -> None:
        job_name = self.client.allocate_pds("HERC01.SOURCE", "HERC01")
        submitted = (self.root / "AAINTRDR").read_bytes().decode("ascii")

        self.assertEqual(len(job_name), 8)
        self.assertIn("PGM=IEFBR14", submitted)
        self.assertIn("DSN=HERC01.SOURCE", submitted)
        self.assertIn("VOL=SER=TSO001", submitted)

    def test_allocate_pds_rejects_invalid_dataset_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid MVS data set name"):
            self.client.allocate_pds("HERC01.BAD-NAME", "HERC01")
        self.assertFalse((self.root / "AAINTRDR").exists())

    def test_binary_member_can_be_retried_during_folder_download(self) -> None:
        pds = self.root / "HERC01.PROFILE"
        pds.mkdir()
        (pds / "ISPPROF").write_bytes(b"\x00\x01binary profile")
        destination = Path(self.temp.name) / "download"
        retries = []
        reports = []

        self.client.download(
            ["HERC01.PROFILE"],
            "/",
            destination,
            "text",
            lambda *_args: True,
            reports.append,
            confirm_binary_retry=lambda *args: retries.append(args) or True,
        )

        self.assertEqual((destination / "HERC01.PROFILE" / "ISPPROF").read_bytes(), b"\x00\x01binary profile")
        self.assertEqual(len(retries), 1)
        self.assertIn("Downloaded ISPPROF as binary", reports)

    def test_remote_list_parses_ftpd_list_format(self) -> None:
        (self.root / "SOURCE").mkdir()
        (self.root / "HELLO").write_text("source", encoding="utf-8")

        entries = self.client.list_dir("/")

        self.assertEqual([(entry["name"], entry["kind"]) for entry in entries], [("SOURCE", "dir"), ("HELLO", "file")])
        self.assertEqual(entries[1]["size"], "6")
        self.assertEqual(entries[1]["updated"], "2026-01-01")

    def test_remote_filter_matches_dataset_middle_across_hlqs(self) -> None:
        entries = [
            {"name": "ALPHA.PROD.SOURCE", "kind": "dir", "size": "", "updated": "2025-01-01"},
            {"name": "BETA.TEST.SOURCE", "kind": "dir", "size": "", "updated": "2026-01-01"},
            {"name": "GAMMA.PROD.LOAD", "kind": "file", "size": "100", "updated": "2024-01-01"},
        ]

        matching = filter_sort_remote_entries(entries, ".PROD.", "Name")

        self.assertEqual([entry["name"] for entry in matching], ["ALPHA.PROD.SOURCE", "GAMMA.PROD.LOAD"])

    def test_remote_sort_by_update_date_is_newest_first(self) -> None:
        entries = [
            {"name": "OLDER", "kind": "file", "size": "", "updated": "2024-01-01"},
            {"name": "NEWER", "kind": "file", "size": "", "updated": "2026-01-01"},
        ]

        sorted_entries = filter_sort_remote_entries(entries, "", "Date updated")

        self.assertEqual([entry["name"] for entry in sorted_entries], ["NEWER", "OLDER"])

    def test_declining_remote_file_creation_overwrite_preserves_file(self) -> None:
        existing = self.root / "EMPTY"
        existing.write_text("keep this", encoding="utf-8")

        self.client.create_remote_file("/", "EMPTY", lambda *_args: False)

        self.assertEqual(existing.read_text(encoding="utf-8"), "keep this")


if __name__ == "__main__":
    unittest.main()