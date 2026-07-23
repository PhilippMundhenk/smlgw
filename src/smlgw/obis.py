"""OBIS code handling and value formatting.

An OBIS identifier addresses a single measured quantity.  On the wire SML
carries it as six bytes ``A B C D E F`` which are conventionally rendered as
``A-B:C.D.E*F`` (for example ``1-0:1.8.0*255`` for the positive active energy
total).  The legacy Node gateway obtained the numeric payload it published to
MQTT via ``obisResult["1-0:1.8.0*255"].valueToString().split(" ")[0]`` -- i.e.
the scaled value, without the unit.  :class:`ObisValue` reproduces exactly that
formatting so the MQTT payloads stay byte-for-byte compatible.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# DLMS unit enumeration (IEC 62056-6-2), limited to what smart meters emit.
DLMS_UNITS: dict[int, str] = {
    1: "a", 2: "mo", 3: "wk", 4: "d", 5: "h", 6: "min", 7: "s",
    8: "°", 9: "°C", 10: "currency", 11: "m", 12: "m/s", 13: "m³",
    14: "m³", 15: "m³/h", 16: "m³/h", 17: "m³/d", 18: "m³/d", 19: "l",
    20: "kg", 21: "N", 22: "Nm", 23: "Pa", 24: "bar", 25: "J", 26: "J/h",
    27: "W", 28: "VA", 29: "var", 30: "Wh", 31: "VAh", 32: "varh",
    33: "A", 34: "C", 35: "V", 36: "V/m", 37: "F", 38: "Ω", 39: "Ωm²/m",
    40: "Wb", 41: "T", 42: "A/m", 43: "H", 44: "Hz", 45: "1/(Wh)",
    46: "1/(varh)", 47: "1/(VAh)", 48: "V²h", 49: "A²h", 50: "kg/s",
    51: "S", 52: "K", 53: "1/(V²h)", 54: "1/(A²h)", 55: "1/m³", 56: "%",
    57: "Ah", 60: "Wh/m³", 61: "J/m³", 62: "Mol%", 63: "g/m³",
    64: "Pa·s", 65: "J/kg", 70: "dBm", 71: "dBµV", 72: "dB",
    254: "", 255: "",
}

# Human readable names for the OBIS codes commonly present on residential
# electricity meters.  Used only to make the web UI friendlier; unknown codes
# are still fully usable.
OBIS_NAMES: dict[str, str] = {
    "1-0:1.8.0*255": "Positive active energy total (A+)",
    "1-0:1.8.1*255": "Positive active energy tariff 1 (A+ T1)",
    "1-0:1.8.2*255": "Positive active energy tariff 2 (A+ T2)",
    "1-0:2.8.0*255": "Negative active energy total (A-)",
    "1-0:2.8.1*255": "Negative active energy tariff 1 (A- T1)",
    "1-0:2.8.2*255": "Negative active energy tariff 2 (A- T2)",
    "1-0:16.7.0*255": "Sum active instantaneous power (A+ - A-)",
    "1-0:36.7.0*255": "Active instantaneous power L1",
    "1-0:56.7.0*255": "Active instantaneous power L2",
    "1-0:76.7.0*255": "Active instantaneous power L3",
    "1-0:32.7.0*255": "Voltage L1",
    "1-0:52.7.0*255": "Voltage L2",
    "1-0:72.7.0*255": "Voltage L3",
    "1-0:31.7.0*255": "Current L1",
    "1-0:51.7.0*255": "Current L2",
    "1-0:71.7.0*255": "Current L3",
    "1-0:14.7.0*255": "Frequency",
    "1-0:0.0.0*255": "Meter address 1",
    "1-0:0.0.9*255": "Server ID / device identity",
    "1-0:96.1.0*255": "Meter serial number",
    "1-0:96.5.0*255": "Meter status word",
}


def format_obis(raw: bytes) -> str:
    """Format the six OBIS bytes as ``A-B:C.D.E*F``.

    Real meters occasionally emit a five byte object name (no ``F`` group).  In
    that case ``F`` defaults to ``255`` which matches the smartmeter-obis
    behaviour and the topic keys used by the legacy gateway.
    """
    if len(raw) == 5:
        a, b, c, d, e = raw
        f = 255
    elif len(raw) == 6:
        a, b, c, d, e, f = raw
    else:
        raise ValueError(f"OBIS object name must be 5 or 6 bytes, got {len(raw)}")
    return f"{a}-{b}:{c}.{d}.{e}*{f}"


def obis_name(code: str) -> str:
    """Return a human readable label for *code*, or the code itself."""
    return OBIS_NAMES.get(code, code)


# DLMS unit code for active energy in watt-hours. The legacy smartmeter-obis
# library special-cased this: it divides the value by 1000 and reports kWh.
UNIT_WH = 30

_TEN = Decimal(10)


def _scaled_decimal(raw_value: int | float, scaler: int) -> Decimal:
    """Scale a register value by ``10**scaler`` exactly."""
    dec = raw_value if isinstance(raw_value, int) else Decimal(str(raw_value))
    dec = Decimal(dec)
    if scaler >= 0:
        return dec * (_TEN ** scaler)
    return dec / (_TEN ** (-scaler))


def _clean10(value: Decimal) -> Decimal:
    """Mimic JS ``parseFloat(value.toFixed(10))`` — round to 10 dp, drop noise."""
    return value.quantize(Decimal("1e-10"), rounding=ROUND_HALF_UP)


def _format_number(value: Decimal) -> str:
    """Render *value* the way the legacy gateway published it.

    Integers stay integers ("12345"); scaled values keep their fractional
    digits without exponent notation or trailing zeros ("12345.6").
    """
    normalized = _clean10(value).normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _octet_to_string(raw: bytes) -> str:
    """Printable-ASCII buffers become text; everything else becomes hex.

    This matches smartmeter-obis, which rendered buffers whose bytes are all in
    0x20-0x7e as ``buf.toString()`` and the rest as ``buf.toString('hex')``.
    """
    if raw and all(0x20 <= b <= 0x7E for b in raw):
        return raw.decode("ascii")
    return raw.hex()


@dataclass(frozen=True)
class ObisValue:
    """A single decoded OBIS entry from an SML list."""

    code: str
    raw_value: object
    scaler: int = 0
    unit_code: int = 255
    status: int | None = None
    value_time: int | None = None

    @property
    def unit(self) -> str:
        # Energy (Wh) is reported as kWh because the value is divided by 1000.
        if self.unit_code == UNIT_WH:
            return "kWh"
        return DLMS_UNITS.get(self.unit_code, "")

    @property
    def is_numeric(self) -> bool:
        return isinstance(self.raw_value, (int, float)) and not isinstance(self.raw_value, bool)

    def _scaled(self) -> Decimal:
        scaled = _scaled_decimal(self.raw_value, self.scaler)
        if self.unit_code == UNIT_WH:
            scaled = scaled / Decimal(1000)
        return scaled

    @property
    def value(self) -> object:
        """The final scaled value (Wh→kWh applied); non-numeric passes through."""
        if self.is_numeric:
            return _clean10(self._scaled())
        return self.raw_value

    def numeric_value(self) -> float | None:
        """The published value as a float, or ``None`` for non-numeric readings."""
        if self.is_numeric:
            return float(self.value)
        return None

    def value_to_string(self) -> str:
        """Numeric/string value only, matching ``.valueToString().split(" ")[0]``."""
        if self.is_numeric:
            return _format_number(self._scaled())
        if isinstance(self.raw_value, (bytes, bytearray)):
            return _octet_to_string(bytes(self.raw_value))
        return str(self.raw_value)

    def full_string(self) -> str:
        """Value with unit, matching smartmeter-obis ``valueToString()``."""
        base = self.value_to_string()
        return f"{base} {self.unit}".rstrip()

    @property
    def name(self) -> str:
        return obis_name(self.code)
