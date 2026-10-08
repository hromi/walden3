from walden.core.merge import deep_merge

def test_deep_merge():
    assert deep_merge({"a":{"x":1,"y":2},"b":2},{"a":{"x":3},"b":None})=={"a":{"x":3,"y":2}}

def test_empty_values_do_not_erase_memory():
    base={"projects":["Walden 3"],"name":"DDH","dog":{"name":"Juni"}}
    assert deep_merge(base,{"projects":[],"name":"","dog":{},"roles":[]})==base

def test_lists_accumulate_without_duplicates():
    assert deep_merge({"projects":["Walden 3"]},{"projects":["Walden 3","Frank"]})=={"projects":["Walden 3","Frank"]}
