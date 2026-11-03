# Live test on the 3DEXPERIENCE laptop

About 30 minutes. Everything runs as the normal user (no admin). Branch under
test: `claude/background-service-catia-c43f2e`.

## 0. Before you start

- Start 3DEXPERIENCE (CATIA) and log in. Leave it open the whole time.
- Quit Claude Desktop completely (right-click its tray icon > Quit). The
  installer edits its config, and Claude Desktop only reads it on start.

## 1. Install (one paste)

Open **Windows PowerShell** from the Start menu (not as administrator), paste
this as one line, press Enter:

```powershell
powershell -ExecutionPolicy Bypass -c "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/friessssss/catia-v5-mcp-server/claude/background-service-catia-c43f2e/scripts/install.ps1))) -Branch claude/background-service-catia-c43f2e"
```

Expected at the end (takes 1-3 minutes the first time):

- `Service running: http://127.0.0.1:8765/mcp`
- `'catia' configured in ...claude_desktop_config.json`
- a status block with `Commit: <12 characters>`

Write down the Commit value; it must match the commit the developer said
was pushed. If the window shows a red error instead, take a screenshot of the
whole window and stop here.

## 2. Service check

In the same PowerShell window:

```powershell
catia-mcp status
```

Expected: `Service: running`, `Autostart: on`. (If `catia-mcp` is not
found, close PowerShell, open a new one and retry.)

## 3. Automated smoke test (through the service)

Paste these two commands one at a time (Enter after each; never on one line):

```powershell
irm https://raw.githubusercontent.com/friessssss/catia-v5-mcp-server/claude/background-service-catia-c43f2e/scripts/live_smoke_test.py -OutFile $env:TEMP\live_smoke_test.py
```

```powershell
& "$env:APPDATA\uv\tools\catia-v5-mcp-server\Scripts\python.exe" $env:TEMP\live_smoke_test.py --url http://127.0.0.1:8765/mcp --drawing --report "$env:USERPROFILE\Desktop\smoke_report.json"
```

CATIA will build a test block, holes, patterns and a drawing on its own;
don't click in CATIA while it runs. At the end it prints a PASS/FAIL table
and writes `smoke_report.json` to the Desktop. For rows marked **EYES**,
look at CATIA and jot down whether it did what the row says (view turned to
the front, undo removed the point, and so on).

## 4. Claude Desktop

Start Claude Desktop. In a new chat, click the tools/connectors icon: a
`catia` server with about 130 tools should be listed and enabled. Then send
these prompts one by one and note what happens:

1. `Use catia_status and tell me what CATIA has open.`
2. `Create a new part, make a 100 x 60 x 40 mm block, drill a 10 mm hole through its center, then show me an isometric screenshot.`
   The picture should appear in the chat.
3. `Show me the front view, then the top view.` In CATIA, compare with the
   Front and Top buttons of CATIA's own view toolbar: same orientation?
4. `Undo the last feature and show me a screenshot.`
5. Click a face of the block in CATIA, then: `What do I have selected?`
6. `Search the CATIA API documentation for AddNewHoleFromSketch.`
   (The first search unpacks the help file and can take a minute.)
7. `Using catia_run_python, list every body and feature in the part.`

## 5. Failure handling

1. In CATIA open **Me > Preferences** (or any dialog that stays open), and
   while it is open ask Claude: `Add a 5 mm fillet to all edges.` Wait up to
   2 minutes. Note what Claude reports (expected: a CATIA_BUSY or
   CATIA_TIMEOUT message naming the dialog). Close the dialog.
2. Close CATIA completely. Ask Claude: `What is CATIA's status?` Expected: it
   says CATIA is not running, and **no new CATIA window opens**.
3. Start CATIA again and log in. Ask the same question: it should reconnect
   by itself.

## 6. Restart check

Sign out of Windows and back in (or reboot). Without running anything,
open PowerShell and run `catia-mcp status`: expected `Service: running`.

## 7. Send back

- `smoke_report.json` from the Desktop
- your notes from steps 3-6 (one line per item is enough)
- these two logs:
  - `%LOCALAPPDATA%\catia-mcp\catia_mcp.log`
  - `%LOCALAPPDATA%\catia-mcp\proxy.log` (if present)
- screenshots of anything that looked wrong

(Paste `%LOCALAPPDATA%\catia-mcp` into the File Explorer address bar to open
the log folder.)

## Round 2 checks (after the 2026-11-02 field report)

Update first (`catia-mcp update`, or the install command at the top for the
very first time), check `catia-mcp status` shows the new commit.

**A. Probe (2 minutes, creates nothing).** Open a 3D Part you made in
the UI in CATIA, then paste one line at a time:

```powershell
irm https://raw.githubusercontent.com/friessssss/catia-v5-mcp-server/claude/background-service-catia-c43f2e/scripts/probe_3dx.py -OutFile $env:TEMP\probe_3dx.py
```

```powershell
& "$env:APPDATA\uv\tools\catia-v5-mcp-server\Scripts\python.exe" $env:TEMP\probe_3dx.py
```

Send back `probe_3dx.json` from the Desktop.

**B. In Claude Desktop, with that 3D Part still open:**

1. `Create a new 3D Part called probe part 2.` Note whether a 3D Part (not
   a 3D Shape) appears and how many licence dialogs pop up.
2. `Pad a 100 x 100 x 60 block, list its edges, then fillet all edges R5.`
   Expect 12 edges and one fillet, no error.
3. `Drill an M6 tapped hole through the centre (metric coarse).` Expect a
   Ø4.917 tap drill, M6, through.
4. `Make a drawing on A1, third angle, with top, front and section views and
   generate dimensions on the top view.` Note dimensions landing in other
   views and any 120° / Ø4.917 / duplicate 60 that survive.
5. `Apply <your company drawing template> and link the PART NUMBER cell to
   the part's EIN.`
6. `Assign an EIN to probe part 2.` Note what happens (number, dialog, or a
   message to use the UI).
7. Safety: with the template window in front, `Add a text "test" to this
   drawing.` Expected: refused with WRITE_PROTECTED (owned by another user).
8. `Preview saving probe part 2.` Expected: a description, nothing saved.

## Updating to a newer version

Once version 0.3.0 or later is installed, updating is one command:

```powershell
catia-mcp update
```

It opens a new PowerShell window that installs the newest code from the
same branch and restarts the service. If Claude Desktop is open it is closed
during the update and reopened automatically when the window says **Done**
(chats are kept; finish or stop any reply in progress first). Then run
`catia-mcp status` and check the `Commit:` line. `catia-mcp update --check` only reports whether a
newer commit exists.

## Undo everything

```powershell
powershell -ExecutionPolicy Bypass -c "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/friessssss/catia-v5-mcp-server/claude/background-service-catia-c43f2e/scripts/install.ps1))) -Uninstall"
```
