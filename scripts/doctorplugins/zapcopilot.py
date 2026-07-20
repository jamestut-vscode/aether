import json
import subprocess

def _run_npm_json(args):
    """Run npm and parse its JSON stdout."""
    cmd = ['npm', *args]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    output = result.stdout
    return json.loads(output)


def _resolve_npm_spec(spec):
    """Ask npm which exact version it would install for a package spec."""
    # `npm pack --dry-run --json` resolves ranges without downloading files.
    pack_result = _run_npm_json(['pack', spec, '--dry-run', '--json'])

    # npm returns a single manifest entry for the resolved package.
    resolved_version = pack_result[0].get('version')
    if not isinstance(resolved_version, str) or not resolved_version:
        raise RuntimeError(f'Could not resolve an exact version for {spec!r}')

    return resolved_version


def _get_npm_package_manifest_entry(package_name, spec, field_name):
    """Get an entry from `npm view` for a given package name and version."""
    # Then ask npm for only the manifest field we actually need.
    ret = _run_npm_json(['view', f'{package_name}@{spec}', field_name, '--json'])

    if not isinstance(ret, str) or not ret:
        raise RuntimeError( f'Could not read {field_name} from {package_name}@{spec}')

    return ret


def _remove_dependency(dependencies, package_name):
    """Remove one dependency and report whether anything changed."""
    # Missing deps are fine; this plugin should be safely repeatable.
    if package_name not in dependencies:
        return False
    print(f"Removing {package_name} ...")
    del dependencies[package_name]
    return True


def _replace_deviceid_dependency(dependencies):
    """Replace @vscode/deviceid with the type dependency it supplied."""
    # Nothing to replace if upstream already dropped the package.
    DEVICEID_PACKAGE_NAME = '@vscode/deviceid'
    FSEXTRA_PACKAGE_NAME = 'fs-extra'

    if DEVICEID_PACKAGE_NAME not in dependencies:
        return False

    # Resolve ranges first so we inspect the exact manifest npm would install.
    deviceid_spec = dependencies[DEVICEID_PACKAGE_NAME]

    # Remove deviceid
    _remove_dependency(dependencies, DEVICEID_PACKAGE_NAME)

    # If we don't have fs-extra, resolve, then add
    if  FSEXTRA_PACKAGE_NAME not in dependencies:
        print(f"Retreiving {FSEXTRA_PACKAGE_NAME} info from {DEVICEID_PACKAGE_NAME} ...")
        resolved_deviceid_version = _resolve_npm_spec(f'{DEVICEID_PACKAGE_NAME}@{deviceid_spec}')
        fs_extra_types_version = _get_npm_package_manifest_entry(
            DEVICEID_PACKAGE_NAME,
            resolved_deviceid_version,
            f'dependencies.{FSEXTRA_PACKAGE_NAME}',
        )
        print(
            f"{DEVICEID_PACKAGE_NAME}@{resolved_deviceid_version} depends on",
            f"{FSEXTRA_PACKAGE_NAME}@{fs_extra_types_version}"
        )
        dependencies[FSEXTRA_PACKAGE_NAME] = fs_extra_types_version

    return True


def pre_run(doctorfn):
    """Patch package manifests before product-info doctoring runs."""
    # Patch both package manifests, but only the top-level compile script.
    for pkgpath, zap_script in [('package.json', True), ('remote/package.json', False)]:
        print(f"Zapping Copilot from {pkgpath}...")
        with doctorfn(pkgpath) as context:
            # Dependency removal applies to all manifests.
            dependencies = context.data.get('dependencies', {})
            # Copilot is removed outright.
            if _remove_dependency(dependencies, '@github/copilot'):
                context.modified = True
            # DeviceID is removed, but one transitive type dependency (fs-extra) is preserved.
            if _replace_deviceid_dependency(dependencies):
                context.modified = True

            # Only the root manifest has the compile script we need to trim.
            if not zap_script:
                continue

            COMPILE_COPILOT = 'compile-copilot'
            compile_script = context.data.get('scripts', {}).get('compile', '')
            compile_script_parts = compile_script.split()
            if COMPILE_COPILOT not in compile_script_parts:
                continue
            context.data['scripts']['compile'] = ' '.join(
                part for part in compile_script_parts if part != COMPILE_COPILOT)
            context.modified = True
