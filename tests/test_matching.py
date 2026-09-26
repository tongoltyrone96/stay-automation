from app.matching import key_tokens, match_lock

LISTINGS = [
    {"id": i, "name": name, "hostaway_name": name}
    for i, name in enumerate([
        "Maple 124b", "Pine Grove", "Maple 122", "CC6", "Birch Road 629",
        "Maple 109", "Maple 111", "Lake Tarn", "Maple 165", "cc1d", "CC3",
        "Riverside 1386 (ST2)", "Riverside 1376 (ST1)", "ValleyPines - 1 -2099", "Oak Lane",
        "Maple 1224",
    ], start=1)
]
BY_NAME = {l["name"]: l["id"] for l in LISTINGS}


def test_key_tokens_drop_noise_and_dates():
    assert key_tokens("Cedar 302 Encode 9 2026") == ["cedar", "302"]
    assert key_tokens("826 Sunset Jan26") == ["826", "sunset"]
    assert key_tokens("109 Maple Pkwy") == ["109", "maple"]


def test_real_lock_names_match_their_listing():
    cases = {
        "124 Maple": "Maple 124b",
        "Pine Grove New": "Pine Grove",
        "122 Maple New": "Maple 122",
        "Cc6 Schlage": "CC6",
        "629 Birch Encode": "Birch Road 629",
        "109 Maple Pkwy": "Maple 109",
        "Lake Tarn 9 26 Encode": "Lake Tarn",
        "CC1": "cc1d",
        "Cc3 New": "CC3",
        "1386 Riverside": "Riverside 1386 (ST2)",
    }
    for lock, listing in cases.items():
        assert match_lock(lock, LISTINGS) == BY_NAME[listing], lock


def test_number_prefix_does_not_match_longer_number():
    # "122" must match "Maple 122", not "Maple 1224".
    assert match_lock("122 Maple", LISTINGS) == BY_NAME["Maple 122"]


def test_address_matches_house_number_the_name_lacks():
    listings = [
        {"id": 1, "name": "Falling Water 1", "hostaway_name": "Falling Water 1", "address": "1200 Falling Water Drive"},
        {"id": 2, "name": "Falling Water 2", "hostaway_name": "Falling Water 2", "address": "1114 Falling Water Drive"},
        {"id": 3, "name": "MoonlitB", "hostaway_name": "MoonlitB", "address": "826 Moonlit Lane Northwest"},
        {"id": 4, "name": "Moonlight", "hostaway_name": "Moonlight", "address": None},
    ]
    assert match_lock("1114 Falling Water Front", listings) == 2
    assert match_lock("826 Moonlit Jan26", listings) == 3


def test_no_match_or_ambiguous_returns_none():
    assert match_lock("6805 Hill", LISTINGS) is None
    assert match_lock("Maple", LISTINGS) is None  # many Maples
    assert match_lock("New Encode Lock", LISTINGS) is None  # nothing left after noise
