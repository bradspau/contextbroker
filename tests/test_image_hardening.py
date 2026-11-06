"""The API image runs as a non-root user and its build context carries no
planning, tracker, operator-script or key material (ACC-05)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _lines(name):
    out = []
    for raw in (ROOT / name).read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _instructions():
    """(keyword, text) per Dockerfile instruction, with backslash continuation lines joined."""
    out, pending = [], ""
    for line in _lines("Dockerfile"):
        pending = f"{pending} {line}".strip() if pending else line
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        out.append((pending.split(None, 1)[0].upper(), pending))
        pending = ""
    return out


def test_dockerfile_has_exactly_one_user_instruction():
    users = [l for kw, l in _instructions() if kw == "USER"]
    assert len(users) == 1, users


def test_user_is_not_root():
    (user_line,) = [l for kw, l in _instructions() if kw == "USER"]
    value = user_line.split(None, 1)[1].strip()
    assert value not in ("root", "0", "0:0", "root:root")
    assert value.split(":")[0] not in ("root", "0")


def test_user_comes_after_the_last_copy_and_before_cmd():
    kws = [kw for kw, _ in _instructions()]
    user_at = kws.index("USER")
    last_copy = max(i for i, kw in enumerate(kws) if kw == "COPY")
    assert last_copy < user_at < kws.index("CMD")


def test_cmd_is_unchanged():
    cmd = [l for kw, l in _instructions() if kw == "CMD"]
    assert cmd == ['CMD ["uvicorn", "broker_engine.main:app", "--host", "0.0.0.0", "--port", "8080"]']


def test_dockerignore_excludes_planning_tracker_operator_and_key_material():
    lines = _lines(".dockerignore")
    for entry in (".beads/", ".planning/", ".claude/", "design/", "scripts/", "CLAUDE.md",
                  "AGENTS.md", "planning.md", "*.pem", "*.key", ".env"):
        assert entry in lines, entry


def test_dockerignore_keeps_the_licensed_patterns():
    lines = _lines(".dockerignore")
    for entry in ("ontologies/licensed", "**/ontology/**/*.ttl", "ontologies/tbox/*.ttl",
                  "!ontologies/tbox/topology.ttl", "ontologies/**/*.zip"):
        assert entry in lines, entry


def test_data_dir_is_created_and_owned_by_the_container_user_before_user():
    # A new named volume copies the ownership of the image directory it covers, so
    # the non-root user can write its state file only if /data exists and is owned
    # by it in the image (Pitfall 3).
    insts = _instructions()
    kws = [kw for kw, _ in insts]
    user_at = kws.index("USER")
    last_copy = max(i for i, kw in enumerate(kws) if kw == "COPY")
    (user_line,) = [l for kw, l in insts if kw == "USER"]
    uid_gid = user_line.split(None, 1)[1].strip()
    runs = [(i, l) for i, (kw, l) in enumerate(insts)
            if kw == "RUN" and last_copy < i < user_at and "mkdir -p /data" in l]
    assert len(runs) == 1, runs
    assert "chown {} /data".format(uid_gid) in runs[0][1]
    assert uid_gid == "10001:10001"


def test_models_are_downloaded_by_commit_at_build_time_and_the_image_runs_offline():
    # The two embedding models enter the image once, by commit, and the running container never
    # fetches one (D-36). The revisions and directories are the ones the embedder loads (config).
    from okf_retrieval import config

    insts = _instructions()
    kws = [kw for kw, _ in insts]
    pip_at = next(i for i, (kw, l) in enumerate(insts) if kw == "RUN" and "pip install" in l)
    first_copy_after_pip = next(i for i, kw in enumerate(kws) if kw == "COPY" and i > pip_at)
    runs = [(i, l) for i, (kw, l) in enumerate(insts) if kw == "RUN" and "snapshot_download" in l]
    assert len(runs) == 1, runs
    at, run = runs[0]
    assert pip_at < at < first_copy_after_pip
    for literal in (config.DENSE_SOURCE, config.DENSE_REVISION, config.SPARSE_MODEL, config.SPARSE_REVISION,
                    f"{config.DEFAULT_MODEL_DIR}/{config.DENSE_SUBDIR}",
                    f"{config.DEFAULT_MODEL_DIR}/{config.SPARSE_SUBDIR}"):
        assert run.count(literal) == 1, literal
    assert "chmod -R a+rX " + config.DEFAULT_MODEL_DIR in run
    offline = [i for i, (kw, l) in enumerate(insts) if kw == "ENV" and "HF_HUB_OFFLINE=1" in l]
    assert len(offline) == 1
    assert at < offline[0] < kws.index("USER")


def test_model_revisions_are_full_commit_hashes():
    from okf_retrieval import config

    for revision in (config.DENSE_REVISION, config.SPARSE_REVISION):
        assert len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)
