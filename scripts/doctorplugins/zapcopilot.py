def zap_copilot_dep(context, srcpath):
    deps = context.data.get('dependencies', {})
    if '@github/copilot' in deps:
        print(f"Removing @github/copilot dep from {srcpath}...")
        del deps['@github/copilot']
        context.modified = True

def pre_run(doctorfn):
    for pkgpath, zap_script in [('package.json', True), ('remote/package.json', False)]:
        with doctorfn(pkgpath) as context:
            zap_copilot_dep(context, pkgpath)
            if not zap_script:
                continue
            compile_script = context.data.get('scripts', {}).get('compile', '')
            if 'compile-copilot' in compile_script:
                print("Removing compile-copilot from compile script ...")
                context.data['scripts']['compile'] = compile_script.replace(' compile-copilot', '')
                context.modified = True
