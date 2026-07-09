def pre_run(doctorfn):
    with doctorfn('package.json') as context:
        deps = context.data.get('dependencies', {})
        if '@github/copilot' in deps:
            print("Removing @github/copilot from dependencies ...")
            del deps['@github/copilot']
            context.modified = True
        compile_script = context.data.get('scripts', {}).get('compile', '')
        if 'compile-copilot' in compile_script:
            print("Removing compile-copilot from compile script ...")
            context.data['scripts']['compile'] = compile_script.replace(' compile-copilot', '')
            context.modified = True
