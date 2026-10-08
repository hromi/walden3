from walden.core.uri import parse, person

def test_uri():
    u=person("0xabc"); x=parse(u); assert x.kind=="person" and x.ident=="0xabc"
