def pre_run(doctorfn):
    kvs = {
        "nameShort": "Aether",
        "nameLong": "Aether",
        "applicationName": "ae",
        "dataFolderName": ".aether",
        "sharedDataFolderName": ".aether-shared",
        "urlProtocol": "aether",
        "serverApplicationName": "aether-server",
        "serverDataFolderName": ".aether-server",
        "tunnelApplicationName": "aether-tunnel",
    }
    print("Updating product naming ...")
    with doctorfn('product.json') as context:
        if all(context.data[k] == v for k, v in kvs.items()):
            print("Product naming already set. Skipping.")
            return 0
        for k, v in kvs.items():
            context.data[k] = v
        context.modified = True
