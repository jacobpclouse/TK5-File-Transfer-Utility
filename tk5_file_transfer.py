#!/usr/bin/env python3
# chmod +x tk5_file_transfer.py
# ./tk5_file_transfer.py

"""Tkinter client for browsing and transferring files through MVS-TK5 FTPD.

The module is split into validation/configuration helpers, the FtpFiles protocol
adapter, and TransferWindow. The window owns the browser and Quick Sync views;
FTP work runs on background threads while prompts and widget updates stay on Tk's
main thread. Quick Sync stores file-to-member mappings, never login credentials.
"""

from __future__ import annotations

import ftplib
import hashlib
import io
import json
import os
import posixpath
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from uuid import uuid4


APP_TITLE = "TK5 File Transfer"
MVS_QUALIFIER = re.compile(r"[A-Z@$#][A-Z0-9@$#]{0,7}\Z")

COLORS = {
    "background": "#F1F5F2",
    "surface": "#FFFFFF",
    "surface_alt": "#E6EEE9",
    "border": "#D1DDD6",
    "text": "#1C2D26",
    "muted": "#62736A",
    "accent": "#177A5C",
    "accent_hover": "#12664C",
    "vscode": "#007ACC",
    "vscode_hover": "#005A9E",
    "selection": "#D5ECE1",
}


def _validate_command_text(value: str) -> str:
    """Reject line breaks that could inject extra commands into FTP control text."""
    if "\r" in value or "\n" in value:
        raise ValueError("FTP paths and names cannot contain line breaks.")
    return value


def _validate_remote_name(value: str) -> str:
    """Require one safe remote path component before using it in FTP or host paths."""
    _validate_command_text(value)
    if not value or value in (".", "..") or "/" in value or "\\" in value:
        raise ValueError("The FTP server returned an unsafe entry name.")
    return value


def normalize_remote_path(value: str) -> str:
    """Convert relative or absolute FTPD paths to a canonical absolute path."""
    value = _validate_command_text(value.strip())
    return posixpath.normpath("/" + value.lstrip("/"))


def filter_sort_remote_entries(entries: list[dict[str, str]], query: str, sort_mode: str) -> list[dict[str, str]]:
    """Filter one FTPD listing by name and sort it by name or reported date."""
    query = query.strip().casefold()
    visible = [entry for entry in entries if query in entry["name"].casefold()]
    if sort_mode == "Date updated":
        visible.sort(key=lambda entry: entry["name"].casefold())
        visible.sort(key=lambda entry: entry.get("updated", ""), reverse=True)
    else:
        visible.sort(key=lambda entry: (entry["kind"] != "dir", entry["name"].casefold()))
    return visible


def validate_dataset_name(value: str) -> str:
    """Uppercase and validate a fully qualified MVS data set name."""
    name = value.strip().upper()
    qualifiers = name.split(".")
    if len(name) > 44 or not qualifiers or any(not MVS_QUALIFIER.fullmatch(item) for item in qualifiers):
        raise ValueError("Enter a valid MVS data set name, such as HERC01.SOURCE.")
    return name


def validate_member_name(value: str) -> str:
    """Normalize a PDS member name and enforce the MVS eight-character rule."""
    name = value.strip().upper()
    if not re.fullmatch(r"[A-Z@$#][A-Z0-9@$#]{0,7}", name):
        raise ValueError("PDS member names must be 1-8 MVS characters, with no extension (for example HELLO).")
    return name


class NonTextTransferError(ValueError):
    """Raised when a TYPE A download contains bytes that indicate binary data."""

    pass


def quick_sync_config_path() -> Path:
    """Return the per-user config path used for host-to-MVS sync mappings."""
    if sys.platform == "win32":
        config_root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        config_root = Path.home() / "Library" / "Application Support"
    else:
        config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_root / "tk5-file-transfer" / "quick_sync.json"


def load_quick_sync_mappings(config_path: Path) -> list[dict[str, str]]:
    """Load only well-formed Quick Sync entries; ignore a missing/bad config."""
    try:
        records = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(records, list):
        return []
    required = {"id", "local_path", "remote_directory", "member_name"}
    return [
        {key: record[key] for key in required} | {
            "last_uploaded_hash": str(record.get("last_uploaded_hash", "")),
            "last_uploaded_at": str(record.get("last_uploaded_at", "")),
        }
        for record in records
        if isinstance(record, dict) and required.issubset(record) and all(isinstance(record[key], str) for key in required)
    ]


def save_quick_sync_mappings(config_path: Path, mappings: list[dict[str, str]]) -> None:
    """Atomically save mapping metadata without storing FTP credentials."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text(json.dumps(mappings, indent=2), encoding="utf-8")
    os.replace(temporary, config_path)


def file_sha256(path: Path) -> str:
    """Hash a host file incrementally so Quick Sync can skip unchanged files."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def push_quick_sync_mappings(client, mappings, mode, confirm_overwrite, report, force: bool = False) -> list[dict[str, str]]:
    """Push mapped host files, optionally forcing selected entries, and return hash updates."""
    updates = []
    for mapping in mappings:
        source = Path(mapping["local_path"]).expanduser()
        target_dir = normalize_remote_path(mapping["remote_directory"])
        member = validate_member_name(mapping["member_name"])
        if not source.is_file():
            report(f"Missing host file: {source}")
            continue

        current_hash = file_sha256(source)
        remote_members = {entry["name"].upper() for entry in client.list_dir(target_dir)}
        if not force and current_hash == mapping.get("last_uploaded_hash") and member in remote_members:
            report(f"Unchanged: {source.name}")
            continue

        messages = []
        try:
            client.upload([source], target_dir, mode, confirm_overwrite, messages.append, member)
        except Exception as error:
            report(f"Failed {source.name}: {error}")
            continue

        if any(message.startswith("Uploaded ") for message in messages):
            updates.append({
                "id": mapping["id"],
                "last_uploaded_hash": current_hash,
                "last_uploaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
            report(f"Synced {source.name} to {target_dir}/{member}")
        else:
            for message in messages:
                report(message)
    return updates


class FtpFiles:
    """File operations over the native MVS datasets exposed by TK5 FTPD."""

    def __init__(self, ftp: ftplib.FTP) -> None:
        """Wrap an authenticated ftplib session for MVS-aware file operations."""
        self.ftp = ftp

    @classmethod
    def connect(cls, host: str, port: int, username: str, password: str) -> "FtpFiles":
        """Open a passive FTP connection and authenticate with the supplied RAKF credentials."""
        ftp = ftplib.FTP(timeout=25, encoding="latin-1")
        ftp.connect(host, port)
        ftp.login(username, password)
        ftp.set_pasv(True)
        return cls(ftp)

    def list_dir(self, path: str) -> list[dict[str, str]]:
        """Run FTPD LIST and parse its MVS data set or PDS-member rows."""
        path = normalize_remote_path(path)
        original = self.ftp.pwd()
        try:
            self.ftp.cwd(path)
            lines: list[str] = []
            self.ftp.retrlines("LIST", lines.append)
            entries_by_name: dict[str, dict[str, str]] = {}
            for line in lines:
                if not line or line.lower().startswith("total "):
                    continue
                fields = line.split(maxsplit=8)
                if len(fields) < 9:
                    raise ftplib.error_proto(f"Unexpected FTPD LIST entry: {line}")
                name = _validate_remote_name(fields[8])
                try:
                    updated = datetime.strptime(" ".join(fields[5:8]), "%b %d %Y").date().isoformat()
                except ValueError:
                    updated = ""
                entry = {
                    "name": name,
                    "kind": "dir" if fields[0].startswith("d") else "file",
                    "size": fields[4],
                    "updated": updated,
                }
                previous = entries_by_name.get(name)
                if previous is None or entry["kind"] == "dir":
                    entries_by_name[name] = entry
            return sorted(entries_by_name.values(), key=lambda entry: (entry["kind"] != "dir", entry["name"].casefold()))
        finally:
            self.ftp.cwd(original)

    def _transfer_file(self, local_path: Path, remote_name: str, mode: str) -> None:
        """Upload one host file with FTP ASCII translation or byte-preserving binary mode."""
        remote_name = _validate_remote_name(remote_name)
        if mode == "text":
            with local_path.open("rb") as source:
                self.ftp.storlines(f"STOR {remote_name}", source)
        else:
            with local_path.open("rb") as source:
                self.ftp.storbinary(f"STOR {remote_name}", source)

    def _receive_file(self, remote_name: str, destination: Path, mode: str) -> None:
        """Download to a temporary host file, then atomically replace the destination."""
        remote_name = _validate_remote_name(remote_name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        os.close(handle)
        temp_path = Path(temp_name)
        try:
            if mode == "text":
                with temp_path.open("w", encoding="latin-1", newline="\n") as output:
                    def write_text_line(line: str) -> None:
                        """Reject obvious binary records before writing a text-mode result."""
                        if any(ord(character) < 32 and character not in "\t" for character in line):
                            raise NonTextTransferError("member contains binary control bytes")
                        output.write(line + "\n")

                    self.ftp.retrlines(f"RETR {remote_name}", write_text_line)
            else:
                with temp_path.open("wb") as output:
                    self.ftp.retrbinary(f"RETR {remote_name}", output.write)
            os.replace(temp_path, destination)
        except (UnicodeDecodeError, NonTextTransferError):
            temp_path.unlink(missing_ok=True)
            try:
                self.ftp.voidresp()
            except ftplib.all_errors:
                pass
            raise
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    def upload(
        self,
        local_paths: list[Path],
        remote_directory: str,
        mode: str,
        confirm_overwrite,
        report,
        remote_name_override: str = "",
    ) -> None:
        """Upload each selected host item into an existing MVS location."""
        remote_directory = normalize_remote_path(remote_directory)
        for local_path in local_paths:
            self._upload_path(local_path, remote_directory, mode, confirm_overwrite, report, remote_name_override)

    def _upload_path(self, local_path: Path, remote_directory: str, mode: str, confirm, report, remote_name_override: str = "") -> None:
        """Recursively upload folders or place one source file in the target PDS."""
        remote_directory = normalize_remote_path(remote_directory)
        local_path = local_path.resolve()
        remote_entries = {entry["name"]: entry["kind"] for entry in self.list_dir(remote_directory)}
        if remote_name_override and local_path.is_dir():
            raise ValueError("A remote name override can only be used for a single file.")
        if remote_name_override:
            remote_name = _validate_remote_name(remote_name_override.strip())
        elif not local_path.is_dir() and remote_directory.strip("/"):
            remote_name = validate_member_name(local_path.stem)
        else:
            remote_name = _validate_remote_name(local_path.name)
        remote_target = posixpath.join(remote_directory, remote_name)

        if local_path.is_dir():
            existing = remote_entries.get(remote_name)
            if existing == "file":
                raise OSError(f"Cannot upload folder; a remote file already exists: {remote_target}")
            if existing is None:
                raise NotImplementedError(
                    f"FTPD does not support MKD. Allocate the PDS {remote_name} from MVS before uploading this folder."
                )
            for child in sorted(local_path.iterdir(), key=lambda item: item.name.casefold()):
                self._upload_path(child, remote_target, mode, confirm, report)
            return

        existing = remote_entries.get(remote_name)
        if existing == "dir":
            raise OSError(f"Cannot upload file; a remote folder already exists: {remote_target}")
        if existing == "file" and not confirm(local_path, remote_target):
            report(f"Skipped {local_path.name}")
            return
        self.ftp.cwd(remote_directory)
        self._transfer_file(local_path, remote_name, mode)
        report(f"Uploaded {local_path.name}")

    def download(
        self,
        remote_paths: list[str],
        remote_directory: str,
        local_directory: Path,
        mode: str,
        confirm_overwrite,
        report,
        local_name_override: str = "",
        confirm_binary_retry=None,
    ) -> None:
        """Download selected remote items, optionally retrying non-text members as binary."""
        remote_directory = normalize_remote_path(remote_directory)
        local_directory.mkdir(parents=True, exist_ok=True)
        if local_name_override and len(remote_paths) != 1:
            raise ValueError("A local name override can only be used when downloading one file.")
        for remote_name in remote_paths:
            self._download_path(
                remote_name,
                remote_directory,
                local_directory,
                mode,
                confirm_overwrite,
                report,
                local_name_override,
                confirm_binary_retry,
            )

    def _download_path(
        self,
        remote_name: str,
        remote_directory: str,
        local_directory: Path,
        mode: str,
        confirm,
        report,
        local_name_override: str = "",
        confirm_binary_retry=None,
    ) -> None:
        """Download a PDS member or recurse through its member listing."""
        remote_directory = normalize_remote_path(remote_directory)
        remote_name = _validate_remote_name(remote_name)
        self.ftp.cwd(remote_directory)
        kind = next((entry["kind"] for entry in self.list_dir(remote_directory) if entry["name"] == remote_name), "file")
        if local_name_override and kind == "dir":
            raise ValueError("A local name override can only be used when downloading one file.")
        local_name = _validate_remote_name(local_name_override.strip() if local_name_override else remote_name)
        local_path = local_directory / local_name

        if kind == "dir":
            if local_path.exists() and not local_path.is_dir():
                raise OSError(f"Cannot download folder; a local file already exists: {local_path}")
            local_path.mkdir(parents=True, exist_ok=True)
            for entry in self.list_dir(posixpath.join(remote_directory, remote_name)):
                self._download_path(
                    entry["name"],
                    posixpath.join(remote_directory, remote_name),
                    local_path,
                    mode,
                    confirm,
                    report,
                    confirm_binary_retry=confirm_binary_retry,
                )
            return

        if local_path.exists() and not confirm(local_path, posixpath.join(remote_directory, remote_name)):
            report(f"Skipped {remote_name}")
            return
        self.ftp.cwd(remote_directory)
        try:
            self._receive_file(remote_name, local_path, mode)
        except (UnicodeDecodeError, NonTextTransferError) as error:
            if mode != "text" or not confirm_binary_retry or not confirm_binary_retry(remote_name, local_path, str(error)):
                report(f"Skipped {remote_name}: non-text data in text mode")
                return
            self._receive_file(remote_name, local_path, "binary")
            report(f"Downloaded {remote_name} as binary")
            return
        report(f"Downloaded {remote_name}")

    def allocate_pds(self, dataset_name: str, notify_user: str) -> str:
        """Submit a constrained PDS allocation job through FTPD's internal reader."""
        dataset_name = validate_dataset_name(dataset_name)
        notify_user = validate_member_name(notify_user)
        if any(entry["name"].upper() == dataset_name for entry in self.list_dir("/")):
            raise FileExistsError(f"Data set {dataset_name} already appears in the FTPD catalog listing.")

        job_name = f"PDS{uuid4().hex[:5].upper()}"
        jcl = [
            f"//{job_name} JOB (SYS),'ALLOC PDS',CLASS=A,MSGCLASS=X,NOTIFY={notify_user}",
            "//ALLOC    EXEC PGM=IEFBR14",
            f"//NEWPDS   DD DSN={dataset_name},DISP=(NEW,CATLG,DELETE),",
            "//            UNIT=3390,VOL=SER=TSO001,",
            "//            SPACE=(TRK,(10,5,5)),",
            "//            DCB=(DSORG=PO,RECFM=FB,LRECL=80,BLKSIZE=800)",
            "/*",
        ]
        original = self.ftp.pwd()
        try:
            self.ftp.cwd("/")
            self.ftp.storlines("STOR AAINTRDR", io.BytesIO(("\r\n".join(jcl) + "\r\n").encode("ascii")))
        finally:
            self.ftp.cwd(original)
        return job_name

    def create_remote_file(self, parent: str, name: str, confirm_overwrite) -> None:
        """Create an empty PDS member, asking before replacing an existing member."""
        parent = normalize_remote_path(parent)
        name = validate_member_name(name)
        entries = {entry["name"]: entry["kind"] for entry in self.list_dir(parent)}
        if entries.get(name) == "dir":
            raise FileExistsError(f"A remote folder named {name} already exists.")
        if entries.get(name) == "file" and not confirm_overwrite(Path(name), posixpath.join(parent, name)):
            return
        self.ftp.cwd(parent)
        self.ftp.storbinary(f"STOR {name}", io.BytesIO(b""))

    def close(self) -> None:
        """Gracefully end the FTP session, falling back to closing its socket."""
        try:
            self.ftp.quit()
        except ftplib.all_errors:
            self.ftp.close()


class TransferWindow:
    """Build the Tk interface and coordinate transfers, dialogs, and Quick Sync."""

    def __init__(self, root: tk.Tk) -> None:
        """Initialize state, build the interface, and start the Tk event poller."""
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1120x740")
        self.root.minsize(820, 580)
        self._configure_style()
        self.events: queue.Queue = queue.Queue()
        self.ftp: FtpFiles | None = None
        self.busy = False
        self.local_path = tk.StringVar(value=str(Path.home()))
        self.remote_path = tk.StringVar(value="/")
        self.host = tk.StringVar(value="localhost")
        self.port = tk.StringVar(value="2121")
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.use_demo_credentials = tk.BooleanVar(value=False)
        self.transfer_mode = tk.StringVar(value="text")
        self.quick_sync_mode = tk.StringVar(value="text")
        self.remote_name = tk.StringVar()
        self.remote_filter = tk.StringVar()
        self.remote_sort = tk.StringVar(value="Name")
        self.remote_entries: list[dict[str, str]] = []
        self.quick_sync_config = quick_sync_config_path()
        self.quick_sync_mappings = load_quick_sync_mappings(self.quick_sync_config)
        self.status = tk.StringVar(value="Start FTPD on TK5, then connect to begin.")

        self._build_ui()
        self._refresh_local()
        self.root.after(100, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        """Apply the shared colors, typography, row heights, and button states."""
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.root.configure(background=COLORS["background"])
        style.configure("TFrame", background=COLORS["background"])
        style.configure("Pane.TFrame", background=COLORS["surface"])
        style.configure("Header.TFrame", background=COLORS["background"])
        style.configure("TLabel", background=COLORS["background"], foreground=COLORS["text"])
        style.configure("Muted.TLabel", foreground=COLORS["muted"])
        style.configure("Title.TLabel", font=("TkDefaultFont", 18, "bold"), foreground=COLORS["text"])
        style.configure("Subtitle.TLabel", font=("TkDefaultFont", 10), foreground=COLORS["muted"])
        style.configure(
            "TLabelframe",
            background=COLORS["surface"],
            bordercolor=COLORS["border"],
            relief="solid",
            borderwidth=1,
        )
        style.configure("TLabelframe.Label", background=COLORS["surface"], foreground=COLORS["text"], font=("TkDefaultFont", 10, "bold"))
        style.configure("TEntry", padding=(8, 7), fieldbackground=COLORS["surface"], foreground=COLORS["text"], bordercolor=COLORS["border"])
        style.configure("TButton", padding=(10, 7), background=COLORS["surface"], foreground=COLORS["text"], bordercolor=COLORS["border"])
        style.map(
            "TButton",
            background=[("active", COLORS["surface_alt"]), ("disabled", COLORS["background"])],
            foreground=[("disabled", COLORS["muted"])],
        )
        style.configure("Accent.TButton", background=COLORS["accent"], foreground=COLORS["surface"], bordercolor=COLORS["accent"], font=("TkDefaultFont", 10, "bold"))
        style.map("Accent.TButton", background=[("active", COLORS["accent_hover"]), ("disabled", COLORS["surface_alt"])])
        style.configure("VSCode.TButton", background=COLORS["vscode"], foreground=COLORS["surface"], bordercolor=COLORS["vscode"], font=("TkDefaultFont", 10, "bold"))
        style.map("VSCode.TButton", background=[("active", COLORS["vscode_hover"]), ("disabled", COLORS["surface_alt"])], foreground=[("disabled", COLORS["muted"])])
        style.configure("Treeview", rowheight=34, background=COLORS["surface"], fieldbackground=COLORS["surface"], foreground=COLORS["text"], bordercolor=COLORS["border"], borderwidth=0, font=("TkDefaultFont", 10))
        style.configure("Treeview.Heading", background=COLORS["surface_alt"], foreground=COLORS["muted"], font=("TkDefaultFont", 9, "bold"), padding=(9, 9), relief="flat")
        style.map("Treeview", background=[("selected", COLORS["selection"])], foreground=[("selected", COLORS["text"])])
        style.configure("Vertical.TScrollbar", background=COLORS["surface_alt"], troughcolor=COLORS["surface"], bordercolor=COLORS["surface"])
        style.configure("Status.TLabel", background=COLORS["surface_alt"], foreground=COLORS["muted"], padding=(12, 8))

    def _build_ui(self) -> None:
        """Build the connection strip, demo login toggle, tabs, and status line."""
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(18, 14, 18, 8))
        header.pack(fill="x")
        ttk.Label(header, text="TK5 File Transfer", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="MVS 3.8J  /  FTPD", style="Subtitle.TLabel").pack(side="left", padx=(14, 0), pady=(5, 0))

        connection = ttk.LabelFrame(self.root, text="Connection", padding=(12, 10))
        connection.pack(fill="x", padx=16, pady=(4, 8))
        fields = (
            ("Host", self.host, 18),
            ("Port", self.port, 7),
            ("User", self.username, 14),
            ("Password", self.password, 14),
        )
        for column, (label, variable, width) in enumerate(fields):
            ttk.Label(connection, text=label).grid(row=0, column=column * 2, sticky="w", padx=(0, 4))
            entry = ttk.Entry(connection, textvariable=variable, width=width, show="*" if label == "Password" else "")
            entry.grid(row=0, column=column * 2 + 1, sticky="ew", padx=(0, 10))
        connection.columnconfigure(7, weight=1)
        self.connect_button = ttk.Button(connection, text="Connect", command=self._connect)
        self.connect_button.grid(row=0, column=8, padx=(4, 0))
        self.disconnect_button = ttk.Button(connection, text="Disconnect", command=self._disconnect, state="disabled")
        self.disconnect_button.grid(row=0, column=9, padx=(6, 0))
        ttk.Checkbutton(
            connection,
            text="Use TK5 demo credentials (HERC01 / CUL8TR)",
            variable=self.use_demo_credentials,
            command=self._toggle_demo_credentials,
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Label(connection, text="Optional; not saved", style="Muted.TLabel").grid(row=1, column=5, columnspan=4, sticky="w", pady=(8, 0))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        self.browser_tab = ttk.Frame(self.notebook)
        self.quick_sync_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.browser_tab, text="Browse / Transfer")
        self.notebook.add(self.quick_sync_tab, text="Quick Sync")

        mode_frame = ttk.LabelFrame(self.browser_tab, text="Transfer options", padding=(12, 8))
        mode_frame.pack(fill="x", padx=16, pady=(0, 8))
        ttk.Label(mode_frame, text="Mode", style="Muted.TLabel").pack(side="left")
        ttk.Radiobutton(mode_frame, text="Text / source", variable=self.transfer_mode, value="text").pack(side="left", padx=(10, 5))
        ttk.Radiobutton(mode_frame, text="Binary", variable=self.transfer_mode, value="binary").pack(side="left", padx=(5, 18))
        ttk.Label(mode_frame, text="Name override", style="Muted.TLabel").pack(side="left", padx=(0, 7))
        ttk.Entry(mode_frame, textvariable=self.remote_name, width=18).pack(side="left")
        ttk.Label(mode_frame, text="One file only: HELLO for a PDS upload; HELLO.cbl for download.", style="Muted.TLabel").pack(side="left", padx=10)

        panes = ttk.Panedwindow(self.browser_tab, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=0, pady=(0, 8))
        local_frame = ttk.LabelFrame(panes, text="Host files", padding=10)
        remote_frame = ttk.LabelFrame(panes, text="MVS data sets and members", padding=10)
        panes.add(local_frame, weight=1)
        panes.add(remote_frame, weight=1)
        self.local_tree = self._build_file_pane(local_frame, self.local_path, self._refresh_local, self._local_double_click)
        self.remote_tree = self._build_file_pane(remote_frame, self.remote_path, self._refresh_remote, self._remote_double_click)

        local_actions = ttk.Frame(local_frame, style="Pane.TFrame")
        local_actions.pack(fill="x", before=self.local_tree, pady=(7, 0))
        ttk.Button(local_actions, text="New file", command=self._new_local_file).pack(side="left")
        ttk.Button(local_actions, text="New folder", command=self._new_local_folder).pack(side="left", padx=(6, 0))
        self.open_folder_button = ttk.Button(local_actions, text="Open current folder in VS Code", style="VSCode.TButton", command=self._open_current_folder)
        self.open_folder_button.pack(side="right")
        self.local_tree.bind("<<TreeviewSelect>>", self._update_host_actions, add="+")

        remote_filter_bar = ttk.Frame(remote_frame, style="Pane.TFrame")
        remote_filter_bar.pack(fill="x", before=self.remote_tree, pady=(7, 0))
        ttk.Label(remote_filter_bar, text="Find name", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        filter_entry = ttk.Entry(remote_filter_bar, textvariable=self.remote_filter)
        filter_entry.pack(side="left", fill="x", expand=True)
        ttk.Label(remote_filter_bar, text="Sort", style="Muted.TLabel").pack(side="left", padx=(12, 6))
        sort_box = ttk.Combobox(remote_filter_bar, textvariable=self.remote_sort, values=("Name", "Date updated"), state="readonly", width=15)
        sort_box.pack(side="left")
        sort_box.bind("<<ComboboxSelected>>", lambda _event: self._render_remote_entries())
        self.remote_filter.trace_add("write", lambda *_args: self._render_remote_entries())

        remote_actions = ttk.Frame(remote_frame, style="Pane.TFrame")
        remote_actions.pack(fill="x", before=self.remote_tree, pady=(7, 0))
        self.new_remote_file_button = ttk.Button(remote_actions, text="New member", command=self._new_remote_file, state="disabled")
        self.new_remote_file_button.pack(side="left")
        self.new_remote_folder_button = ttk.Button(remote_actions, text="Create PDS", command=self._new_remote_folder, state="disabled")
        self.new_remote_folder_button.pack(side="left", padx=(6, 0))
        self.refresh_catalog_button = ttk.Button(remote_actions, text="Refresh catalog", command=self._refresh_remote_catalog, state="disabled")
        self.refresh_catalog_button.pack(side="left", padx=(6, 0))

        transfer_bar = ttk.Frame(self.browser_tab, padding=(0, 4, 0, 10))
        transfer_bar.pack(fill="x")
        self.upload_button = ttk.Button(transfer_bar, text="Upload selected  →", style="Accent.TButton", command=self._upload_selected, state="disabled")
        self.upload_button.pack(side="left")
        self.download_button = ttk.Button(transfer_bar, text="←  Download selected", style="Accent.TButton", command=self._download_selected, state="disabled")
        self.download_button.pack(side="left", padx=(8, 0))
        self.upload_folder_button = ttk.Button(transfer_bar, text="Upload folder…", command=self._upload_folder, state="disabled")
        self.upload_folder_button.pack(side="left", padx=(8, 0))

        self._build_quick_sync_tab()

        ttk.Label(self.root, textvariable=self.status, anchor="w", style="Status.TLabel").pack(fill="x", side="bottom")

    def _build_quick_sync_tab(self) -> None:
        """Build controls for persistent host-file-to-PDS member mappings."""
        content = ttk.Frame(self.quick_sync_tab, padding=16)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="Mapped source files", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            content,
            text="Map each host source file to a PDS member. Quick Sync pushes changed host files; overwrites still require confirmation.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 12))
        toolbar = ttk.Frame(content)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="Transfer mode", style="Muted.TLabel").pack(side="left")
        ttk.Radiobutton(toolbar, text="Text / source", variable=self.quick_sync_mode, value="text").pack(side="left", padx=(8, 4))
        ttk.Radiobutton(toolbar, text="Binary", variable=self.quick_sync_mode, value="binary").pack(side="left", padx=(4, 16))
        self.quick_add_button = ttk.Button(toolbar, text="Add mapping…", command=self._add_quick_sync_mapping)
        self.quick_add_button.pack(side="left")
        self.quick_remove_button = ttk.Button(toolbar, text="Remove mapping", command=self._remove_quick_sync_mapping, state="disabled")
        self.quick_remove_button.pack(side="left", padx=(6, 0))
        self.quick_push_button = ttk.Button(toolbar, text="Push selected", style="Accent.TButton", command=self._push_quick_sync_selected, state="disabled")
        self.quick_push_button.pack(side="right")
        self.quick_push_all_button = ttk.Button(toolbar, text="Push changed files", style="Accent.TButton", command=self._push_quick_sync_all, state="disabled")
        self.quick_push_all_button.pack(side="right", padx=(0, 6))

        self.quick_sync_tree = ttk.Treeview(content, columns=("source", "target", "last"), show="headings", selectmode="extended")
        self.quick_sync_tree.heading("source", text="Host source")
        self.quick_sync_tree.heading("target", text="MVS PDS member")
        self.quick_sync_tree.heading("last", text="Last pushed")
        self.quick_sync_tree.column("source", minwidth=180, width=360)
        self.quick_sync_tree.column("target", minwidth=180, width=360)
        self.quick_sync_tree.column("last", minwidth=130, width=180, stretch=False)
        scrollbar = ttk.Scrollbar(content, orient="vertical", command=self.quick_sync_tree.yview, style="Vertical.TScrollbar")
        self.quick_sync_tree.configure(yscrollcommand=scrollbar.set)
        self.quick_sync_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.quick_sync_tree.bind("<<TreeviewSelect>>", self._update_quick_sync_actions)
        self._render_quick_sync_mappings()

    def _render_quick_sync_mappings(self) -> None:
        """Display saved local-file to PDS-member pairs and their last push time."""
        if not hasattr(self, "quick_sync_tree"):
            return
        self.quick_sync_tree.delete(*self.quick_sync_tree.get_children())
        for mapping in self.quick_sync_mappings:
            target = f"{mapping['remote_directory'].rstrip('/')}/{mapping['member_name']}"
            self.quick_sync_tree.insert(
                "",
                "end",
                iid=mapping["id"],
                values=(mapping["local_path"], target, mapping.get("last_uploaded_at", "Never")),
            )
        self._update_quick_sync_actions()

    def _update_quick_sync_actions(self, _event=None) -> None:
        """Enable mapping actions according to connection, busy, and selection state."""
        busy = self.busy
        connected = self.ftp is not None
        selected = bool(self.quick_sync_tree.selection())
        self.quick_add_button.configure(state="disabled" if busy else "normal")
        self.quick_remove_button.configure(state="normal" if selected and not busy else "disabled")
        self.quick_push_button.configure(state="normal" if selected and connected and not busy else "disabled")
        self.quick_push_all_button.configure(
            state="normal" if self.quick_sync_mappings and connected and not busy else "disabled"
        )

    def _add_quick_sync_mapping(self) -> None:
        """Ask for a host source and its PDS/member target, then save that mapping."""
        source_name = filedialog.askopenfilename(
            parent=self.root,
            title="Choose a host source file",
            initialdir=self.local_path.get(),
        )
        if not source_name:
            return
        source = Path(source_name).expanduser().resolve()
        if not source.is_file():
            messagebox.showerror("Quick Sync", "Choose a regular host file.", parent=self.root)
            return

        current_remote = normalize_remote_path(self.remote_path.get()) if self.ftp else "/"
        remote_directory = simpledialog.askstring(
            "Quick Sync target",
            "Remote PDS path (for example /HERC01.SOURCE):",
            initialvalue=current_remote if current_remote != "/" else "/HERC01.SOURCE",
            parent=self.root,
        )
        if not remote_directory:
            return
        try:
            remote_directory = normalize_remote_path(remote_directory)
            if remote_directory == "/":
                raise ValueError("Choose a PDS, not the MVS root listing.")
            default_member = validate_member_name(source.stem)
        except ValueError as error:
            messagebox.showerror("Quick Sync target", str(error), parent=self.root)
            return

        member_name = simpledialog.askstring(
            "Quick Sync member",
            "PDS member name:",
            initialvalue=default_member,
            parent=self.root,
        )
        if not member_name:
            return
        try:
            member_name = validate_member_name(member_name)
        except ValueError as error:
            messagebox.showerror("Quick Sync member", str(error), parent=self.root)
            return

        mapping = {
            "id": uuid4().hex,
            "local_path": str(source),
            "remote_directory": remote_directory,
            "member_name": member_name,
            "last_uploaded_hash": "",
            "last_uploaded_at": "",
        }
        duplicate = next(
            (
                item
                for item in self.quick_sync_mappings
                if item["local_path"] == mapping["local_path"]
                and item["remote_directory"] == remote_directory
                and item["member_name"] == member_name
            ),
            None,
        )
        if duplicate:
            messagebox.showinfo("Quick Sync", "That host file is already mapped to this PDS member.", parent=self.root)
            return
        self.quick_sync_mappings.append(mapping)
        self._save_quick_sync_config()
        self._render_quick_sync_mappings()

    def _save_quick_sync_config(self) -> None:
        """Persist mappings only; FTP usernames and passwords are never saved."""
        try:
            save_quick_sync_mappings(self.quick_sync_config, self.quick_sync_mappings)
        except OSError as error:
            messagebox.showerror("Quick Sync configuration", str(error), parent=self.root)

    def _remove_quick_sync_mapping(self) -> None:
        """Remove selected mapping records without deleting either source file."""
        selected = set(self.quick_sync_tree.selection())
        if not selected:
            return
        self.quick_sync_mappings = [item for item in self.quick_sync_mappings if item["id"] not in selected]
        self._save_quick_sync_config()
        self._render_quick_sync_mappings()

    def _push_quick_sync_selected(self) -> None:
        """Push only the mappings selected in the Quick Sync table."""
        selected = set(self.quick_sync_tree.selection())
        self._start_quick_sync([item for item in self.quick_sync_mappings if item["id"] in selected], force=True)

    def _push_quick_sync_all(self) -> None:
        """Push all mapped files whose host content has changed since the last push."""
        self._start_quick_sync(self.quick_sync_mappings)

    def _start_quick_sync(self, mappings: list[dict[str, str]], force: bool = False) -> None:
        """Run a snapshot of the selected mapping set on the FTP worker thread."""
        if not self.ftp or not mappings:
            return
        client = self.ftp
        selected_mappings = deepcopy(mappings)
        mode = self.quick_sync_mode.get()
        self._start_task(
            "quick_sync",
            lambda: push_quick_sync_mappings(client, selected_mappings, mode, self._confirm_overwrite, self._report, force),
        )

    def _toggle_demo_credentials(self) -> None:
        """Fill the built-in TK5 demo credentials only while the checkbox is opted in."""
        if self.use_demo_credentials.get():
            self.username.set("HERC01")
            self.password.set("CUL8TR")
        elif self.username.get() == "HERC01" and self.password.get() == "CUL8TR":
            self.username.set("")
            self.password.set("")

    def _build_file_pane(self, parent, path_var, refresh, double_click):
        """Create a navigable path bar and sortable-looking file listing pane."""
        path_row = ttk.Frame(parent, style="Pane.TFrame")
        path_row.pack(fill="x", pady=(0, 2))
        ttk.Entry(path_row, textvariable=path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(path_row, text="Go", command=refresh, width=5, style="Accent.TButton").pack(side="left", padx=(6, 0))
        ttk.Button(path_row, text="Up", command=lambda: self._go_up(path_var, refresh), width=5).pack(side="left", padx=(6, 0))

        tree = ttk.Treeview(parent, columns=("kind", "size", "updated"), show="tree headings", selectmode="extended")
        tree.heading("#0", text="Name", anchor="w")
        tree.heading("kind", text="Type", anchor="w")
        tree.heading("size", text="Size", anchor="e")
        tree.heading("updated", text="Updated", anchor="w")
        tree.column("#0", minwidth=140, width=280)
        tree.column("kind", width=82, stretch=False)
        tree.column("size", width=86, anchor="e", stretch=False)
        tree.column("updated", width=105, anchor="w", stretch=False)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=tree.yview, style="Vertical.TScrollbar")
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        tree.bind("<Double-1>", double_click)
        tree.bind("<Return>", double_click)
        return tree

    def _go_up(self, path_var, refresh) -> None:
        """Navigate the host or MVS pane to its parent directory."""
        current = path_var.get().strip()
        if path_var is self.local_path:
            path_var.set(str(Path(current).parent))
        else:
            path_var.set(posixpath.dirname(normalize_remote_path(current).rstrip("/")) or "/")
        refresh()

    def _refresh_local(self) -> None:
        """Reload host directory rows and update selection-dependent actions."""
        try:
            path = Path(self.local_path.get()).expanduser().resolve()
            if not path.is_dir():
                raise NotADirectoryError(path)
            self.local_path.set(str(path))
            self.local_tree.delete(*self.local_tree.get_children())
            for entry in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
                kind = "Folder" if entry.is_dir() else "File"
                size = "" if entry.is_dir() else str(entry.stat().st_size)
                updated = datetime.fromtimestamp(entry.stat().st_mtime).strftime("%Y-%m-%d")
                self.local_tree.insert("", "end", iid=entry.name, text=entry.name, values=(kind, size, updated))
            self._update_host_actions()
            self.status.set(f"Host: {path}")
        except (OSError, tk.TclError) as error:
            messagebox.showerror("Host folder", str(error), parent=self.root)

    def _refresh_remote(self) -> None:
        """List the current MVS location asynchronously through FTPD."""
        if not self.ftp:
            return
        path = normalize_remote_path(self.remote_path.get())
        self.remote_path.set(path)
        self._start_task("remote_list", lambda: (path, self.ftp.list_dir(path)))

    def _refresh_remote_catalog(self) -> None:
        """Ask FTPD to rescan its VTOC cache, then refresh the MVS pane."""
        if not self.ftp:
            return
        client = self.ftp
        path = normalize_remote_path(self.remote_path.get())
        self.remote_path.set(path)
        self._start_task("remote_list", lambda: self._rescan_remote_catalog(client, path))

    @staticmethod
    def _rescan_remote_catalog(client: FtpFiles, path: str):
        """Refresh FTPD's catalog cache and return the requested LIST result."""
        client.ftp.sendcmd("REFR")
        return path, client.list_dir(path)

    def _render_remote_entries(self) -> None:
        """Apply the current name filter and sort to cached FTPD entries."""
        if not hasattr(self, "remote_tree"):
            return
        entries = filter_sort_remote_entries(self.remote_entries, self.remote_filter.get(), self.remote_sort.get())
        self.remote_tree.delete(*self.remote_tree.get_children())
        for entry in entries:
            kind_name = "Folder" if entry["kind"] == "dir" else "File"
            self.remote_tree.insert(
                "",
                "end",
                iid=entry["name"],
                text=entry["name"],
                values=(kind_name, entry["size"], entry.get("updated", "")),
            )

    def _local_double_click(self, _event=None) -> None:
        """Open a selected host folder in the left-side browser pane."""
        selected = self.local_tree.selection()
        if selected and self.local_tree.set(selected[0], "kind") == "Folder":
            self.local_path.set(str(Path(self.local_path.get()) / selected[0]))
            self._refresh_local()

    def _remote_double_click(self, _event=None) -> None:
        """Open a selected PDS or data set folder in the MVS browser pane."""
        selected = self.remote_tree.selection()
        if selected and self.remote_tree.set(selected[0], "kind") == "Folder":
            self.remote_path.set(posixpath.join(normalize_remote_path(self.remote_path.get()), selected[0]))
            self._refresh_remote()

    def _connect(self) -> None:
        """Validate connection fields and establish FTPD login off the UI thread."""
        try:
            port = int(self.port.get())
            if not 1 <= port <= 65535:
                raise ValueError("Port must be between 1 and 65535.")
        except ValueError as error:
            messagebox.showerror("Connection", str(error), parent=self.root)
            return
        host, username, password = self.host.get().strip(), self.username.get().strip(), self.password.get()
        self._start_task("connect", lambda: FtpFiles.connect(host, port, username, password))

    def _disconnect(self) -> None:
        """Close the active FTP session and disable remote actions."""
        if self.ftp:
            client, self.ftp = self.ftp, None
            self._start_task("disconnect", client.close)
            self._set_connected(False)

    def _selected_local_paths(self) -> list[Path]:
        """Return the selected host entries as paths under the current folder."""
        return [Path(self.local_path.get()) / name for name in self.local_tree.selection()]

    def _selected_remote_names(self) -> list[str]:
        """Return selected MVS entry names from the current FTPD listing."""
        return list(self.remote_tree.selection())

    def _update_host_actions(self, _event=None) -> None:
        """Keep the VS Code action enabled only for an available host folder."""
        can_open = not self.busy and Path(self.local_path.get()).expanduser().is_dir()
        self.open_folder_button.configure(state="normal" if can_open else "disabled")

    def _open_current_folder(self) -> None:
        """Launch VS Code on the current host directory, not the selected row."""
        folder = Path(self.local_path.get()).expanduser().resolve()
        if not folder.is_dir():
            messagebox.showerror("Host folder", f"Not a folder: {folder}", parent=self.root)
            return
        executable = shutil.which("code") or shutil.which("code-insiders")
        if not executable:
            messagebox.showerror("VS Code not found", "The 'code' command is not on PATH.", parent=self.root)
            return
        try:
            subprocess.Popen([executable, str(folder)])
        except OSError as error:
            messagebox.showerror("Open in VS Code", str(error), parent=self.root)

    def _upload_selected(self) -> None:
        """Upload selected host entries into the current MVS location."""
        if not self.ftp:
            return
        paths = self._selected_local_paths()
        if paths:
            client, target, mode, remote_name = self.ftp, self.remote_path.get(), self.transfer_mode.get(), self.remote_name.get()
            if remote_name.strip() and (len(paths) != 1 or paths[0].is_dir()):
                messagebox.showerror("Remote name", "A remote name can only be used when uploading one file.", parent=self.root)
                return
            self._start_task("upload", lambda: client.upload(paths, target, mode, self._confirm_overwrite, self._report, remote_name))

    def _download_selected(self) -> None:
        """Download selected MVS entries into the current host directory."""
        if not self.ftp:
            return
        names = self._selected_remote_names()
        if names:
            client, source, target, mode, local_name = self.ftp, self.remote_path.get(), Path(self.local_path.get()), self.transfer_mode.get(), self.remote_name.get()
            if local_name.strip() and (len(names) != 1 or self.remote_tree.set(names[0], "kind") == "Folder"):
                messagebox.showerror("Local name", "A name override can only be used when downloading one file.", parent=self.root)
                return
            self._start_task(
                "download",
                lambda: client.download(names, source, target, mode, self._confirm_overwrite, self._report, local_name, self._confirm_binary_retry),
            )

    def _upload_folder(self) -> None:
        """Choose a host directory and upload it beneath the current MVS path."""
        folder = filedialog.askdirectory(parent=self.root, title="Choose a host folder to upload")
        if folder and self.ftp:
            self.remote_name.set("")
            client, mode, target = self.ftp, self.transfer_mode.get(), self.remote_path.get()
            self._start_task("upload", lambda: client.upload([Path(folder)], target, mode, self._confirm_overwrite, self._report))

    def _new_local_file(self) -> None:
        """Create an empty host file, confirming before replacing an existing file."""
        name = simpledialog.askstring("New host file", "File name:", parent=self.root)
        if not name:
            return
        path = Path(self.local_path.get()) / name.strip()
        try:
            if path.name != name.strip() or name.strip() in (".", ".."):
                raise ValueError("Enter a single file name, without path separators.")
            if path.exists() and not messagebox.askyesno("Replace host file?", f"Replace this host file?\n\n{path}", parent=self.root):
                return
            path.write_bytes(b"")
            self._refresh_local()
        except (OSError, ValueError) as error:
            messagebox.showerror("New host file", str(error), parent=self.root)

    def _new_local_folder(self) -> None:
        """Create one host directory beneath the current host path."""
        name = simpledialog.askstring("New host folder", "Folder name:", parent=self.root)
        if not name:
            return
        try:
            path = Path(self.local_path.get()) / name.strip()
            if path.name != name.strip() or name.strip() in (".", ".."):
                raise ValueError("Enter a single folder name, without path separators.")
            path.mkdir()
            self._refresh_local()
        except (OSError, ValueError) as error:
            messagebox.showerror("New host folder", str(error), parent=self.root)

    def _new_remote_file(self) -> None:
        """Create an empty MVS member in the currently displayed PDS."""
        name = simpledialog.askstring("New remote file", "File name:", parent=self.root)
        if name and self.ftp:
            client, parent = self.ftp, self.remote_path.get()
            self._start_task("remote_change", lambda: client.create_remote_file(parent, name, self._confirm_overwrite))

    def _new_remote_folder(self) -> None:
        """Request confirmation and submit the generated PDS allocation job."""
        dataset_name = simpledialog.askstring(
            "Create MVS PDS",
            "Data set name (for example HERC01.SOURCE):",
            parent=self.root,
        )
        if not dataset_name or not self.ftp:
            return
        try:
            dataset_name = validate_dataset_name(dataset_name)
        except ValueError as error:
            messagebox.showerror("Create MVS PDS", str(error), parent=self.root)
            return
        if not messagebox.askyesno(
            "Submit allocation job?",
            f"Submit JCL to allocate {dataset_name} on TSO001 as an FB/80 PDS?\n\nThe internal-reader job runs as RAKF PROD/PRDGROUP, not as your FTP user. RAKF must grant that group ALTER access to the requested data set.",
            parent=self.root,
        ):
            return
        client, notify_user = self.ftp, self.username.get().strip().upper()
        self._start_task("allocate_pds", lambda: client.allocate_pds(dataset_name, notify_user))

    def _confirm_binary_retry(self, remote_name: str, local_path: Path, reason: str) -> bool:
        """Ask on the Tk thread whether a non-text member should retry as binary."""
        answered = threading.Event()
        response: list[bool] = []
        self.events.put(("binary_confirm", (remote_name, local_path, reason, answered, response)))
        answered.wait()
        return response[0]

    def _confirm_overwrite(self, local_path: Path, remote_path: str) -> bool:
        """Ask on the Tk thread before replacing an existing transfer destination."""
        answered = threading.Event()
        response: list[bool] = []
        self.events.put(("confirm", (local_path, remote_path, answered, response)))
        answered.wait()
        return response[0]

    def _report(self, text: str) -> None:
        """Queue a worker status message for safe display by the Tk event loop."""
        self.events.put(("status", text))

    def _start_task(self, kind: str, work) -> None:
        """Run blocking FTP work in a worker and return results through the event queue."""
        if self.busy:
            return
        self.busy = True
        self._set_busy(True)
        self.status.set("Working…")

        def run() -> None:
            """Execute one queued background operation and report result or error."""
            try:
                self.events.put(("result", (kind, work())))
            except Exception as error:
                self.events.put(("error", (kind, str(error))))
            finally:
                self.events.put(("done", kind))

        threading.Thread(target=run, daemon=True).start()

    def _poll_events(self) -> None:
        """Drain worker events, show dialogs, update widgets, and schedule the next poll."""
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "confirm":
                    local_path, remote_path, answered, response = payload
                    approved = messagebox.askyesno(
                        "Confirm overwrite",
                        f"Replace the existing destination?\n\nHost: {local_path}\nTK5:  {remote_path}",
                        parent=self.root,
                    )
                    response.append(approved)
                    answered.set()
                elif event == "status":
                    self.status.set(payload)
                elif event == "error":
                    _kind, text = payload
                    if "not opened 13" in text.casefold():
                        text += (
                            "\n\nFTPD could not open the MVS target. Open an existing writable PDS, "
                            "use a valid member name of 1-8 characters without an extension, and check RAKF update access."
                        )
                    self.status.set(f"Error: {text}")
                    messagebox.showerror("Transfer error", text, parent=self.root)
                elif event == "binary_confirm":
                    remote_name, local_path, reason, answered, response = payload
                    approved = messagebox.askyesno(
                        "Non-text PDS member",
                        f"{remote_name} could not be downloaded as text ({reason}).\n\nRetry this member in binary mode? It will preserve the original bytes, which may not be readable as source text. Choose No to skip it and continue the folder download.\n\nHost path: {local_path}",
                        parent=self.root,
                    )
                    response.append(approved)
                    answered.set()
                elif event == "result":
                    kind, result = payload
                    if kind == "connect":
                        self.ftp = result
                        self.remote_path.set(result.ftp.pwd())
                        self._set_connected(True)
                        self.root.after(0, self._refresh_remote)
                    elif kind == "remote_list":
                        path, entries = result
                        self.remote_path.set(path)
                        self.remote_entries = entries
                        self._render_remote_entries()
                        self.status.set(f"TK5: {path} ({len(self.remote_tree.get_children())}/{len(entries)} shown)")
                    elif kind == "allocate_pds":
                        self.status.set(f"Allocation job {result} submitted. Wait for JES completion, then press Go to refresh.")
                    elif kind == "quick_sync":
                        updates = {item["id"]: item for item in result}
                        for mapping in self.quick_sync_mappings:
                            if mapping["id"] in updates:
                                mapping.update(updates[mapping["id"]])
                        self._save_quick_sync_config()
                        self._render_quick_sync_mappings()
                        if not result:
                            self.status.set("No mapped files changed or were pushed.")
                elif event == "done":
                    kind = payload
                    self.busy = False
                    self._set_busy(False)
                    if kind == "disconnect":
                        self.status.set("Disconnected.")
                    elif kind in ("upload", "download", "remote_change", "quick_sync"):
                        self._refresh_local()
                        if self.ftp:
                            self._refresh_remote()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _set_connected(self, connected: bool) -> None:
        """Update controls whose availability depends on an FTP connection."""
        self.connect_button.configure(state="disabled" if connected else "normal")
        self.disconnect_button.configure(state="normal" if connected else "disabled")
        for button in (self.upload_button, self.download_button, self.upload_folder_button, self.new_remote_file_button, self.new_remote_folder_button, self.refresh_catalog_button):
            button.configure(state="normal" if connected and not self.busy else "disabled")
        self._update_quick_sync_actions()

    def _set_busy(self, busy: bool) -> None:
        """Disable conflicting controls while a serialized FTP operation runs."""
        self.connect_button.configure(state="disabled" if busy or self.ftp else "normal")
        self.disconnect_button.configure(state="disabled" if busy or not self.ftp else "normal")
        for button in (self.upload_button, self.download_button, self.upload_folder_button, self.new_remote_file_button, self.new_remote_folder_button, self.refresh_catalog_button):
            button.configure(state="normal" if self.ftp and not busy else "disabled")
        self._update_host_actions()
        self._update_quick_sync_actions()

    def _on_close(self) -> None:
        """Close the FTP socket and destroy the root window."""
        if self.ftp:
            try:
                self.ftp.ftp.close()
            except ftplib.all_errors:
                pass
        self.root.destroy()


def main() -> None:
    """Create the Tk root window and run the desktop event loop."""
    root = tk.Tk()
    TransferWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()