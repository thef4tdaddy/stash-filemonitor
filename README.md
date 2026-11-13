# FileMonitor: custom host edition

Based on [stashapp/CommunityScripts FileMonitor](https://github.com/stashapp/CommunityScripts/tree/main/plugins/FileMonitor).
Upstream author: David Maisonave. Upstream notices and AGPL license are retained.
See [UPSTREAM_README.md](UPSTREAM_README.md) for original features and credits.

## What changed

* Host polling defaults to 15 seconds. Absolute Linux mounts and repeated path
  components map correctly, replacing only the leading prefix.
* New-file events scan that media file, not the entire parent import root. Empty
  directory creation does not launch a broad scan. Files directly in the import
  base folder work as well as nested files.
* Growing files wait until size and modification time have stayed unchanged for
  20 seconds. This is a settling check, not proof a paused download is complete;
  keep using completed-file staging outside watched folders.
* Pending paths survive a busy Stash scan queue. Known READY/RUNNING scans are
  not submitted again merely because 30 minutes passed. FAILED jobs are terminal.
* Optional Identify-after-scan runs only after a confirmed successful scan, only
  for scene IDs whose file paths exactly match the scanned files and which lack
  a StashDB ID. It uses the saved Stash Identify sources and field options.
* Identification runs when the monitor checks job status, even if no new files
  arrive. Its pending job IDs survive monitor restarts; ambiguous submission
  outcomes require review instead of a blind retry.
* Repeated scans do not queue duplicate Identify jobs for scene IDs already being
  identified. Ctrl+C cleans up the watcher and shared-memory marker.

## Upgrade an existing host installation

This remains a **host service**, not a Docker watcher. Installing a Stash plugin
package alone does not update the separate host copy. Stop the running host
monitor before applying an upgrade. Backups are created automatically.

Using the existing host Python environment, run from this downloaded checkout:

```bash
sudo /volume3/filemonitor-host/venv/bin/python upgrade_host.py \
  --host-dir /volume3/filemonitor-host --enable-identify-after-scan
```

The installer preserves the existing startup wrapper, authentication, virtual
environment, mount mapping, helper files, and user configuration. It replaces
`app/filemonitor.py`, adds `app/identify_after_scan.py`, and appends managed options
to `app/filemonitor_config.py`. It does not start the monitor or access media.

In Stash, first configure **Identify** with your desired StashDB source and save
its defaults. Disable another scheduler's Identify-after-scan task before enabling
this feature. The bundled FileMonitor Scheduler can remain off.

Restart using the existing host wrapper and console log. Look for
`identify-after-scan=True`, targeted scan paths, and `[IdentifyAfterScan]` messages.
Test one completed file before making the service start at boot. A saved StashDB
match triggers the separate Whisparr Sync plugin; Whisparr's existing notifications
continue to update Stash. No new return connection is added here.

## Limits and recovery

This is tested offline and needs a live host trial. A successful Identify job can
still find no match; it does not invent IDs or guarantee every file is identified.
Files already present at watcher startup are the baseline, not automatic events.
This does not identify the full existing library on startup.

Preserve `app/state/identify_after_scan.json`. FAILED, UNKNOWN,
IDENTIFY_SUBMITTING, and NEEDS_CONFIGURATION records require checking the
corresponding Stash job and settings before manual recovery. No automatic cleanup,
media deletion, or database reset is introduced. Existing upstream maintenance
features remain controlled by their separate settings.

This shortens the delay before Whisparr knows a completed file exists. It does
not prevent downloads already pending, or replace the autobrr/tunnel duplicate
check. The tunnel must also consider pending/in-progress downloads to cover the
window before identification finishes.

Run tests with `python -m pytest -q tests`. No live NAS data is needed.
