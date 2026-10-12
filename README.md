# TK5 File Transfer Utility

A small Python/Tkinter desktop utility for moving source files between a host computer and MVS datasets exposed by the standalone FTPD server in MVS Turnkey 5. MAKE SURE THE APP IS IN CONSOLE MODE BEFORE USING.

## Start FTPD in TK5

At the interactive Hercules console, start the standalone FTP daemon without parameters:

```text
/S FTPD
```

Keep the MVS-TK5 Hercules console running, wait for its `herc =====>` prompt, and enter the command there, not in a Linux shell. The TK5 FTPD procedure does not accept `SRVPORT` on the `START` command; doing so causes `IEF657I SYMBOL NOT DEFINED IN PROCEDURE` and a JCL error. Its port is configured in `SYS1.PARMLIB(FTPDPM00)` instead. The distributed FTPD configuration defaults to port `2121`, which is also the app's default. If you change the configured port, use the same port in the app and restart FTPD.

After starting, look for FTPD startup messages confirming the listener port. If there are no MVS/FTPD messages, the command did not reach the active guest operator console or FTPD did not start. On Linux, verify the host listener with `ss -ltn | grep ':2121'`; it should show a `LISTEN` socket before you connect.

FTPD authenticates through RAKF. Use an FTP-authorized TK5 user, such as `HERC01` with the credentials configured on your system. The daemon must be allowed by your RAKF `FTPAUTH` profile. If login fails, check FTPD startup messages and the RAKF authorization setup.

ENABLE CONSOLE MODE BY GOING INTO mvs-tk5/unattended and running: **set_console_mode**

## What the remote pane represents

This app connects to the standalone FTPD, not HTTPD's embedded FTP service on port `8021`. The TK5 FTPD uses `LIST` to show MVS data sets and PDS members; its listing is presented as a tree for navigation. A PDS appears as a folder and its members as files.

For a PDS source member, use a valid MVS member name (up to 8 characters, typically no extension), and navigate to the target PDS before uploading. For example, upload the sample as member `HELLO` into your source PDS, not as a host path such as `tests/samples/HELLO.cbl`. FTPD cannot allocate a new sequential data set through `STOR`; allocate one from MVS first. Dataset names and member names must follow MVS naming rules.

## Requirements

- Python 3.10 or newer
- Tkinter (`python3-tk` on some Linux installations)
- MVS-TK5 with its FTPD started task running
- Network access to the Hercules host's FTP port, normally `2121` for the configuration above

The app uses only Python's standard library; there is no `pip install` step.

## Start

From this folder, run:

```sh
python3 tk5_file_transfer.py
```

Run the tests with:

```sh
python3 -m unittest discover -s tests -v
```

In the connection bar, enter the Hercules host, port `2121`, and your RAKF username and password. The default host is `localhost`, which is correct only when Hercules is running on the same computer. The remote pane opens at the FTP account's starting location; navigate to the desired data set or PDS.

For an unmodified TK5 demo account, select **Use TK5 demo credentials** to prefill `HERC01` / `CUL8TR`. This is opt-in and session-only; the password is never saved by the app. Do not use it if you changed the demo password.

Choose **Open current folder in VS Code** in the host pane to open the directory currently shown there. No item selection is needed. This requires the `code` or `code-insiders` command to be available on `PATH`.

## Transfer files

The **Quick Sync** tab stores host-file-to-PDS-member mappings for repeated source updates. Add a host file, choose its target PDS and member, then use **Push selected** or **Push changed files**. **Push selected** reapplies the host copy even if its hash is unchanged; **Push changed files** skips unchanged local content. Existing remote members always require overwrite confirmation. Mapping metadata is saved under the user's application config directory; it contains paths, member names, and hashes, not FTP credentials.

- Select one or more host entries and choose **Upload selected**.
- Select one or more remote entries and choose **Download selected**. Downloads go into the host folder shown in the left pane.
- Double-click a folder to enter it. Use **Up** to move to its parent; edit the path and choose **Go** to navigate directly.
- In the MVS pane, **Find name** filters the current listing as you type. For example, `.PROD.` matches data set names containing that qualifier regardless of their HLQ. Choose **Date updated** to sort newest first; the dates and sort precision are limited to what FTPD reports in `LIST`.
- In **Quick Sync**, choose **Add mapping…** to map one host source file to a PDS path and member name. The mappings and last-pushed file hashes are saved in your user config directory; credentials are never saved there. **Push selected** forces the host copy; **Push changed files** skips unchanged local files. Existing remote members still require overwrite confirmation.
- Quick Sync is host-to-mainframe only: the host file is authoritative. To include a file downloaded from MVS, add a mapping for its host copy and the corresponding PDS member.
- **Upload folder…** uploads the chosen folder and its contents beneath the current remote location. For MVS source PDS work, uploading individual members is usually more appropriate than uploading a host folder.
- When uploading a file into a PDS, the app uses the host file's stem as the member name, so `HELLO.cbl` becomes member `HELLO`. **Name override** can replace that name for a single file. When downloading a member, enter `HELLO.cbl` in the override to retain the host extension. Member names must be 1-8 MVS characters without a period.
- **Create PDS** submits a generated allocation job through FTPD's `AAINTRDR`, creating a cataloged FB/80 PDS on `TSO001`. This internal-reader job runs as RAKF `PROD`/`PRDGROUP`, not as the FTP user. RAKF must grant `PRDGROUP` `ALTER` access to the requested data set; otherwise JES reports `RAKF0005`/`RAKF000A` and the job fails. After the job completes, choose **Refresh catalog**. New sequential data sets still need to be allocated from MVS.
- Existing files at the destination always trigger an explicit confirmation before replacement. Declining skips that file. Directory uploads merge into an existing remote folder; they do not remove extra remote files.
- Use **Text / source code** for COBOL, assembler, JCL, and other text; use **Binary** for opaque binary files. FTPD performs the EBCDIC/ASCII conversion for text transfers. If a text download contains binary control bytes, the app offers to retry that member in binary mode or skip it; binary data may not be readable as source text.
- **New host file/folder** creates items in the currently shown host folder. **New member** creates an empty member in the current PDS, with overwrite confirmation if it already exists. Member names must be 1-8 MVS characters with no extension.
- A `Not opened 13` error means FTPD could not open the requested MVS target. Verify that you are inside an existing writable PDS, that the member name is valid, and that your RAKF identity has update access. Use **Create PDS** for a new PDS; sequential datasets must be allocated from MVS before transfer.

For example, to permit only the internal-reader job to allocate `TEST.PROD.SOURCE`, a RAKF administrator can define a specific DATASET profile and grant `PRDGROUP` ALTER:

```text
ADDSD 'TEST.PROD.SOURCE' UACC(NONE)
PERMIT 'TEST.PROD.SOURCE' CLASS(DATASET) ID(PRODGROUP) ACCESS(ALTER)
```

Use a narrower or different profile appropriate to your security policy, then reload the RAKF profiles. Do not grant `PRDGROUP` universal ALTER access just to make PDS creation work.

Host-to-TK5 uploads never silently replace a remote file. Downloads also ask before replacing a host file. Transfers are serialized so the FTP session is not used concurrently.

## Test sample

The `tests/samples/HELLO.cbl` file is a small fixed-format COBOL source sample. Select it in the host pane and upload while positioned in a target PDS; it will be stored as member `HELLO`. The member can then be used by MVS compile JCL. To bring it back under its original host filename, select the member, enter `HELLO.cbl` in **Name override**, and download.

## Network note

FTP sends credentials and file contents without encryption. Prefer `localhost` or a trusted isolated network. Do not expose the FTP port to the public internet.