def pre_run(doctorfn):
    print("Adding VSCode marketplace support ...")
    target_key = 'extensionsGallery'
    with doctorfn('product.json') as context:
        if target_key in context.data:
            print("Marketplace already set.")
            return
        context.data[target_key] = {
            "serviceUrl": "https://open-vsx.org/vscode/gallery",
            "itemUrl": "https://open-vsx.org/vscode/item",
        }
        context.modified = True
