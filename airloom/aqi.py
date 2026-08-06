"""US EPA PM2.5 AQI calculations and presentation metadata."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN


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


def truncate_pm25(value: float) -> float:
    """Truncate PM2.5 to one decimal place as required by EPA AQI guidance."""
    if not math.isfinite(value):
        return 0.0
    return float(Decimal(str(max(0.0, value))).quantize(Decimal("0.1"), rounding=ROUND_DOWN))


def aqi_from_pm25(pm25: float | None) -> int | None:
    """Convert a PM2.5 concentration in µg/m³ to the 0–500 US AQI scale."""
    if pm25 is None:
        return None
    concentration = truncate_pm25(pm25)
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


# The EPA extended (piecewise) correction switches from the US-wide linear fit
# to a quadratic above this CF=1 concentration, where the linear fit was only
# validated below it (Barkjohn et al. / EPA AirNow Fire and Smoke Map).
EPA_QUADRATIC_BREAKPOINT = 343.0
# Neutral relative humidity assumed when the sensor does not report one; raw
# CF=1 data over-reports roughly 2x, so an uncorrected fallback is worse.
DEFAULT_HUMIDITY = 50.0


def epa_corrected_pm25(cf1: float | None, humidity: float | None) -> float | None:
    """Apply the US EPA wildfire-smoke correction used for PurpleAir CF=1 data."""
    if cf1 is None or not math.isfinite(cf1):
        return None
    cf1 = max(0.0, cf1)
    if humidity is None or not math.isfinite(humidity):
        humidity = DEFAULT_HUMIDITY
    humidity = max(0.0, min(100.0, humidity))
    if cf1 < EPA_QUADRATIC_BREAKPOINT:
        corrected = 0.52 * cf1 - 0.086 * humidity + 5.75
    else:
        corrected = 0.46 * cf1 + 3.93e-4 * cf1 * cf1 + 2.97
    return max(0.0, corrected)

