#!/usr/bin/env python3
"""
Cross-platform installer & uninstaller for ibkr-desktop-fix (macOS / Linux / Windows).

1. Locates IBKR Desktop (`ntws.vmoptions` and `lib/ntws4launch-*.jar`).
2. Builds `ibkr-webchannel-fix.jar` (via JDK `javac --release 17` if installed, or via the
   zero-dependency Python ClassFile v61.0 assembler).
3. Verifies the agent JAR and its transformed ClassFile v61.0 + StackMapTable outputs against
   both synthetic targets and your local `ntws4launch-*.jar` BEFORE modifying any config file.
4. Atomically deploys `ibkr-webchannel-fix.jar` to `~/ntws/ibkr-webchannel-fix.jar` and updates
   the `### keep on update` block of `ntws.vmoptions` idempotently.
"""

import argparse
import glob
import hashlib
import ntpath
import os
import platform
import posixpath
import shutil
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import build_jar as bj
import verify_agent as va

MANAGED_FLAGS_ZGC = [
    "-XX:-UseParallelGC",
    "-XX:+UseZGC",
    "-Dio.qt.log-messages=CRITICAL",
]
MANAGED_FLAGS_NO_ZGC = [
    "-Dio.qt.log-messages=CRITICAL",
]
KEEP_MARKER = "### keep on update"


def candidate_ibkr_dirs() -> list:
    home = os.path.expanduser("~")
    sysname = platform.system()
    cands = []
    if sysname == "Darwin":
        cands.extend([
            os.path.join(home, "Applications", "IBKR Desktop"),
            "/Applications/IBKR Desktop",
            os.path.join(home, "Applications", "IBKR Desktop.app", "Contents", "Resources", "app"),
            "/Applications/IBKR Desktop.app/Contents/Resources/app",
        ])
    elif sysname == "Windows":
        local_app = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        cands.extend([
            os.path.join(local_app, "Programs", "IBKR Desktop"),
            r"C:\Jts\ibkr-desktop",
            os.path.join(prog_files, "IBKR Desktop"),
        ])
    else:
        cands.extend([
            os.path.join(home, "Jts", "ibkr-desktop"),
            os.path.join(home, "ibkr-desktop"),
            "/opt/ibkr-desktop",
            "/usr/local/ibkr-desktop",
        ])
    return cands


def detect_vmoptions(ibkr_dir: str = None, vmoptions_path: str = None) -> str:
    if vmoptions_path:
        p = os.path.abspath(os.path.expanduser(vmoptions_path))
        if not os.path.isfile(p):
            raise FileNotFoundError(f"Specified ntws.vmoptions does not exist: {p}")
        return p
    if ibkr_dir:
        d = os.path.abspath(os.path.expanduser(ibkr_dir))
        p = os.path.join(d, "ntws.vmoptions")
        if not os.path.isfile(p):
            raise FileNotFoundError(f"ntws.vmoptions not found inside: {d}")
        return p
    for d in candidate_ibkr_dirs():
        p = os.path.join(d, "ntws.vmoptions")
        if os.path.isfile(p):
            return p
    searched = "\n  - ".join(candidate_ibkr_dirs())
    raise FileNotFoundError(
        f"Could not auto-detect IBKR Desktop ntws.vmoptions. Searched:\n  - {searched}\n"
        f"Pass --ibkr-dir <path> or --vmoptions <path/to/ntws.vmoptions>."
    )


def detect_ntws4launch_jar(ibkr_dir: str) -> str:
    pattern = os.path.join(ibkr_dir, "lib", "ntws4launch-*.jar")
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


def has_whitespace(s: str) -> bool:
    return any(c.isspace() for c in s)


def default_agent_dest(sysname: str = None, home: str = None, env: dict = None) -> str:
    sysname = sysname or platform.system()
    home = home if home is not None else os.path.expanduser("~")
    env = env if env is not None else os.environ
    pjoin = ntpath.join if sysname == "Windows" else posixpath.join
    primary = pjoin(home, "ntws", "ibkr-webchannel-fix.jar")
    if not has_whitespace(primary):
        return primary

    if sysname == "Windows":
        win_dirs = []
        if sysname == platform.system() and os.path.isdir(r"C:\Jts"):
            win_dirs.append(r"C:\Jts\ibkr-desktop-fix")
        win_dirs.extend([
            ntpath.join(env.get("PROGRAMDATA", r"C:\ProgramData"), "ibkr-desktop-fix"),
            ntpath.join(env.get("PUBLIC", r"C:\Users\Public"), "ibkr-desktop-fix"),
            r"C:\Jts\ibkr-desktop-fix",
        ])
        for d in win_dirs:
            cand = ntpath.join(d, "ibkr-webchannel-fix.jar")
            if not has_whitespace(cand):
                return cand
        return r"C:\ibkr-desktop-fix\ibkr-webchannel-fix.jar"

    if sysname == "Darwin":
        return "/Users/Shared/ibkr-desktop-fix/ibkr-webchannel-fix.jar"
    return "/var/tmp/ibkr-desktop-fix/ibkr-webchannel-fix.jar"


def detect_installed_agent_dest(raw_text: str) -> str:
    for ln in raw_text.splitlines():
        s = ln.strip()
        if is_our_agent_line(s):
            return s[len("-javaagent:"):]
    return None


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_our_agent_line(stripped: str) -> bool:
    return stripped.startswith("-javaagent:") and "ibkr-webchannel-fix.jar" in stripped


def compute_installed_vmoptions(raw_text: str, agent_dest: str, enable_zgc: bool) -> str:
    newline = "\r\n" if "\r\n" in raw_text else "\n"
    lines = raw_text.splitlines()

    marker_idx = -1
    for i, ln in enumerate(lines):
        if ln.strip().lower() == KEEP_MARKER:
            marker_idx = i
            break

    if marker_idx == -1:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(KEEP_MARKER)
        marker_idx = len(lines) - 1

    head = [ln for ln in lines[:marker_idx + 1] if not is_our_agent_line(ln.strip())]
    tail_raw = [ln for ln in lines[marker_idx + 1:] if not is_our_agent_line(ln.strip())]

    desired = list(MANAGED_FLAGS_ZGC if enable_zgc else MANAGED_FLAGS_NO_ZGC)
    agent_flag = f"-javaagent:{agent_dest}"
    existing_tail_set = {ln.strip() for ln in tail_raw}

    out_tail = list(tail_raw)
    while out_tail and out_tail[-1].strip() == "":
        out_tail.pop()

    for flag in desired:
        if flag not in existing_tail_set:
            out_tail.append(flag)
            existing_tail_set.add(flag)
    out_tail.append(agent_flag)

    return newline.join(head + out_tail) + newline


def compute_uninstalled_vmoptions(raw_text: str) -> str:
    newline = "\r\n" if "\r\n" in raw_text else "\n"
    lines = raw_text.splitlines()
    marker_idx = -1
    for i, ln in enumerate(lines):
        if ln.strip().lower() == KEEP_MARKER:
            marker_idx = i
            break

    remove_in_tail = set(MANAGED_FLAGS_ZGC)
    out = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if is_our_agent_line(s):
            continue
        if marker_idx != -1 and i > marker_idx and s in remove_in_tail:
            continue
        out.append(ln)

    while out and out[-1].strip() == "":
        out.pop()
    return newline.join(out) + newline


def remove_or_trash_file(path: str):
    if not os.path.exists(path):
        return
    if platform.system() == "Darwin":
        trash_dir = os.path.join(os.path.expanduser("~"), ".Trash")
        if os.path.isdir(trash_dir):
            dest = os.path.join(trash_dir, f"{os.path.basename(path)}.{int(time.time())}")
            shutil.move(path, dest)
            print(f"[install] Moved {path} -> {dest}")
            return
    os.remove(path)
    print(f"[install] Removed {path}")


def atomic_write_text(path: str, content: str):
    parent = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(prefix=".ntws_vmoptions_", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        if os.path.exists(path):
            shutil.copymode(path, tmp_path)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def run_install(args):
    vmopt_path = detect_vmoptions(args.ibkr_dir, args.vmoptions)
    ibkr_dir = os.path.dirname(vmopt_path)

    with open(vmopt_path, "r", encoding="utf-8") as f:
        orig_text = f.read()

    if args.agent_dest:
        agent_dest = os.path.abspath(os.path.expanduser(args.agent_dest))
    elif args.uninstall:
        agent_dest = detect_installed_agent_dest(orig_text) or os.path.abspath(default_agent_dest())
    else:
        agent_dest = os.path.abspath(default_agent_dest())

    if args.uninstall:
        new_text = compute_uninstalled_vmoptions(orig_text)
        print(f"[install] Target vmoptions : {vmopt_path}")
        print(f"[install] Deployed agent   : {agent_dest}")
        if args.dry_run:
            print("[install] --dry-run enabled; resulting ntws.vmoptions would be:\n")
            print(new_text.rstrip("\r\n"))
            return
        if new_text != orig_text:
            atomic_write_text(vmopt_path, new_text)
            print(f"[install] Cleaned managed flags from {vmopt_path}")
        else:
            print(f"[install] No managed flags present in {vmopt_path}")
        remove_or_trash_file(agent_dest)
        print("[install] Uninstall complete. Restart IBKR Desktop to apply.")
        return

    if has_whitespace(agent_dest):
        raise ValueError(
            f"Agent JAR destination path must not contain spaces (install4j splits vmoptions on whitespace): {agent_dest}\n"
            f"Pass --agent-dest with a whitespace-free directory."
        )

    ntws4_jar = detect_ntws4launch_jar(ibkr_dir)
    print(f"[install] Detected IBKR dir : {ibkr_dir}")
    print(f"[install] Target vmoptions  : {vmopt_path}")
    print(f"[install] Target IBKR JAR   : {ntws4_jar or '(not found; verifying against synthetic ClassFile v61.0)'}")
    print(f"[install] Agent destination : {agent_dest}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        staged_jar = os.path.join(tmp_dir, "ibkr-webchannel-fix.jar")
        if args.prebuilt and os.path.isfile(bj.DEFAULT_OUT):
            shutil.copy2(bj.DEFAULT_OUT, staged_jar)
            backend = "prebuilt-dist-jar"
        else:
            backend = bj.build_jar(staged_jar)
        digest = sha256_file(staged_jar)
        print(f"[install] Built agent JAR ({os.path.getsize(staged_jar)} bytes, backend={backend}, sha256={digest[:16]}...)")

        va.run_verification(staged_jar, ibkr_jar=ntws4_jar)
        new_text = compute_installed_vmoptions(orig_text, agent_dest, enable_zgc=not args.no_zgc)

        if args.dry_run:
            print("[install] --dry-run enabled; no files modified. Resulting ntws.vmoptions would be:\n")
            print(new_text.rstrip("\r\n"))
            return

        os.makedirs(os.path.dirname(agent_dest), exist_ok=True)
        fd, tmp_jar = tempfile.mkstemp(prefix=".ibkr_agent_", suffix=".jar", dir=os.path.dirname(agent_dest))
        os.close(fd)
        try:
            shutil.copy2(staged_jar, tmp_jar)
            os.replace(tmp_jar, agent_dest)
        finally:
            if os.path.exists(tmp_jar):
                os.remove(tmp_jar)

    bak_path = vmopt_path + ".bak"
    if not os.path.exists(bak_path):
        shutil.copy2(vmopt_path, bak_path)
        print(f"[install] Saved stock backup: {bak_path}")

    if new_text != orig_text:
        atomic_write_text(vmopt_path, new_text)
        print(f"[install] Updated {vmopt_path}")
    else:
        print(f"[install] {vmopt_path} already configured")

    print(f"[install] Deployed {agent_dest} (sha256={sha256_file(agent_dest)})")
    print("[install] Installation complete. Restart IBKR Desktop to activate.")


def main():
    ap = argparse.ArgumentParser(description="Install or uninstall ibkr-desktop-fix")
    ap.add_argument("--ibkr-dir", default=None, help="Path to IBKR Desktop installation directory")
    ap.add_argument("--vmoptions", default=None, help="Explicit path to ntws.vmoptions")
    ap.add_argument(
        "--agent-dest",
        default=None,
        help=f"Destination path for ibkr-webchannel-fix.jar (default: {default_agent_dest()})",
    )
    ap.add_argument("--no-zgc", action="store_true", help="Do not add -XX:-UseParallelGC / -XX:+UseZGC")
    ap.add_argument("--prebuilt", action="store_true", help="Use prebuilt dist/ibkr-webchannel-fix.jar if present")
    ap.add_argument("--uninstall", action="store_true", help="Remove managed flags from ntws.vmoptions and remove agent JAR")
    ap.add_argument("--dry-run", action="store_true", help="Build and verify without modifying any files")
    args = ap.parse_args()
    run_install(args)


if __name__ == "__main__":
    main()
