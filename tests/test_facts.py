from walden.core.facts import apply, in_room, render


def test_add_update_retract_keep_origin():
    mem = apply({}, [{"op": "add", "fact": "has a dog named Juni", "said_by": "wal://person/d", "said_by_name": "DDH"},
                     {"op": "add", "fact": "Has a dog named Juni", "said_by": "wal://person/d"}],  # duplicate ignored
                room="wal://room/r", episode="wal://episode/e1", at="2026-09-24", taken=set())
    [f] = mem["facts"]
    assert f["id"].startswith("f-") and f["room"] == "wal://room/r" and f["episode"] == "wal://episode/e1"
    mem2 = apply(mem, [{"op": "update", "id": f["id"], "fact": "has a dog named Juni (80% poodle)", "said_by": "wal://person/d", "said_by_name": "DDH"}],
                 room="wal://room/r", episode="wal://episode/e2", at="2026-09-25", taken=set())
    assert mem2["facts"][0]["fact"].endswith("(80% poodle)") and mem2["facts"][0]["id"] == f["id"]
    assert mem["facts"][0]["fact"] == "has a dog named Juni"  # input not mutated
    assert apply(mem2, [{"op": "retract", "id": f["id"]}], room="r", episode=None, at="", taken=set())["facts"] == []


def test_facts_stay_in_their_room_and_render():
    facts = [{"id": "f-1", "fact": "likes tea", "room": "A", "said_by_name": "Alice", "at": "2026-09-24"},
             {"id": "f-2", "fact": "likes rum", "room": "B", "said_by_name": "Alice", "at": "2026-09-24"}]
    assert [f["id"] for f in in_room(facts, "A")] == ["f-1"]
    assert render(in_room(facts, "A")) == "- likes tea (said by Alice, 2026-09-24)"
    assert render(facts[:1], with_ids=True) == "- [f-1] likes tea (said by Alice, 2026-09-24)"
    assert render([]) == "- (nothing yet)"


def test_update_into_an_existing_fact_supersedes_instead_of_duplicating():
    mem = {"facts": [{"id": "f-1", "fact": "has a dog named Juni"}, {"id": "f-2", "fact": "Juni is 80% poodle"}]}
    out = apply(mem, [{"op": "update", "id": "f-1", "fact": "Juni is 80% poodle."}], room="r", episode=None, at="", taken=set())
    assert [f["id"] for f in out["facts"]] == ["f-2"]


def test_duplicates_ignore_articles_and_punctuation():
    mem = {"facts": [{"id": "f-1", "fact": "second 'D' in DDH stands for 'Devatman'"}]}
    out = apply(mem, [{"op": "add", "fact": "The second D in DDH stands for Devatman."}], room="r", episode=None, at="", taken=set())
    assert len(out["facts"]) == 1


def test_restatements_are_duplicates_but_more_detail_is_not():
    from walden.core.facts import restates
    assert restates("She has an older brother, Tomas, who is a surgeon in Vienna.", "has an older brother, Tomas, who is a surgeon in Vienna")
    assert restates("lives in Bratislava", "lives in Bratislava and spends summers in a cabin in the High Tatras")
    assert not restates("lives in Bratislava and spends summers in a cabin in the High Tatras", "lives in Bratislava")
    assert not restates("speaks Czech", "speaks Slovak")
    mem = apply({"facts": [{"id": "f-1", "fact": "has two sons, Leo (11) and Matej (7)"}]},
                [{"op": "add", "fact": "They have two sons, Leo (11) and Matej (7)."}], room="r", episode=None, at="", taken=set())
    assert len(mem["facts"]) == 1


def test_restatements_with_a_name_in_front_are_duplicates():
    from walden.core.facts import restates
    assert restates("Kovac was born in 1981 in Kosice, Slovakia", "born in 1981 in Kosice, Slovakia")
    assert restates("Kovac's father designed signal boxes", "father designed signal boxes, mother taught physics at a grammar school")
    assert not restates("believes every child should learn to read in the language spoken at home", "speaks Slovak, Czech, French, English")
    assert not restates("wants the tutor to support Romani as well as Slovak", "current project is a reading tutor for Roma children in eastern Slovakia")


def test_a_surname_in_front_does_not_hide_a_duplicate():
    from walden.core.facts import restates
    assert restates("Kovac lives in Bratislava", "lives in Bratislava and spends summers in a cabin in the High Tatras")
    assert restates("Kovac is vegetarian", "is vegetarian")
    assert not restates("Lantern Labs employs 23 people", "co-founded Lantern Labs in 2014")


def test_single_digits_make_facts_different():
    from walden.core.facts import restates
    assert not restates("has 2 sons", "has 3 sons")
    assert restates("has 3 sons", "has 3 sons and a cat")
