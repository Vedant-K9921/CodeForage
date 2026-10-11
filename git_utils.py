"""Every function here runs a real `git` command and returns plain Python data."""

import os
import re
import shutil
import subprocess
import tempfile

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def valid_name(name: str) -> bool:
    """Stops names like '../../etc' from escaping the repos folder."""
    return bool(NAME_RE.match(name)) and name not in (".", "..")


def run_git(args, cwd=None):
    """Run git safely (list of args, never a shell string). Returns stdout text."""
    result = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout


def repo_path(repos_dir, username, repo_name):
    return os.path.join(repos_dir, username, f"{repo_name}.git")


# ---------- create / delete ----------
def create_bare_repo(repos_dir, username, repo_name):
    path = repo_path(repos_dir, username, repo_name)
    os.makedirs(path, exist_ok=True)
    run_git(["init", "--bare", "--initial-branch=main", path])
    return path


def delete_repo(repos_dir, username, repo_name):
    shutil.rmtree(repo_path(repos_dir, username, repo_name), ignore_errors=True)


# ---------- read ----------
def is_repo_empty(path):
    try:
        return run_git(["rev-list", "-n", "1", "--all"], cwd=path).strip() == ""
    except subprocess.CalledProcessError:
        return True


def get_branches(path):
    try:
        return run_git(["branch", "--format=%(refname:short)"], cwd=path).split()
    except subprocess.CalledProcessError:
        return []


def get_tree(path, branch, subpath=""):
    """List files/folders. Returns [{'name','type','path'}] with folders first."""
    try:
        target = f"{branch}:{subpath}" if subpath else branch
        out = run_git(["ls-tree", target], cwd=path)
    except subprocess.CalledProcessError:
        return []
    items = []
    for line in out.splitlines():
        meta, name = line.split("\t", 1)
        kind = meta.split()[1]  # 'blob' (file) or 'tree' (folder)
        full = f"{subpath}/{name}" if subpath else name
        items.append({"name": name, "type": "dir" if kind == "tree" else "file", "path": full})
    return sorted(items, key=lambda i: (i["type"] != "dir", i["name"].lower()))


def get_file_content(path, branch, filepath):
    try:
        return run_git(["show", f"{branch}:{filepath}"], cwd=path)
    except (subprocess.CalledProcessError, UnicodeDecodeError):
        return None


def get_commits(path, branch, limit=50):
    try:
        out = run_git(["log", branch, "-n", str(limit), "--pretty=format:%H|%an|%ar|%s"], cwd=path)
    except subprocess.CalledProcessError:
        return []
    commits = []
    for line in out.splitlines():
        sha, author, when, message = line.split("|", 3)
        commits.append(
            {"sha": sha, "short": sha[:7], "author": author, "when": when, "message": message}
        )
    return commits


def parse_diff(patch):
    """Turn raw patch text into [{'text','kind'}] so CSS can colour each line."""
    lines = []
    for line in patch.splitlines():
        if (
            line.startswith("+++")
            or line.startswith("---")
            or line.startswith("diff ")
            or line.startswith("index ")
        ):
            kind = "meta"
        elif line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "del"
        else:
            kind = "ctx"
        lines.append({"text": line, "kind": kind})
    return lines


def get_commit_details(path, sha):
    try:
        meta = run_git(["show", sha, "-s", "--format=%an|%ad|%s"], cwd=path).strip()
        patch = run_git(["show", sha, "--pretty=format:", "--patch"], cwd=path)
    except subprocess.CalledProcessError:
        return None
    author, date, message = meta.split("|", 2)
    return {
        "sha": sha,
        "author": author,
        "date": date,
        "message": message,
        "diff": parse_diff(patch),
    }


def get_branch_diff(path, target, source):
    try:
        return parse_diff(run_git(["diff", f"{target}...{source}", "--patch"], cwd=path))
    except subprocess.CalledProcessError:
        return []


# ---------- write (clone to a temp folder, change, push back) ----------
def _identity(name, email):
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def add_file(path, branch, filename, content, message, author, email):
    """Create/overwrite one file and commit it. Works even on an empty repo."""
    tmp = tempfile.mkdtemp()
    try:
        abs_path = os.path.abspath(path)
        run_git(["clone", abs_path, "."], cwd=tmp)
        existing = get_branches(path)
        if branch in existing:
            run_git(["checkout", branch], cwd=tmp)
        else:
            run_git(["checkout", "-B", branch], cwd=tmp)
        target = os.path.normpath(os.path.join(tmp, filename))
        if not target.startswith(os.path.abspath(tmp)):
            raise ValueError("bad filename")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        run_git(["add", "-A"], cwd=tmp)
        run_git(_identity(author, email) + ["commit", "-m", message], cwd=tmp)
        run_git(["push", "origin", branch], cwd=tmp)
        return True
    except (subprocess.CalledProcessError, ValueError):
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def create_branch(path, new_branch, from_branch):
    try:
        run_git(["branch", new_branch, from_branch], cwd=path)
        return True
    except subprocess.CalledProcessError:
        return False


def _merge_in_temp(path, target, source, author, email, push):
    tmp = tempfile.mkdtemp()
    try:
        run_git(["clone", os.path.abspath(path), "."], cwd=tmp)
        run_git(["checkout", target], cwd=tmp)
        merge = subprocess.run(
            ["git"]
            + _identity(author, email)
            + [
                "merge",
                f"origin/{source}",
                "--no-ff",
                "-m",
                f"Merge branch '{source}' into {target}",
            ],
            cwd=tmp,
            capture_output=True,
            text=True,
        )
        if merge.returncode != 0:  # conflict!
            subprocess.run(["git", "merge", "--abort"], cwd=tmp, capture_output=True)
            return False
        if push:
            run_git(["push", "origin", target], cwd=tmp)
        return True
    except subprocess.CalledProcessError:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def can_merge(path, target, source):
    """Dry run: True if source merges into target without conflicts."""
    return _merge_in_temp(path, target, source, "check", "check@example.com", push=False)


def merge_branches(path, target, source, author, email):
    return _merge_in_temp(path, target, source, author, email, push=True)