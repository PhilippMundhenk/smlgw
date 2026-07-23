import pytest

from smlgw.obis import ObisValue, format_obis, obis_name


def test_format_obis_six_bytes():
    assert format_obis(bytes([1, 0, 1, 8, 0, 255])) == "1-0:1.8.0*255"


def test_format_obis_five_bytes_defaults_f_255():
    assert format_obis(bytes([1, 0, 16, 7, 0])) == "1-0:16.7.0*255"


def test_format_obis_rejects_wrong_length():
    with pytest.raises(ValueError):
        format_obis(bytes([1, 2, 3]))


@pytest.mark.parametrize(
    "raw,scaler,expected",
    [
        (123456, -1, "12345.6"),   # pure scaling (non-energy unit)
        (100000, -1, "10000"),     # trailing zeros trimmed -> integer
        (23456, -1, "2345.6"),
        (550, -1, "55"),
        (4123, -1, "412.3"),
        (42, 0, "42"),
        (1000, -3, "1"),
        (12345, -2, "123.45"),
        (5, 3, "5000"),            # positive scaler
    ],
)
def test_value_to_string_matches_legacy_numeric(raw, scaler, expected):
    # Unit 27 (W) is not special-cased, so this tests pure scaling.
    v = ObisValue(code="1-0:16.7.0*255", raw_value=raw, scaler=scaler, unit_code=27)
    # Legacy gateway used .valueToString().split(" ")[0] -> numeric part only.
    assert v.value_to_string() == expected
    assert v.full_string().split(" ")[0] == expected


@pytest.mark.parametrize(
    "raw,scaler,expected",
    [
        (734512, -1, "73.4512"),   # smartmeter-obis divides Wh by 1000 -> kWh
        (500000, -1, "50"),
        (123456, -1, "12.3456"),
        (10000000, 0, "10000"),
    ],
)
def test_energy_unit30_is_divided_by_1000_kwh(raw, scaler, expected):
    v = ObisValue(code="1-0:1.8.0*255", raw_value=raw, scaler=scaler, unit_code=30)
    assert v.value_to_string() == expected
    assert v.unit == "kWh"
    assert v.full_string() == f"{expected} kWh"


def test_full_string_includes_unit():
    v = ObisValue(code="1-0:16.7.0*255", raw_value=4123, scaler=-1, unit_code=27)
    assert v.full_string() == "412.3 W"


def test_octet_value_non_ascii_renders_as_hex():
    v = ObisValue(code="1-0:96.1.0*255", raw_value=bytes.fromhex("0102ab"), unit_code=255)
    assert v.value_to_string() == "0102ab"


def test_octet_value_printable_ascii_renders_as_text():
    # Matches smartmeter-obis: all-printable buffers become text, not hex.
    v = ObisValue(code="1-0:96.1.0*255", raw_value=b"EMH00123", unit_code=255)
    assert v.value_to_string() == "EMH00123"


def test_obis_name_lookup_and_fallback():
    assert obis_name("1-0:1.8.0*255").startswith("Positive active energy total")
    assert obis_name("9-9:9.9.9*255") == "9-9:9.9.9*255"
