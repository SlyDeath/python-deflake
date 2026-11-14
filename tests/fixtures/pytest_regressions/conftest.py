def pytest_collection_modifyitems(items):
    # These projects use a fixed order so their state dependencies always reproduce
    items.sort(key=lambda item: item.nodeid)
