import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("after_scan", Path(__file__).parents[1] / "identify_after_scan.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Stash:
    def __init__(self):
        self.jobs = {"1": {"status": "RUNNING"}, "2": {"status": "RUNNING"}}
        self.messages = []
        self.scenes = [{"id": "10", "stash_ids": [], "files": [{"path": "/media/new.mp4"}]}]
        self.sources = [{"source": {"stash_box_endpoint": "https://stashdb.org/graphql"}}]
        self.submitted = []
        self.lose_response = False

    def Log(self, message):
        self.messages.append(message)

    Warn = Log

    def find_job(self, job_id):
        return self.jobs.get(str(job_id))

    def find_scenes(self, **kwargs):
        return self.scenes

    def get_configuration_defaults(self, fragment):
        return {"identify": {"sources": self.sources, "options": {"skipMultipleMatches": True}}}

    def call_GQL(self, query, variables):
        self.submitted.append(variables["input"])
        if self.lose_response:
            raise TimeoutError("lost response")
        return {"metadataIdentify": "2"}


def pipeline(tmp_path, stash):
    return module.IdentifyAfterScan(stash, tmp_path / "state.json", enabled=True)


def test_wait_for_scan_then_identify_only_exact_unmatched_scenes(tmp_path):
    stash = Stash()
    stash.scenes += [
        {"id": "11", "stash_ids": [{"endpoint": "https://stashdb.org/graphql", "stash_id": "known"}], "files": [{"path": "/media/new.mp4"}]},
        {"id": "12", "stash_ids": [], "files": [{"path": "/media/unrelated.mp4"}]},
    ]
    worker = pipeline(tmp_path, stash)
    worker.track("1", ["/media/new.mp4"])
    worker.tick()
    assert not stash.submitted
    stash.jobs["1"] = {"status": "FINISHED"}
    worker.tick()
    assert stash.submitted == [{"sources": stash.sources, "options": {"skipMultipleMatches": True}, "sceneIDs": ["10"]}]
    worker.tick()
    assert len(stash.submitted) == 1


@pytest.mark.parametrize("job", [None, {"status": "FAILED"}, {"status": "CANCELLED"}, {"status": "FINISHED", "error": "scan failed"}])
def test_failed_or_unknown_scans_do_not_identify(tmp_path, job):
    stash = Stash()
    stash.jobs["1"] = job
    worker = pipeline(tmp_path, stash)
    worker.track("1", ["/media/new.mp4"])
    worker.tick()
    assert not stash.submitted


def test_restart_resumes_known_identify_job_without_repeating(tmp_path):
    stash = Stash()
    stash.jobs["1"] = {"status": "FINISHED"}
    worker = pipeline(tmp_path, stash)
    worker.track("1", ["/media/new.mp4"])
    worker.tick()
    restarted = pipeline(tmp_path, stash)
    restarted.tick()
    assert len(stash.submitted) == 1
    stash.jobs["2"] = {"status": "FINISHED"}
    restarted.tick()
    assert restarted.jobs["1"]["state"] == "DONE"


def test_unknown_submission_outcome_is_not_repeated(tmp_path):
    stash = Stash()
    stash.jobs["1"] = {"status": "FINISHED"}
    stash.lose_response = True
    worker = pipeline(tmp_path, stash)
    worker.track("1", ["/media/new.mp4"])
    worker.tick()
    pipeline(tmp_path, stash).tick()
    assert len(stash.submitted) == 1
    assert worker.jobs["1"]["state"] == "IDENTIFY_SUBMITTING"


def test_missing_identify_sources_require_configuration(tmp_path):
    stash = Stash()
    stash.jobs["1"] = {"status": "FINISHED"}
    stash.sources = []
    worker = pipeline(tmp_path, stash)
    worker.track("1", ["/media/new.mp4"])
    worker.tick()
    assert worker.jobs["1"]["state"] == "NEEDS_CONFIGURATION"
    assert not stash.submitted


def test_disabled_feature_does_not_write_or_submit(tmp_path):
    stash = Stash()
    worker = module.IdentifyAfterScan(stash, tmp_path / "state.json")
    worker.track("1", ["/media/new.mp4"])
    worker.tick()
    assert not stash.submitted and not (tmp_path / "state.json").exists()


def test_files_must_settle_before_targeted_scan(tmp_path):
    target = tmp_path / "new.mp4"
    target.write_bytes(b"one")
    queue = module.MediaTargets(["mp4"], settle_seconds=20)
    path = str(target)
    assert queue.ready([path, path], now=0) == ([], [path])
    assert queue.ready([path], now=19) == ([], [path])
    target.write_bytes(b"larger-file")
    assert queue.ready([path], now=20) == ([], [path])
    assert queue.ready([path], now=40) == ([path], [])
    assert not queue.accepts(path, is_directory=True)
    assert not queue.accepts(path + ".partial")


def test_directory_cannot_become_library_identify_scope(tmp_path):
    stash = Stash()
    stash.jobs["1"] = {"status": "FINISHED"}
    worker = pipeline(tmp_path, stash)
    worker.track("1", ["/media"])
    worker.tick()
    assert not stash.submitted


def test_runtime_wiring_preserves_pending_paths_and_does_not_expire_active_jobs():
    source = (Path(__file__).parents[1] / "filemonitor.py").read_text()
    assert 'TmpTargetPaths.append(os.path.dirname(TargetPath))' not in source
    assert 'pipeline.track(lastScanJob' in source
    assert 'pipeline.tick()' in source
    condition = next(line for line in source.splitlines() if "if lastScanJob['lastStatus'] == None" in line)
    assert 'or elapsedTime > MAX_SECONDS_WAIT_SCANJOB_COMPLETE' not in condition
    assert 'TmpTargetPaths + lastScanJob[\'DelayedProcessTargetPaths\']' in source


def test_repeated_scan_does_not_duplicate_pending_identify(tmp_path):
    stash = Stash()
    stash.jobs["1"] = {"status": "FINISHED"}
    stash.jobs["3"] = {"status": "FINISHED"}
    worker = pipeline(tmp_path, stash)
    worker.track("1", ["/media/new.mp4"])
    worker.track("3", ["/media/new.mp4"])
    worker.tick()
    assert len(stash.submitted) == 1


def test_host_upgrade_preserves_config_and_rejects_running_monitor(tmp_path):
    import fcntl
    installer_spec = importlib.util.spec_from_file_location("upgrade", Path(__file__).parents[1] / "upgrade_host.py")
    installer = importlib.util.module_from_spec(installer_spec)
    installer_spec.loader.exec_module(installer)
    app = tmp_path / "app"
    app.mkdir()
    (app / "filemonitor.py").write_text("# original\n")
    (app / "filemonitor_config.py").write_text("config = {'custom': 'preserve'}\n")
    wrapper = tmp_path / "run_monitor.py"
    wrapper.write_text("# existing authentication wrapper\n")
    with (tmp_path / "monitor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="Stop the running"):
            installer.upgrade(tmp_path, True)
    installer.upgrade(tmp_path, True)
    assert wrapper.read_text() == "# existing authentication wrapper\n"
    assert "'custom': 'preserve'" in (app / "filemonitor_config.py").read_text()
    assert "'identifyAfterScan': True" in (app / "filemonitor_config.py").read_text()
    assert (app / "identify_after_scan.py").is_file()
    assert list((tmp_path / "backups").glob("*/filemonitor.py"))[0].read_text() == "# original\n"
