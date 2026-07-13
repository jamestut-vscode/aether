#!/usr/bin/env python3
"""
Create .tar.xz archives of built VSCode packages.

This is a standalone utility that archives the output directories produced by
njgen.py (darwin app/REH) and derivedreh.py (Linux REH variants).  Archives
are written to work/packaging/ by default.

Usage:
    scripts/create-archive.py
    scripts/create-archive.py -o /tmp/releases
    scripts/create-archive.py --parallel-mode compression
    scripts/create-archive.py -j 4
"""

import argparse
import multiprocessing
import os
import subprocess
import sys
import threading
from os import path

# Output file suffix.  Change this if the compression format changes.
SUFFIX = ".tar.xz"


# ---------------------------------------------------------------------------
# CWD setup — same pattern as njgen.py: change to the script's directory, then
# to the repo root so all relative paths resolve correctly.
# ---------------------------------------------------------------------------

def set_cwd():
    newcwd = path.dirname(sys.argv[0])
    if newcwd:
        os.chdir(newcwd)
    repodir = subprocess.check_output(
        ['git', 'rev-parse', '--show-toplevel']).decode('utf8').rstrip()
    os.chdir(repodir)


# ---------------------------------------------------------------------------
# Archiving primitives
# ---------------------------------------------------------------------------

def archive_one(source_dir: str, output_path: str, compression_threads: int | None = None):
    """
    Create a single .tar.xz archive.

    tar is piped to xz. When compression_threads is set, xz runs multi-threaded
    with -T<compression_threads> (typically fed from -j in "compression" mode,
    one job at a time). When it is None/falsy, xz runs single-threaded; this is
    used in "job" mode where parallelism comes from running many archives
    concurrently via threads rather than within xz.

    Args:
        source_dir:          Directory to archive (e.g. "work/Aether-darwin-arm64").
        output_path:         Full path for the output .tar.xz file.
        compression_threads: xz thread count, or None for single-threaded xz.
    """
    parent = path.dirname(source_dir)
    name = path.basename(source_dir)

    tar_cmd = ["tar", "--uid", "0", "--gid", "0", "-C", parent, "-cf", "-",
               "--no-mac-metadata", "--exclude=.DS_Store", "--exclude=._*", name]
    xz_cmd = ["xz"]
    if compression_threads:
        xz_cmd.append(f"-T{compression_threads}")

    tar_env = dict(os.environ, COPYFILE_DISABLE="1")

    with open(output_path, "wb") as fout:
        tar = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, env=tar_env)
        xz = subprocess.Popen(xz_cmd, stdin=tar.stdout, stdout=fout)
        tar.stdout.close()  # allow tar to receive SIGPIPE if xz exits early
        xz.wait()
        tar.wait()
        if tar.returncode != 0 or xz.returncode != 0:
            raise RuntimeError(
                f"archive failed: tar={tar.returncode} xz={xz.returncode}"
            )


class ParallelArchiver:
    """
    Runs multiple archive_one() jobs concurrently using threads.

    Only used in "job" mode, where each xz instance is single-threaded but
    many archives are compressed in parallel.  The semaphore limits the
    number of concurrent jobs to avoid overwhelming the system.
    """

    def __init__(self, njobs: int):
        self.sem = threading.Semaphore(njobs)
        self.threads: list[threading.Thread] = []

    def add_job(self, source_dir: str, output_path: str, compression_threads: int | None = None):
        """Enqueue an archive job.  Blocks if at capacity."""
        self.sem.acquire()
        t = threading.Thread(
            target=self._run,
            args=(source_dir, output_path, compression_threads),
        )
        self.threads.append(t)
        t.start()

    def _run(self, source_dir: str, output_path: str, compression_threads: int | None = None):
        try:
            archive_one(source_dir, output_path, compression_threads)
        finally:
            self.sem.release()

    def wait_all(self):
        """Block until every queued job has finished."""
        for t in self.threads:
            t.join()


# ---------------------------------------------------------------------------
# Directory discovery
# ---------------------------------------------------------------------------

def collect_directories() -> list[tuple[str, str]]:
    """
    Return a list of (source_dir, archive_name) tuples for every directory
    that should be archived.

    Sources come from two places:
      1. Darwin targets — hardcoded paths produced by njgen.py.
      2. Derived REH targets — dynamically obtained from derivedreh.targets,
         which maps target names (e.g. "linux-gnu-x64") to classes whose
         instances expose an output_name attribute.
    """
    dirs: list[tuple[str, str]] = []

    # --- 1. Darwin app and REH (produced by njgen.py) ---
    for name in ["Aether-darwin-arm64", "aether-reh-darwin-arm64"]:
        dirs.append((path.join("work", name), name))

    # --- 2. Derived Linux REH variants (produced by derivedreh.py) ---
    # Import derivedreh from the same directory as this script.
    # This triggers module-level code (REPODIR, BASE_TAG computation),
    # which is safe and expected.
    scripts_dir = path.dirname(path.abspath(__file__))
    sys.path.insert(0, scripts_dir)
    from derivedreh import targets as derivedreh_targets, BASE_DR_WORKDIR

    for target_name, clazz in derivedreh_targets.items():
        # Instantiate the target class to obtain its output_name.
        # The classes are lightweight — __init__ only computes strings.
        instance = clazz(target_name)
        # Path mirrors Builder.target_workdir() + output_name:
        #   work/derivedreh/<target>/<output_name>
        src = path.join(BASE_DR_WORKDIR, target_name, instance.output_name)
        dirs.append((src, instance.output_name))

    return dirs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    set_cwd()

    ap = argparse.ArgumentParser(
        description="Create archives of built VSCode packages.",
    )
    ap.add_argument(
        "--output-dir", "-o",
        default="work/packaging",
        help="Output directory for archives (default: work/packaging)",
    )
    ap.add_argument(
        "--parallelism", "-j",
        type=int,
        default=multiprocessing.cpu_count(),
        help=(
            "Max concurrent archive jobs in 'job' mode (default: cpu count). "
            "In 'compression' mode this is passed to xz as the -T thread count."
        ),
    )
    ap.add_argument(
        "--parallel-mode",
        choices=["compression", "job"],
        default="job",
        help=(
            "Parallelism strategy: 'compression' uses xz -T0 (xz handles "
            "all threads, jobs run sequentially); 'job' runs many single-"
            "threaded xz processes in parallel (default)."
        ),
    )
    args = ap.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    # Collect all candidate directories and filter to existing ones.
    all_dirs = collect_directories()
    existing: list[tuple[str, str]] = []
    for src, name in all_dirs:
        if path.isdir(src):
            existing.append((src, name))
        else:
            print(f"skipping {src} (not found)")

    if not existing:
        print("no directories to archive")
        return

    # Filter out archives that already exist.
    to_archive: list[tuple[str, str]] = []
    for src, name in existing:
        out = path.join(args.output_dir, name + SUFFIX)
        if path.exists(out):
            print(f"skipping {src} -> {out} (archive already exists)")
        else:
            to_archive.append((src, name))

    if not to_archive:
        print("all archives already exist, nothing to do")
        return

    # Run the archive jobs.
    if args.parallel_mode == "compression":
        # xz -T<j> uses the requested thread count; no benefit from
        # multiple jobs, so run them sequentially.
        for src, name in to_archive:
            out = path.join(args.output_dir, name + SUFFIX)
            print(f"archiving {src} -> {out}")
            archive_one(src, out, args.parallelism)
    else:
        # job mode: each xz is single-threaded (None -> no -T), run many in parallel.
        archiver = ParallelArchiver(args.parallelism)
        for src, name in to_archive:
            out = path.join(args.output_dir, name + SUFFIX)
            print(f"archiving {src} -> {out}")
            archiver.add_job(src, out)
        archiver.wait_all()


if __name__ == "__main__":
    sys.exit(main() or 0)
