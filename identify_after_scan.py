"""Durable, opt-in Identify jobs after confirmed successful Stash scans."""

import json
import os
import time
from pathlib import Path

IDENTIFY_OPTIONS = """fieldOptions { field strategy createMissing }
setCoverImage setOrganized includeMalePerformers performerGenders
skipMultipleMatches skipMultipleMatchTag skipSingleNamePerformers skipSingleNamePerformerTag"""


class MediaTargets:
    """Keep events scoped to media files and wait while their stat signature changes."""
    def __init__(self, extensions, settle_seconds=20):
        self.extensions = {str(value).lower().lstrip(".") for value in extensions}
        self.settle_seconds = settle_seconds
        self.observed = {}

    def accepts(self, path, is_directory=False):
        return not is_directory and Path(path).suffix.lower().lstrip(".") in self.extensions

    def ready(self, paths, now=None):
        now = time.monotonic() if now is None else now
        ready, deferred = [], []
        for path in dict.fromkeys(paths):
            try:
                info = Path(path).stat()
            except FileNotFoundError:
                self.observed.pop(path, None)
                continue
            if not self.accepts(path) or not Path(path).is_file() or info.st_size == 0:
                continue
            signature = (info.st_size, info.st_mtime_ns)
            previous = self.observed.get(path)
            if not previous or previous[0] != signature:
                self.observed[path] = (signature, now)
                deferred.append(path)
            elif now - previous[1] < self.settle_seconds:
                deferred.append(path)
            else:
                ready.append(path)
        return ready, deferred


class IdentifyAfterScan:
    def __init__(self, stash, state_file, enabled=False):
        self.stash = stash
        self.enabled = enabled
        self.path = Path(state_file)
        self.jobs = {}
        if enabled and self.path.exists():
            self.jobs = json.loads(self.path.read_text())

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as handle:
            json.dump(self.jobs, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def track(self, job_id, paths):
        if not self.enabled:
            return
        self.jobs[str(job_id)] = {"state": "SCAN", "paths": list(paths), "id": str(job_id)}
        self.save()
        self.stash.Log(f"[IdentifyAfterScan] Tracking scan {job_id} for {len(paths)} path(s)")

    def tick(self):
        if not self.enabled:
            return
        for record in self.jobs.values():
            if record["state"] not in ("SCAN", "IDENTIFY"):
                continue
            try:
                job = self.stash.find_job(record["id"])
                if not job:
                    record["state"] = "UNKNOWN"
                    self.save()
                    self.stash.Warn(f"[IdentifyAfterScan] Job {record['id']} is unavailable; not assuming success")
                    continue
                status = job.get("status")
                if status in ("READY", "RUNNING", "STOPPING"):
                    continue
                if status != "FINISHED" or job.get("error"):
                    record["state"] = "FAILED"
                    self.save()
                    self.stash.Warn(f"[IdentifyAfterScan] Job {record['id']} did not finish successfully ({status})")
                    continue
                if record["state"] == "IDENTIFY":
                    record["state"] = "DONE"
                    self.save()
                    self.stash.Log(f"[IdentifyAfterScan] Identify job {record['id']} finished")
                    continue
                self.identify(record)
            except Exception as exc:
                # Transient reads can retry. A submission marker is never retried
                # automatically if a response was lost after a server accepted it.
                self.stash.Warn(f"[IdentifyAfterScan] {type(exc).__name__}: {exc}")

    def identify(self, record):
        ids = set()
        for path in record["paths"]:
            scenes = self.stash.find_scenes(
                f={"path": {"value": path, "modifier": "EQUALS"}},
                filter={"per_page": -1},
                fragment="id stash_ids { endpoint stash_id } files { path }",
            )
            for scene in scenes:
                # Confirm exact paths locally too; never identify an entire library
                # because a server-side path predicate has broader semantics.
                if not any(file.get("path") == path for file in scene.get("files", [])):
                    continue
                if any(sid.get("stash_id") and "stashdb.org" in sid.get("endpoint", "")
                       for sid in scene.get("stash_ids", [])):
                    continue
                ids.add(str(scene["id"]))
        pending_ids = {
            scene_id for other in self.jobs.values() if other is not record
            and other["state"] in ("IDENTIFY", "IDENTIFY_SUBMITTING", "UNKNOWN")
            for scene_id in other.get("scene_ids", [])
        }
        ids.difference_update(pending_ids)
        if not ids:
            record["state"] = "DONE"
            self.save()
            self.stash.Log("[IdentifyAfterScan] No unmatched scenes at the scanned file paths")
            return
        defaults = self.stash.get_configuration_defaults(
            "identify { sources { source { stash_box_endpoint stash_box_index scraper_id } "
            "options { " + IDENTIFY_OPTIONS + " } } options { " + IDENTIFY_OPTIONS + " } }"
        ).get("identify")
        if not defaults or not defaults.get("sources"):
            record["state"] = "NEEDS_CONFIGURATION"
            self.save()
            self.stash.Warn("[IdentifyAfterScan] Configure and save Stash Identify sources before enabling automatic Identify")
            return
        record["state"] = "IDENTIFY_SUBMITTING"
        record["scene_ids"] = sorted(ids)
        self.save()
        query = "mutation Identify($input: IdentifyMetadataInput!) { metadataIdentify(input: $input) }"
        result = self.stash.call_GQL(query, {"input": {
            "sources": defaults["sources"], "options": defaults.get("options"),
            "sceneIDs": sorted(ids),
        }})
        job_id = result.get("metadataIdentify")
        if not job_id:
            raise RuntimeError("Identify submission returned no job ID; inspect Stash before retrying")
        record["state"] = "IDENTIFY"
        record["id"] = str(job_id)
        self.save()
        self.stash.Log(f"[IdentifyAfterScan] Queued Identify job {job_id} for scene IDs {sorted(ids)}")
