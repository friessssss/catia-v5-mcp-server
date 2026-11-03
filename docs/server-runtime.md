# Serialized CATIA execution

MCP requests enqueue work on one persistent STA thread. Connection initialization,
tool execution and connection cleanup use that same thread, for CATIA V5 and
CATIA 3DEXPERIENCE alike (the platform is detected on connect).

Server-boundary failures return an `ok/code/operation_id/tool/message/data/effects/
diagnostics/warnings/recovery` envelope as both structured content and indented
JSON text, with MCP `isError=true`. Input validation failures do not access COM.
Missing Automation methods retain the guidance about release availability,
pywin32 cache and workbench licences in `message` (on 3DEXPERIENCE it also
points to `catia_inspect_api` and DSYAutomation.chm). Tools raise typed
failures where they can classify them (`INVALID_ARGUMENT`,
`NO_ACTIVE_DOCUMENT`, `WRONG_DOCUMENT_TYPE`, `OBJECT_NOT_FOUND`,
`AMBIGUOUS_TARGET`, `UNSUPPORTED_CAPABILITY`, `PERMISSION_DENIED`);
`effects.modified=false` marks failures detected before CATIA was touched. Exceptions include their
message and traceback in `catia_mcp.log`.

An ordinary error affects one call. Correct the input or inspect CATIA, then call
again; it does not require restarting the server. A cancelled client request does
not stop an already queued or executing COM operation. The worker completes it
before starting another call. Cancellation does not establish model rollback;
inspect the model before repeating a write.

There is no global unknown-session latch in this runtime change. It does not
introduce CAA tools, binaries, dependencies or native delivery-state handling.
CAA-specific indeterminate writes and recovery belong to the separate adapter.

Calls are bounded on the client side. A call still running after
`CATIA_MCP_CALL_TIMEOUT` seconds (default 600) returns `CATIA_TIMEOUT`
(`effects.modified` unknown). While the worker stays inside one call for
longer than `CATIA_MCP_BUSY_GRACE` seconds (default 30), new calls are not
queued behind it: they fail at once with `CATIA_BUSY` and
`effects.modified=false`, naming the stuck tool and how long it has run. Both
messages list the titles of open CATIA dialog windows when the server can find
them (window enumeration only, no COM call, so it works while CATIA is
blocked). `/health` and `catia-mcp status` show the busy call too.

There is still no COM-call cancellation: a timed-out call keeps running inside
CATIA and may yet complete. Close the dialog in CATIA and the worker carries
on; inspect the model before repeating a write. If CATIA cannot recover, close
it; the next call returns `CATIA_NOT_RUNNING` and the server reattaches once
CATIA is back, without a restart.

Install and run the offline regressions:

```sh
python -m pip install -e ".[dev]"
python -m pytest tests test_server.py -q
```

Windows CI runs this command on Python 3.10, 3.12 and 3.13. Local tests use COM doubles
for thread ownership, serialization, errors and cancellation; they do not prove
live CATIA method behavior.

The always-on service, its proxy and its configuration are described in
[background-service.md](background-service.md).
