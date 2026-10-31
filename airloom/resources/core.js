(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.AirloomCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function validateBounds(bounds) {
    if (!bounds || typeof bounds !== "object") throw new TypeError("Bounds must be an object");
    const { north, west, south, east } = bounds;
    for (const [name, value] of Object.entries({ north, west, south, east })) {
      if (!Number.isFinite(value)) throw new RangeError(`${name} must be finite`);
    }
    if (north < -90 || north > 90 || south < -90 || south > 90) {
      throw new RangeError("Latitudes must be between -90 and 90");
    }
    if (west < -180 || west > 180 || east < -180 || east > 180) {
      throw new RangeError("Longitudes must be between -180 and 180");
    }
    if (north < south) throw new RangeError("north must not be below south");
    return bounds;
  }

  function splitBounds(bounds) {
    validateBounds(bounds);
    const { north, west, south, east } = bounds;
    if (west <= east) return [bounds];
    return [
      { north, west, south, east: 180 },
      { north, west: -180, south, east },
    ];
  }

  function boundsContain(outer, inner) {
    const outerParts = splitBounds(outer);
    const innerParts = splitBounds(inner);
    return innerParts.every((part) => outerParts.some((candidate) => (
      part.north <= candidate.north && part.south >= candidate.south
      && part.west >= candidate.west && part.east <= candidate.east
    )));
  }

  function nearestLongitude(longitude, reference) {
    if (!Number.isFinite(reference)) throw new RangeError("reference must be finite");
    if (!Number.isFinite(longitude) || longitude < -180 || longitude > 180) {
      throw new RangeError("longitude must be finite and between -180 and 180");
    }
    return longitude + 360 * Math.round((reference - longitude) / 360);
  }

  const BANDS = [
    { pm_low: 0.0, pm_high: 9.0, aqi_low: 0, aqi_high: 50, label: "Good", color: "#35b779", foreground: "#08271b", guidance: "Air quality is satisfactory. It is a good time to be outside." },
    { pm_low: 9.1, pm_high: 35.4, aqi_low: 51, aqi_high: 100, label: "Moderate", color: "#f6c945", foreground: "#332400", guidance: "Unusually sensitive people may want to reduce prolonged outdoor exertion." },
    { pm_low: 35.5, pm_high: 55.4, aqi_low: 101, aqi_high: 150, label: "Unhealthy for sensitive groups", color: "#f39c3d", foreground: "#341900", guidance: "Sensitive groups should reduce prolonged or heavy outdoor exertion." },
    { pm_low: 55.5, pm_high: 125.4, aqi_low: 151, aqi_high: 200, label: "Unhealthy", color: "#e65b65", foreground: "#2a0509", guidance: "Everyone should reduce prolonged exertion; sensitive groups should avoid it." },
    { pm_low: 125.5, pm_high: 225.4, aqi_low: 201, aqi_high: 300, label: "Very unhealthy", color: "#9b6bc3", foreground: "#12061c", guidance: "Avoid prolonged outdoor exertion. Sensitive groups should remain indoors." },
    { pm_low: 225.5, pm_high: 325.4, aqi_low: 301, aqi_high: 500, label: "Hazardous", color: "#7e394d", foreground: "#ffffff", guidance: "Avoid outdoor activity and keep indoor air as clean as possible." },
  ];
  const UNAVAILABLE = { pm_low: 0, pm_high: 0, aqi_low: 0, aqi_high: 0, label: "Unavailable", color: "#7d8590", foreground: "#0b0e11", guidance: "No recent sensor reading is available." };

  function bandForAQI(aqi) {
    if (!Number.isFinite(aqi)) return UNAVAILABLE;
    for (const band of BANDS) {
      if (aqi <= band.aqi_high) return band;
    }
    return BANDS[BANDS.length - 1];
  }

  function summarySensors(sensors, view) {
    const parts = splitBounds(view);
    return sensors.filter((sensor) => {
      if (!sensor || sensor.remote_favorite === true || !Number.isFinite(sensor.aqi)) return false;
      const { latitude, longitude } = sensor;
      if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90) return false;
      if (!Number.isFinite(longitude) || longitude < -180 || longitude > 180) return false;
      return parts.some((part) => (
        latitude <= part.north && latitude >= part.south
        && longitude >= part.west && longitude <= part.east
      ));
    }).sort((a, b) => a.aqi - b.aqi);
  }

  return { splitBounds, boundsContain, nearestLongitude, bandForAQI, summarySensors, BANDS, UNAVAILABLE };
});
