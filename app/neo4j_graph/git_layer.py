"""Build the git/VCS layer of the graph for one repository.

Nodes:  Repo, Author, Branch, Commit, Directory, File
Edges:  IN_REPO, AUTHORED_BY, PARENT, HEAD, TOUCHES, CONTAINS

Returns the set of currently-tracked files so the code layer can parse them
without re-walking the tree.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from .config import GraphBuildConfig, repo_local_path
from .writer import Neo4jWriter

log = logging.getLogger(__name__)

_US = "\x1f"  # field separator
_RS = "\x1e"  # record separator


def _git(path: str, *args: str, timeout: int = 120) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, check=False,
        capture_output=True, text=True, timeout=timeout, errors="replace",
    )
    return result.stdout if result.returncode == 0 else ""


def _tracked_files(repo_path: str, cfg: GraphBuildConfig) -> list[str]:
    out = _git(repo_path, "ls-files")
    files: list[str] = []
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        if any(p in cfg.skip_dirs for p in PurePosixPath(rel).parts):
            continue
        files.append(rel)
    return files


def build_git_layer(writer: Neo4jWriter, repo: dict[str, Any], cfg: GraphBuildConfig) -> list[str]:
    name = repo["name"]
    repo_path = repo_local_path(repo)
    log.info("[%s] git layer", name)

    writer.write_nodes("Repo", [{
        "name": name,
        "remote_url": repo.get("remote_url", ""),
        "default_branch": repo.get("branch", ""),
        "current_commit": repo.get("current_commit", ""),
        "activity_score": repo.get("activity_score", 0),
        "last_commit_days": repo.get("last_commit_days"),
        "commits_30d": repo.get("commits_30d", 0),
        "commits_90d": repo.get("commits_90d", 0),
        "authors_90d": repo.get("authors_90d", 0),
    }])

    tracked = _tracked_files(repo_path, cfg)
    file_nodes, dir_nodes = [], {}
    contains_rels, file_in_repo = [], []

    def dir_uid(d: str) -> str:
        return f"{name}:{d}"

    for rel in tracked:
        p = PurePosixPath(rel)
        file_uid = f"{name}:{rel}"
        file_nodes.append({
            "uid": file_uid, "repo": name, "path": rel,
            "name": p.name, "ext": p.suffix.lower(),
        })
        file_in_repo.append({"start": file_uid, "end": name})

        chain = list(p.parent.parts)
        child_uid = file_uid
        child_is_file = True
        for depth in range(len(chain), -1, -1):
            d = "/".join(chain[:depth])
            d_uid = dir_uid(d)
            if d_uid not in dir_nodes:
                dir_nodes[d_uid] = {
                    "uid": d_uid, "repo": name, "path": d,
                    "name": chain[depth - 1] if depth > 0 else name,
                }
            contains_rels.append({"start": d_uid, "end": child_uid, "_child_is_file": child_is_file})
            child_uid = d_uid
            child_is_file = False
        contains_rels.append({"start": name, "end": dir_uid(""), "_repo_root": True})

    writer.write_nodes("File", file_nodes)
    writer.write_nodes("Directory", list(dir_nodes.values()))
    writer.write_rels("IN_REPO", "File", "Repo", file_in_repo)

    dir_to_file, dir_to_dir, repo_to_dir = {}, {}, {}
    for r in contains_rels:
        key = (r["start"], r["end"])
        if r.get("_repo_root"):
            repo_to_dir[key] = {"start": r["start"], "end": r["end"]}
        elif r.get("_child_is_file"):
            dir_to_file[key] = {"start": r["start"], "end": r["end"]}
        else:
            dir_to_dir[key] = {"start": r["start"], "end": r["end"]}
    writer.write_rels("CONTAINS", "Repo", "Directory", list(repo_to_dir.values()))
    writer.write_rels("CONTAINS", "Directory", "Directory", list(dir_to_dir.values()))
    writer.write_rels("CONTAINS", "Directory", "File", list(dir_to_file.values()))

    _build_commits(writer, name, repo_path, cfg, set(tracked))
    _build_branches(writer, name, repo_path)
    return tracked


def _build_commits(writer, name, repo_path, cfg, tracked_set) -> None:
    limit = ["-n", str(cfg.max_commits_per_repo)] if cfg.max_commits_per_repo else []
    fmt = f"{_RS}%H{_US}%P{_US}%an{_US}%ae{_US}%at{_US}%ct{_US}%s"
    raw = _git(repo_path, "log", "--all", *limit, f"--pretty=format:{fmt}", "--name-only")
    if not raw:
        return

    commit_nodes, authors = [], {}
    in_repo, authored_by, parent_rels, touches = [], [], [], []
    for record in raw.split(_RS):
        record = record.strip("\n")
        if not record:
            continue
        header, _, body = record.partition("\n")
        fields = header.split(_US)
        if len(fields) < 7:
            continue
        chash, parents, an, ae, at, ct, subject = fields[:7]
        cuid = f"{name}@{chash}"
        commit_nodes.append({
            "uid": cuid, "hash": chash, "short": chash[:10], "repo": name,
            "message": subject[:500],
            "authored_at": int(at) if at.isdigit() else None,
            "committed_at": int(ct) if ct.isdigit() else None,
            "author_name": an, "author_email": ae,
        })
        in_repo.append({"start": cuid, "end": name})
        if ae:
            authors.setdefault(ae, {"email": ae, "name": an})
            authored_by.append({"start": cuid, "end": ae})
        for ph in parents.split():
            parent_rels.append({"start": cuid, "end": f"{name}@{ph}"})
        for line in body.splitlines():
            line = line.strip()
            if line and line in tracked_set:
                touches.append({"start": cuid, "end": f"{name}:{line}"})

    writer.write_nodes("Commit", commit_nodes)
    writer.write_nodes("Author", list(authors.values()))
    writer.write_rels("IN_REPO", "Commit", "Repo", in_repo)
    writer.write_rels("AUTHORED_BY", "Commit", "Author", authored_by)
    writer.write_rels("PARENT", "Commit", "Commit", parent_rels)
    writer.write_rels("TOUCHES", "Commit", "File", touches)
    log.info("[%s] %d commits, %d authors, %d touch-edges",
             name, len(commit_nodes), len(authors), len(touches))


def _build_branches(writer, name, repo_path) -> None:
    raw = _git(repo_path, "for-each-ref",
               f"--format=%(refname:short){_US}%(objectname)", "refs/heads")
    branch_nodes, in_repo, head = [], [], []
    for line in raw.splitlines():
        bname, _, obj = line.partition(_US)
        if not bname:
            continue
        buid = f"{name}@{bname}"
        branch_nodes.append({"uid": buid, "name": bname, "repo": name})
        in_repo.append({"start": buid, "end": name})
        if obj:
            head.append({"start": buid, "end": f"{name}@{obj}"})
    writer.write_nodes("Branch", branch_nodes)
    writer.write_rels("IN_REPO", "Branch", "Repo", in_repo)
    writer.write_rels("HEAD", "Branch", "Commit", head)
