#!/usr/bin/env python3
from posixpath import dirname
import sys
import os
import argparse
import subprocess
import shutil
import json
from os import path
from contextlib import contextmanager

# constants
# mainline VSCode release channel
REL_REH_CHANNEL = 'stable'
# location of base (primary target) of the VSCode REH relative to repo dir
BASE_REH_PATH = "work/aether-reh-darwin-arm64"
# location of VSCode repo path
BASE_VSCODE_PATH = "work/vscode"
# Linux container (e.g. for GNU binutils for stripping binaries)
CONT_NAME = "binutils:latest"
# location of podman Containerfile to build the container (relative to repo dir)
CONT_BLD_FILE = "scripts/containerfiles/binutils.containerfile"
# base work directory for REH derivation
BASE_DR_WORKDIR = "work/derivedreh"
# strip ELF binaries (can be modified via command-line args)
STRIP = False

# dynamic constants
# for declaring dynamic constants
def dynconst(fn): return fn()

# base directory of the repository
@dynconst
def REPODIR() -> str:
    cwd = path.dirname(__file__)
    repodir = subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], cwd=cwd)
    return repodir.decode('utf8').rstrip()

# tag name of our base VSCode repo
@dynconst
def BASE_TAG() -> str:
    rs = subprocess.check_output(['scripts/get-base-commit.sh', '--no-resolve-hash'], cwd=REPODIR)
    return rs.rstrip().decode('ascii')

# lazy dynamic constants (only evaluated on first call)
def lazydynconst(fn):
    cached = None
    def wrapper():
        nonlocal cached
        if cached is None:
            cached = fn()
        return cached
    return wrapper

@lazydynconst
def VSCODE_PRODUCT_INFO() -> dict:
    with open(path.join(REPODIR, BASE_VSCODE_PATH, "product.json")) as f:
        return json.load(f)

@lazydynconst
def SERVER_APP_NAME() -> str:
    return VSCODE_PRODUCT_INFO()['serverApplicationName']

# helper functions
def clonefile(src: str, tgt: str):
    '''
    Use macOS' "cp -c" to use clonefile for efficient copy
    '''
    if path.exists(tgt): return
    subprocess.check_output(["cp", "-c", "-R", src, tgt])

def replace_file(basefrom: str, baseto: str, fn: str):
    os.unlink(path.join(baseto, fn))
    clonefile(path.join(basefrom, fn), path.join(baseto, fn))

def make_executable(target: str):
    subprocess.check_output(["chmod", "+x", target])

def find_elf(basedir: str):
    '''Walk basedir and yield relative paths of files with ELF magic bytes.'''
    ELF_MAGIC = b'\x7fELF'
    for dirpath, _, files in os.walk(basedir):
        for file in files:
            fn = path.join(dirpath, file)
            with open(fn, 'rb') as f:
                magic_bytes = f.read(4)
            if magic_bytes == ELF_MAGIC:
                yield path.relpath(fn, basedir)

def strip_elf_binaries(basedir: str):
    '''
    Find all ELF binaries under basedir and strip them using podman.
    '''
    if not STRIP:
        return
    basedir = path.abspath(basedir)
    files = list(find_elf(basedir))
    if not files:
        return
    cont_basedir = '/target'
    # ensure the container image is available
    try:
        subprocess.check_output(['podman', 'image', 'inspect', CONT_NAME], stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        # build the container
        subprocess.check_call(['podman', 'build', '--tag', CONT_NAME, '--file', path.join(REPODIR, CONT_BLD_FILE)])
    podman_args = ['podman', 'run', '--rm', '-v', f'{basedir}:{cont_basedir}', CONT_NAME, 'strip']
    podman_args.extend(path.join(cont_basedir, fn) for fn in files)
    subprocess.check_call(podman_args)

class ArgPositionMatcher(set):
    '''
    The older version of Python 3 has a bug where providing an empty list in an `nargs='*'` argument
    causes an empty list (`[]`) to be matched against the given `choices` argument.
    '''
    def __contains__(self, item):
        if isinstance(item, list):
            return True
        return super().__contains__(item)

# List of classes to handle various REH targets. These classes must have these members:
# - downloads: list of (url, target file)
# - deps: list of dependencies to other targets
# - output_name: name of the output REH
# - run(workdir: str, *args): *args = workdir of dependencies (order by deps)
class LinuxGeneric:
    def __init__(self, target: str):
        _, kind, arch = target.split("-")
        if kind == 'gnu':
            out_name_suff = f"linux-{arch}"
        elif kind == 'alpine':
            out_name_suff = {'x64': 'linux-alpine', 'arm64': 'alpine-arm64'}[arch]
        else:
            raise NotImplementedError()
        self.extract_name = f"vscode-server-{out_name_suff}"
        self.tarball_name = f"server-{out_name_suff}.tarball"

        self.output_name = f"aether-reh-{out_name_suff}"
        self.deps = []
        # The upstream server tarball provides the Linux-native binaries; the
        # locally-built (patched) REH supplies everything else (see run()).
        self.downloads = [
            (f"https://update.code.visualstudio.com/{BASE_TAG}/server-{out_name_suff}/{REL_REH_CHANNEL}", self.tarball_name),
        ]

    @staticmethod
    def is_node_module_container(dirpath: str):
        '''
        Return True if the given path contains node.js modules
        '''
        if path.basename(dirpath) == "node_modules":
            # obviously true because this is node_modules
            return True

        # check for scoped module name
        if path.basename(path.dirname(dirpath)) == "node_modules" and path.basename(dirpath)[0] == "@":
            # scoped package
            return True

    def run(self, workdir: str, *_):
        # --- Stage 1: Extract the upstream Linux server tarball (b) ---
        # This provides Linux-native ELF binaries (node, native addons, etc.)
        # that will replace the macOS binaries from the locally-built REH (a).
        target_extract_dir = path.join(workdir, self.extract_name)
        if not path.exists(target_extract_dir):
            print("  Extracting ...")
            subprocess.check_call(["tar", "-xf", self.tarball_name], cwd=workdir)

        # --- Stage 2: Clone the locally-built macOS REH (a) as the output (c) ---
        # This gives us the patched VSCode source (JS/TS), extensions, and
        # macOS binaries.  We will swap in Linux-native pieces from (b) below.
        print("  Processing ...")
        target_dir = path.join(workdir, self.output_name)
        clonefile(path.join(REPODIR, BASE_REH_PATH), target_dir)

        # --- Stage 3: Replace the bootstrap script with the Linux version ---
        server_bootstrap_script_path = path.join(target_dir, "bin", SERVER_APP_NAME())
        os.unlink(server_bootstrap_script_path)
        clonefile(path.join(REPODIR, BASE_VSCODE_PATH, "resources/server/bin/code-server-linux.sh"),
            server_bootstrap_script_path)
        make_executable(server_bootstrap_script_path)

        # --- Stage 4: Swap the node binary ---
        replace_file(target_extract_dir, target_dir, "node")

        # --- Stage 5: Verify that (b) can satisfy all of (a)'s node_modules ---
        # Every node_modules directory that (a) has must also exist in (b),
        # otherwise we cannot produce a working Linux REH.
        a_base = path.join(REPODIR, BASE_REH_PATH)
        a_node_modules = []
        for dirpath, _, _ in os.walk(a_base):
            if path.basename(dirpath) == "node_modules":
                a_node_modules.append(path.relpath(dirpath, a_base))
        missing = []
        for rel_path in a_node_modules:
            if not path.exists(path.join(target_extract_dir, rel_path)):
                missing.append(rel_path)
        if missing:
            for m in missing:
                print(f"  MISSING: {m}")
            raise RuntimeError(
                f"{len(missing)} node_modules in source REH not found in upstream Linux server")

        # --- Stage 6: Replace all node_modules wholesale ---
        # Wipe (a)'s node_modules entirely, then copy (b)'s.  This is simpler
        # than swapping individual native binaries — every native addon (.node
        # file, helper binary, etc.) inside node_modules comes from (b) as-is.
        c_node_modules = path.join(target_dir, "node_modules")
        shutil.rmtree(c_node_modules)
        b_node_modules = path.join(target_extract_dir, "node_modules")
        clonefile(b_node_modules, c_node_modules)

        # --- Stage 7: Trim node_modules that (a) doesn't have ---
        for dirpath, dirnames, _ in os.walk(path.join(target_dir, "node_modules")):
            if not self.__class__.is_node_module_container(dirpath):
                continue
            # path relative to the (c)
            dirpath_rel = path.relpath(dirpath, target_dir)
            for i, dirname in reversed(list(enumerate(dirnames))):
                # if this module doesn't exist in (a), trim
                if not path.exists(path.join(a_base, dirpath_rel, dirname)):
                    shutil.rmtree(path.join(dirpath, dirname))
                    del dirnames[i]

        # --- Stage 8: Strip ELF binaries in the assembled output ---
        # Now that (c) has its final set of node_modules, strip all ELF
        # binaries to reduce size.  We scan (c) directly rather than
        # relying on a pre-computed list.
        strip_elf_binaries(target_dir)

class LinuxPortable:
    # patchelf container for patching node binary
    PATCHELF_CONT_NAME = "elfpatcher"
    PATCHELF_CONT_BLD_FILE = "scripts/containerfiles/patchelf.containerfile"

    def __init__(self, target: str):
        _, kind, self.arch = target.split("-")
        assert kind == 'portable'
        # alternative architecture name, commonly used in `uname -m`
        self.linux_arch_name = {'arm64': 'aarch64', 'x64': 'x86_64'}[self.arch]

        self.ld_path = f'/lib/ld-musl-{self.linux_arch_name}.so.1'

        self.cont_name = 'vscode-portable-libs-' + self.arch

        out_name_suff = f'linux-{kind}-{self.arch}'
        self.output_name = f"aether-reh-{out_name_suff}"

        dep = f'linux-alpine-{self.arch}'
        self.deps = [dep]

        # obtain dependency's output name
        self.dep_output_name = LinuxGeneric(dep).output_name

        self.downloads = []

    def _get_container_state(self):
        contstat = subprocess.check_output(['podman', 'container', 'inspect', self.cont_name], stderr=subprocess.DEVNULL)
        return json.loads(contstat)

    @contextmanager
    def _start_container(self):
        # check if it is already started
        try:
            contstat = self._get_container_state()
        except subprocess.CalledProcessError:
            # container does not exist
            self._maybe_create_container()
            contstat = self._get_container_state()

        if contstat[0]['State']['Status'] != 'running':
            subprocess.check_output(['podman', 'start', self.cont_name])
        try:
            yield
        finally:
            # stop the container afterwards
            subprocess.check_output(['podman', 'stop', self.cont_name])

    def _maybe_create_container(self):
        podman_platform = None
        if self.arch == 'arm64':
            # use the default platform
            pass
        elif self.arch == 'x64':
            podman_platform = 'linux/amd64'
        else:
            raise RuntimeError(f"Platform {self.arch} not supported")

        try:
            subprocess.check_output(['podman', 'container', 'inspect', self.cont_name], stderr=subprocess.DEVNULL)
            # container already exists
            return
        except subprocess.CalledProcessError:
            pass

        # container is not present, try building it
        # first, fetch the base image
        IMG_NAME_PREFIX = "alpine"
        img_name = f'{IMG_NAME_PREFIX}-{self.arch}'
        try:
            subprocess.check_output(['podman', 'image', 'inspect', img_name], stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            # download the container
            pullcmd = ['podman', 'pull']
            if podman_platform:
                pullcmd.extend(['--platform', podman_platform])
            pullcmd.append(IMG_NAME_PREFIX)
            subprocess.check_call(pullcmd)
            # tag the container
            subprocess.check_call(['podman', 'tag', IMG_NAME_PREFIX, img_name])

        # proceed to run the container
        subprocess.check_call([
            'podman', 'run', '-d', '--stop-signal=SIGKILL', '--name', self.cont_name,
            img_name, 'sleep', 'infinity'
        ])

        # install dependencies
        subprocess.check_call(['podman', 'exec', self.cont_name, 'apk', 'add', 'libstdc++'])

    def _container_discover_symlink(self, target: str):
        return subprocess.check_output(['podman', 'exec', self.cont_name, 'realpath', target])[:-1].decode('utf8')

    def _copy_from_container(self, src: str, target: str):
        subprocess.check_call(['podman', 'cp', f'{self.cont_name}:{src}', target])

    def _maybe_build_patchelf_container(self):
        cont_name = self.__class__.PATCHELF_CONT_NAME
        try:
            subprocess.check_output(['podman', 'image', 'inspect', cont_name], stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            # build the container
            subprocess.check_call([
                'podman', 'build', '--tag', cont_name,
                '--file', path.join(REPODIR, self.__class__.PATCHELF_CONT_BLD_FILE)
            ])

    def _patch_node_binary(self, output_dir: str):
        cont_name = self.__class__.PATCHELF_CONT_NAME
        output_dir_abs = path.abspath(output_dir)
        cont_output_dir = '/target'
        node_path = path.join(cont_output_dir, 'node')

        # Run patchelf commands to set rpath and interpreter
        subprocess.check_call([
            'podman', 'run', '--rm',
            '-v', f'{output_dir_abs}:{cont_output_dir}',
            cont_name,
            'patchelf', '--set-rpath', '$ORIGIN/sysroot', node_path
        ])
        subprocess.check_call([
            'podman', 'run', '--rm',
            '-v', f'{output_dir_abs}:{cont_output_dir}',
            cont_name,
            'patchelf', '--set-interpreter', f'./sysroot/{path.basename(self.ld_path)}', node_path
        ])

    def run(self, workdir: str, *deps: list[str]):
        clonefile(path.join(deps[0], self.dep_output_name), path.join(workdir, self.output_name))
        output_dir = path.join(workdir, self.output_name)
        target_sysroot_dir = path.join(output_dir, "sysroot")

        libc_name = f'libc.musl-{self.linux_arch_name}.so.1'

        # copy alpine sysroot from container
        with self._start_container():
            os.makedirs(target_sysroot_dir, exist_ok=True)
            self._copy_from_container(self.ld_path, path.join(target_sysroot_dir, path.basename(self.ld_path)))
            os.symlink(path.basename(self.ld_path), path.join(target_sysroot_dir, libc_name))
            self._copy_from_container('/usr/lib/libgcc_s.so.1', path.join(target_sysroot_dir, 'libgcc_s.so.1'))
            stdcpp_common_path = '/usr/lib/libstdc++.so.6'
            stdcpp_path = self._container_discover_symlink(stdcpp_common_path)
            self._copy_from_container(stdcpp_path, path.join(target_sysroot_dir, path.basename(stdcpp_path)))
            os.symlink(path.basename(stdcpp_path), path.join(target_sysroot_dir, path.basename(stdcpp_common_path)))

        # patch node binary using patchelf container
        self._maybe_build_patchelf_container()
        self._patch_node_binary(output_dir)

targets = {
    # GNU/Linux targets officially supported by mainline VSCode
    'linux-gnu-x64': LinuxGeneric,
    'linux-gnu-arm64': LinuxGeneric,
    # alpine/MUSL targets
    'linux-alpine-x64': LinuxGeneric,
    'linux-alpine-arm64': LinuxGeneric,
    # portable targets based on alpine targets
    'linux-portable-x64': LinuxPortable,
    'linux-portable-arm64': LinuxPortable,
}

class Builder:
    def __init__(self):
        self.target_instances = {target: clazz(target) for target, clazz in targets.items()}
        self.prereqs_downloaded = set()
        self.targets_built = set()

    def download_prereqs(self, target):
        if target in self.prereqs_downloaded:
            return
        bldinst = self.target_instances[target]
        for d in bldinst.deps:
            self.download_prereqs(d)
        workdir = self.target_workdir(target)
        for url, filename in bldinst.downloads:
            target_file = path.join(workdir, filename)
            if path.exists(target_file):
                continue
            subprocess.check_call(["curl", "-L", "-o", target_file, url])
        self.prereqs_downloaded.add(target)

    def do_build(self, target):
        if target in self.targets_built:
            return
        print("building", target, "...")
        bldinst = self.target_instances[target]
        workdir = self.target_workdir(target)
        # skip if output directory already exists
        if not path.exists(path.join(workdir, bldinst.output_name)):
            # build the dependencies first
            depworkdirs = []
            for d in bldinst.deps:
                self.do_build(d)
                depworkdirs.append(self.target_workdir(d))
            bldinst.run(workdir, *depworkdirs)
        self.targets_built.add(target)

    @staticmethod
    def target_workdir(target: str) -> str:
        ret = path.join(REPODIR, BASE_DR_WORKDIR, target)
        os.makedirs(ret, exist_ok=True)
        return ret

def main():
    global STRIP

    ap = argparse.ArgumentParser()
    ap.add_argument("--no-strip-elf", action='store_true')
    ap.add_argument("targets", choices=ArgPositionMatcher(targets), nargs='*')
    args = ap.parse_args()

    # if request to strip, check if we have podman
    if not args.no_strip_elf:
        if not shutil.which("podman"):
            print("'podman' is required to strip ELF binaries.")
            print("Disable stripping using '--no-strip-elf' option or install podman.")
            return 1
        STRIP = True

    # check if base (primary target) REH has been built
    if not path.exists(path.join(REPODIR, BASE_REH_PATH)):
        print("primary REH must have been built first before additional REHs can be derived.")
        return 1

    selected_targets = set(args.targets) or targets.keys()
    bldinst = Builder()
    for tgt in selected_targets:
        print("prereqs download for", tgt, "...")
        bldinst.download_prereqs(tgt)
    for tgt in selected_targets:
        bldinst.do_build(tgt)

if __name__ == "__main__":
    sys.exit(main())
