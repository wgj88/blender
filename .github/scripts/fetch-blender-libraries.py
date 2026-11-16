#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Fetch Blender precompiled library snapshots and hydrate their Git LFS objects."""
import concurrent.futures
import hashlib
import io
import os
import re
import stat
import subprocess
import time
import zipfile
from pathlib import Path

import requests

ROOT = Path(os.environ.get("GITHUB_WORKSPACE", "."))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Blender-Android-CI/1.0"})
POINTER = re.compile(
    rb"version https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize ([0-9]+)"
)
BATCH_SIZE = 100
WORKERS = 10


def fetch_archive(repo, commit, destination):
    url = f"https://projects.blender.org/api/v1/repos/blender/{repo}/archive/{commit}.zip"
    print(f"Downloading source archive: {repo}@{commit}", flush=True)
    response = None
    last_error = None
    for attempt in range(1, 6):
        try:
            response = SESSION.get(url, timeout=(30, 300))
            response.raise_for_status()
            break
        except requests.RequestException as error:
            last_error = error
            status = getattr(getattr(error, "response", None), "status_code", None)
            print(f"Archive request attempt {attempt}/5 failed (HTTP {status}): {error}", flush=True)
            if attempt < 5:
                time.sleep(min(30, attempt * 5))
    if response is None or not response.ok:
        # Cloudflare may intermittently reject Python requests from hosted runners;
        # retry through curl's independent HTTP stack before giving up.
        archive_path = destination.parent / f".{repo}-{commit}.zip"
        command = ["curl", "--fail", "--location", "--retry", "5",
                   "--retry-all-errors", "--retry-delay", "5",
                   "--connect-timeout", "30", "--max-time", "600",
                   "--user-agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
                   url, "--output", str(archive_path)]
        subprocess.run(command, check=True)
        archive_bytes = archive_path.read_bytes()
        archive_path.unlink()
    else:
        archive_bytes = response.content
    archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    members = archive.namelist()
    prefix = members[0].split("/", 1)[0] + "/"
    destination.mkdir(parents=True, exist_ok=True)
    for member in members:
        if member.endswith("/"):
            continue
        relative = member[len(prefix):]
        if not relative:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        info = archive.getinfo(member)
        mode_bits = (info.external_attr >> 16) & 0xffff
        if stat.S_ISLNK(mode_bits):
            link_target = archive.read(member).decode("utf-8")
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(link_target)
            continue
        with archive.open(member) as src, target.open("wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
        mode = mode_bits & 0o777
        if mode:
            target.chmod(mode)
        else:
            # Some archives carry no Unix mode; executables must stay runnable.
            data = target.read_bytes()[:4]
            if data == b"\x7fELF" or (data[:2] == b"#!"):
                target.chmod(0o755)
    # Directories are created implicitly via mkdir() under the runner umask;
    # re-apply modes recorded for explicit directory entries.
    for member in members:
        if not member.endswith("/"):
            continue
        relative = member[len(prefix):]
        if not relative:
            continue
        info = archive.getinfo(member)
        mode = (info.external_attr >> 16) & 0o777
        if mode:
            (destination / relative).chmod(mode)
    print(f"Extracted {len(members)} archive entries into {destination}", flush=True)


def lfs_objects(destination):
    objects = []
    for path in destination.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if path.stat().st_size > 1024:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        match = POINTER.match(data)
        if match:
            objects.append((path, match.group(1).decode(), int(match.group(2))))
    return objects


def request_batch(repo, objects):
    url = f"https://projects.blender.org/blender/{repo}.git/info/lfs/objects/batch"
    payload = {
        "operation": "download",
        "transfers": ["basic"],
        "objects": [{"oid": oid, "size": size} for _, oid, size in objects],
    }
    response = SESSION.post(
        url,
        json=payload,
        headers={
            "Accept": "application/vnd.git-lfs+json",
            "Content-Type": "application/vnd.git-lfs+json",
        },
        timeout=(30, 120),
    )
    response.raise_for_status()
    actions = {}
    for item in response.json().get("objects", []):
        if "error" in item:
            raise RuntimeError(f"LFS object {item.get('oid')} rejected: {item['error']}")
        actions[item["oid"]] = item.get("actions", {}).get("download")
    return actions


def download_one(item):
    path, oid, size, action = item
    if not action or not action.get("href"):
        raise RuntimeError(f"No LFS download action for {oid} ({path})")
    temporary = path.with_name(path.name + ".lfs-download")
    digest = hashlib.sha256()
    written = 0
    with requests.get(action["href"], headers=action.get("header", {}),
                      stream=True, timeout=(30, 600)) as response:
        response.raise_for_status()
        with temporary.open("wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    output.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
    if written != size or digest.hexdigest() != oid:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"Integrity check failed for {path}: size={written}/{size}, "
            f"sha256={digest.hexdigest()}/{oid}"
        )
    # Hydration replaces the pointer file; the temp file is created with the
    # default umask, so preserve the pointer file's Unix mode (the ZIP/git
    # entry mode, e.g. 0755 for executables) or executables become 0644.
    mode = path.stat().st_mode & 0o777
    if mode:
        temporary.chmod(mode)
    temporary.replace(path)
    return written


def hydrate(repo, destination):
    objects = lfs_objects(destination)
    total_bytes = sum(size for _, _, size in objects)
    print(f"{repo}: {len(objects)} LFS objects, {total_bytes / 1024**3:.2f} GiB", flush=True)
    completed = 0
    downloaded = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        for offset in range(0, len(objects), BATCH_SIZE):
            batch = objects[offset:offset + BATCH_SIZE]
            actions = request_batch(repo, batch)
            tasks = [(path, oid, size, actions.get(oid)) for path, oid, size in batch]
            futures = [executor.submit(download_one, item) for item in tasks]
            for future in concurrent.futures.as_completed(futures):
                downloaded += future.result()
                completed += 1
                if completed % 25 == 0 or completed == len(objects):
                    print(f"{repo}: {completed}/{len(objects)} objects, "
                          f"{downloaded / 1024**3:.2f} GiB", flush=True)
    remaining = lfs_objects(destination)
    if remaining:
        raise RuntimeError(f"{repo}: {len(remaining)} LFS pointer files remain")
    # Self-check: hydrated LFS files must preserve the executable bit of the
    # pointer file; catch any ELF/shebang file left non-executable.
    for path in destination.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if not os.access(path, os.X_OK) and path.read_bytes()[:4] in (
                b"\x7fELF",
                b"#!",
            ):
                print(f"Warning: fixing missing exec bit on {path}", flush=True)
                path.chmod(0o755)
        except OSError:
            continue
    print(f"{repo}: LFS hydration complete", flush=True)


def main():
    libraries = (
        ("lib-android_arm64", "a4f5559683c16104daa796943aad34fddf156385", "android_arm64"),
        ("lib-linux_x64", "bd0e2baa6748315282b8b95749d421c4b2ed8b4c", "linux_x64"),
    )
    for repo, commit, folder in libraries:
        destination = ROOT / "lib" / folder
        fetch_archive(repo, commit, destination)
        hydrate(repo, destination)


if __name__ == "__main__":
    main()

