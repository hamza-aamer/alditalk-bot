import pytest

from alditalk.parsing import fmt_mb, parse_de_number, parse_volume_text, pick_data_usage


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("25", 25.0),
        ("0,85", 0.85),
        ("1.024,5", 1024.5),
        ("1.024", 1024.0),
        ("12.345.678", 12345678.0),
        ("1.5", 1.5),
        ("0.850", 0.85),
        ("850,", 850.0),
    ],
)
def test_parse_de_number(raw, expected):
    assert parse_de_number(raw) == pytest.approx(expected)


def test_parse_volume_mb_of_gb():
    u = parse_volume_text("850,5 MB von 25 GB Inland")
    assert u.remaining_mb == pytest.approx(850.5)
    assert u.total_mb == pytest.approx(25 * 1024)


def test_parse_volume_gb_with_nbsp_and_newlines():
    u = parse_volume_text("Noch\n12,34\xa0GB\nvon 50 GB\nInland & EU")
    assert u.remaining_mb == pytest.approx(12.34 * 1024)
    assert u.total_mb == pytest.approx(50 * 1024)


def test_parse_volume_no_match():
    assert parse_volume_text("Dein Guthaben: 5,00 €") is None
    assert parse_volume_text("") is None


def test_pick_prefers_inland():
    texts = ["Guthaben 3,00 €", "500 MB von 1 GB EU-Roaming", "2,5 GB von 25 GB Inland"]
    idx, u = pick_data_usage(texts)
    assert idx == 2
    assert u.remaining_mb == pytest.approx(2.5 * 1024)


def test_pick_falls_back_to_first_match():
    idx, u = pick_data_usage(["nothing", "700 MB von 25 GB"])
    assert idx == 1 and u.remaining_mb == 700


def test_pick_none():
    assert pick_data_usage(["a", "b"]) is None


def test_fmt_mb():
    assert fmt_mb(512) == "512 MB"
    assert fmt_mb(2048) == "2.00 GB"
    assert fmt_mb(None) == "?"


# --- offers API (structure from recon 2026-11-01, values synthetic) ---------------------------
from alditalk.parsing import describe_packs, usage_from_offers  # noqa: E402

OFFERS = {
    "subscribedOffers": [
        {
            "offerName": "Tarif M",
            "status": "active",
            "isOnDemandRefillApplicable": True,
            "pack": [
                {
                    "unit": "kilobytes",
                    "type": "data",
                    "used": 5565820.0,
                    "allocated": 5.24288e7,
                    "balanceAttributeReference": "dataGrantAmount",
                },
                {
                    "unit": "kilobytes",
                    "type": "data",
                    "used": 0.0,
                    "allocated": 2.4117248e7,
                    "balanceAttributeReference": "dataGrantAmountFUP",
                },
            ],
            "unlimitedUsage": {
                "includedDataVolume": [
                    {"allowanceParameter": "dataGrantAmount", "countryGroup": "germanyOnly"},
                    {"allowanceParameter": "dataGrantAmountFUP", "countryGroup": "euOnly"},
                ]
            },
        }
    ]
}


def test_offers_inland_only():
    u = usage_from_offers(OFFERS)
    assert u.source == "api"
    assert u.total_mb == pytest.approx(50 * 1024)
    assert u.remaining_mb == pytest.approx((52428800 - 5565820) / 1024)  # ~44.69 GB, like the page


def test_offers_extra_refill_pack_is_added():
    import copy

    data = copy.deepcopy(OFFERS)
    data["subscribedOffers"][0]["pack"].append(
        {
            "unit": "kilobytes",
            "type": "data",
            "used": 0.0,
            "allocated": 1048576.0,
            "balanceAttributeReference": "onDemandRefill",
        }
    )
    assert usage_from_offers(data).remaining_mb == pytest.approx((52428800 - 5565820 + 1048576) / 1024)


def test_offers_inactive_or_garbage():
    assert usage_from_offers(None) is None
    assert (
        usage_from_offers({"subscribedOffers": [{"status": "inactive", "pack": OFFERS["subscribedOffers"][0]["pack"]}]})
        is None
    )
    assert (
        usage_from_offers(
            {"subscribedOffers": [{"pack": [{"type": "data", "unit": "parsecs", "used": 1, "allocated": 2}]}]}
        )
        is None
    )


def test_describe_packs_has_no_personal_fields():
    packs = describe_packs(OFFERS)
    assert len(packs) == 2
    assert set(packs[0]) == {"balanceAttributeReference", "unit", "allocated", "used"}


def test_pick_never_uses_roaming_tile():
    tiles = [
        "Noch\n23 GB\nvon 23 GB\nRoaming EU",
        "Unbegrenzt\nMin.\nInland & EU",
    ]
    assert pick_data_usage(tiles) is None


def test_pick_real_tile_texts():
    tiles = [
        "Noch\n44,7 GB\nvon 50 GB\nInland\n1 GB",
        "Noch\n23 GB\nvon 23 GB\nRoaming EU",
        "Unbegrenzt\nMin.\nInland & EU",
        "Unbegrenzt\nSMS\nInland & EU",
    ]
    idx, u = pick_data_usage(tiles)
    assert idx == 0
    assert u.remaining_mb == pytest.approx(44.7 * 1024)
