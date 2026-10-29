"""US EPA PM2.5 AQI calculations and presentation metadata."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN


@dataclass(frozen=True, slots=True)
class AQIBand:
    pm_low: float
    pm_high: float
    aqi_low: int
    aqi_high: int
    label: str
    color: str
    foreground: str
    guidance: str


# US EPA 2024 PM2.5 breakpoints. Concentrations above 325.4 report a flat 500,
# the top of the public AQI scale.
BANDS = (
    AQIBand(0.0, 9.0, 0, 50, "Good", "#35b779", "#08271b", "Air quality is satisfactory. It is a good time to be outside."),
    AQIBand(9.1, 35.4, 51, 100, "Moderate", "#f6c945", "#332400", "Unusually sensitive people may want to reduce prolonged outdoor exertion."),
    AQIBand(35.5, 55.4, 101, 150, "Unhealthy for sensitive groups", "#f39c3d", "#341900", "Sensitive groups should reduce prolonged or heavy outdoor exertion."),
    AQIBand(55.5, 125.4, 151, 200, "Unhealthy", "#e65b65", "#ffffff", "Everyone should reduce prolonged exertion; sensitive groups should avoid it."),
    AQIBand(125.5, 225.4, 201, 300, "Very unhealthy", "#9b6bc3", "#ffffff", "Avoid prolonged outdoor exertion. Sensitive groups should remain indoors."),
    AQIBand(225.5, 325.4, 301, 500, "Hazardous", "#7e394d", "#ffffff", "Avoid outdoor activity and keep indoor air as clean as possible."),
)


def truncate_pm25(value: float | None) -> float | None:
    """Truncate finite PM2.5 to one decimal place as required by EPA guidance."""
    if value is None or not math.isfinite(value):
        return None
    value = max(0.0, value)
    try:
        return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_DOWN))
    except InvalidOperation:
        # Floats large enough to exceed Decimal's default quantize precision
        # have no fractional digits to truncate.
        return value


def aqi_from_pm25(pm25: float | None) -> int | None:
    """Convert a PM2.5 concentration in µg/m³ to the 0–500 US AQI scale."""
    concentration = truncate_pm25(pm25)
    if concentration is None:
        return None
    for band in BANDS:
        if concentration <= band.pm_high:
            value = (
                (band.aqi_high - band.aqi_low)
                / (band.pm_high - band.pm_low)
                * (concentration - band.pm_low)
                + band.aqi_low
            )
            return max(0, min(500, round(value)))
    return 500


def band_for_aqi(aqi: int | None) -> AQIBand:
    if aqi is None:
        return AQIBand(0, 0, 0, 0, "Unavailable", "#7d8590", "#ffffff", "No recent sensor reading is available.")
    for band in BANDS:
        if aqi <= band.aqi_high:
            return band
    return BANDS[-1]


CORRECTION_VERSION = 2

# Neutral relative humidity assumed when the sensor does not report one; raw
# data over-reports roughly 2x, so an uncorrected fallback is worse.
DEFAULT_HUMIDITY = 50.0


def _cf1_corrected(value: float, humidity: float) -> float:
    """Barkjohn et al. (2022), §3.1.3, final equations 1–3.

    https://pmc.ncbi.nlm.nih.gov/articles/PMC9784900/#sec3dot1dot3-sensors-22-09669
    """
    linear = 0.524 * value - 0.0862 * humidity + 5.75
    if value < 570.0:
        return linear
    quadratic = 4.21e-4 * value * value + 0.392 * value + 3.44
    if value >= 611.0:
        return quadratic
    weight = 0.0244 * value - 13.9
    return weight * quadratic + (1.0 - weight) * linear


def _atm_corrected(value: float, humidity: float) -> float:
    """Five-piece CF=ATM correction published in PurpleAir API post 6.

    https://community.purpleair.com/t/is-there-a-field-that-returns-data-with-us-epa-pm2-5-conversion-formula-applied/4593/6
    """
    if value < 30.0:
        return 0.524 * value - 0.0862 * humidity + 5.75
    if value < 50.0:
        weight = value / 20.0 - 3.0 / 2.0
        slope = 0.786 * weight + 0.524 * (1.0 - weight)
        return slope * value - 0.0862 * humidity + 5.75
    if value < 210.0:
        return 0.786 * value - 0.0862 * humidity + 5.75
    if value < 260.0:
        weight = value / 50.0 - 21.0 / 5.0
        slope = 0.69 * weight + 0.786 * (1.0 - weight)
        return (
            slope * value
            - 0.0862 * humidity * (1.0 - weight)
            + 2.966 * weight
            + 5.75 * (1.0 - weight)
            + 8.84e-4 * value * value * weight
        )
    return 2.966 + 0.69 * value + 8.84e-4 * value * value


def epa_corrected_pm25(
    value: float | None,
    humidity: float | None,
    basis: str = "CF1",
) -> float | None:
    """Apply the US EPA correction for a PurpleAir CF1 or ATM measurement."""
    if basis not in ("CF1", "ATM"):
        raise ValueError("basis must be CF1 or ATM")
    if value is None or not math.isfinite(value):
        return None
    value = max(0.0, value)
    if humidity is None or not math.isfinite(humidity):
        humidity = DEFAULT_HUMIDITY
    humidity = max(0.0, min(100.0, humidity))
    corrected = _cf1_corrected(value, humidity) if basis == "CF1" else _atm_corrected(value, humidity)
    return max(0.0, corrected) if math.isfinite(corrected) else None

