"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

const corePath = require.resolve("../../airloom/resources/core.js");
const AirloomCore = require(corePath);

test("splitBounds splits crossing bounds at both antimeridian edges", () => {
  assert.deepEqual(
    AirloomCore.splitBounds({ north: 10, west: 179, south: -10, east: -179 }),
    [
      { north: 10, west: 179, south: -10, east: 180 },
      { north: 10, west: -180, south: -10, east: -179 },
    ],
  );
});

test("splitBounds returns noncrossing bounds as one piece", () => {
  const bounds = { north: 10, west: -20, south: -10, east: 20 };
  assert.deepEqual(AirloomCore.splitBounds(bounds), [bounds]);
});

test("splitBounds rejects non-finite, out-of-range, and inverted views", () => {
  const valid = { north: 10, west: -20, south: -10, east: 20 };
  for (const field of Object.keys(valid)) {
    for (const value of [NaN, Infinity, -Infinity]) {
      assert.throws(() => AirloomCore.splitBounds({ ...valid, [field]: value }));
    }
  }
  for (const [field, value] of [
    ["north", 90.1], ["north", -90.1], ["south", 90.1], ["south", -90.1],
    ["west", 180.1], ["west", -180.1], ["east", 180.1], ["east", -180.1],
  ]) {
    assert.throws(() => AirloomCore.splitBounds({ ...valid, [field]: value }));
  }
  assert.throws(() => AirloomCore.splitBounds({ north: -10, west: -20, south: 10, east: 20 }));
});

test("nearestLongitude projects into the nearest world on both sides", () => {
  assert.equal(AirloomCore.nearestLongitude(-179, 179), 181);
  assert.equal(AirloomCore.nearestLongitude(179, -179), -181);
  assert.equal(AirloomCore.nearestLongitude(170, -170), -190);
});

test("nearestLongitude supports unwrapped references and deterministic ties", () => {
  assert.equal(AirloomCore.nearestLongitude(-179, 540), 541);
  assert.equal(AirloomCore.nearestLongitude(179, -540), -541);
  assert.equal(AirloomCore.nearestLongitude(0, 180), 360);
  assert.equal(AirloomCore.nearestLongitude(0, -180), 0);
});

test("nearestLongitude rejects non-finite or noncanonical longitudes", () => {
  for (const [longitude, reference] of [
    [NaN, 0], [Infinity, 0], [180.1, 0], [-180.1, 0], [0, NaN], [0, Infinity],
  ]) {
    assert.throws(() => AirloomCore.nearestLongitude(longitude, reference));
  }
});

test("bandForAQI uses all six EPA bands at their exact boundaries", () => {
  const cases = [
    [0, "Good", "#35b779"],
    [50, "Good", "#35b779"],
    [51, "Moderate", "#f6c945"],
    [100, "Moderate", "#f6c945"],
    [101, "Unhealthy for sensitive groups", "#f39c3d"],
    [150, "Unhealthy for sensitive groups", "#f39c3d"],
    [151, "Unhealthy", "#e65b65"],
    [200, "Unhealthy", "#e65b65"],
    [201, "Very unhealthy", "#9b6bc3"],
    [300, "Very unhealthy", "#9b6bc3"],
    [301, "Hazardous", "#7e394d"],
    [500, "Hazardous", "#7e394d"],
  ];
  for (const [aqi, label, color] of cases) {
    const band = AirloomCore.bandForAQI(aqi);
    assert.equal(band.label, label, `AQI ${aqi}`);
    assert.equal(band.color, color, `AQI ${aqi}`);
  }
});

test("bandForAQI keeps unavailable and above-scale behavior compatible", () => {
  for (const aqi of [null, undefined, NaN, Infinity]) {
    const band = AirloomCore.bandForAQI(aqi);
    assert.equal(band.label, "Unavailable");
    assert.equal(band.color, "#7d8590");
    assert.equal(band.foreground, "#ffffff");
  }
  assert.equal(AirloomCore.bandForAQI(501).label, "Hazardous");
});

test("summarySensors includes both sides of a crossing view in median order", () => {
  const sensors = [
    { id: "east", latitude: 0, longitude: -175, aqi: 100 },
    { id: "gap", latitude: 0, longitude: 0, aqi: 25 },
    { id: "west", latitude: 0, longitude: 175, aqi: 50 },
    { id: "north", latitude: 11, longitude: 175, aqi: 75 },
    { id: "unavailable", latitude: 0, longitude: -175, aqi: null },
    { id: "nonfinite", latitude: 0, longitude: 175, aqi: NaN },
  ];
  const view = { north: 10, west: 170, south: -10, east: -170 };
  assert.deepEqual(
    AirloomCore.summarySensors(sensors, view).map((sensor) => sensor.id),
    ["west", "east"],
  );
});

test("summarySensors excludes only explicit remote favorites, not favorites or truthy flags", () => {
  const sensors = [
    { id: "local", latitude: 0, longitude: 0, aqi: 50 },
    { id: "remote", latitude: 0, longitude: 0, aqi: 1, favorite: true, remote_favorite: true },
    { id: "favorite", latitude: 0, longitude: 0, aqi: 100, favorite: true },
    { id: "truthy", latitude: 0, longitude: 0, aqi: 75, remote_favorite: 1 },
    { id: "distant", latitude: 0, longitude: 100, aqi: 2, favorite: true },
  ];
  const view = { north: 10, west: -10, south: -10, east: 10 };
  assert.deepEqual(
    AirloomCore.summarySensors(sensors, view).map((sensor) => sensor.id),
    ["local", "truthy", "favorite"],
  );
});

test("summarySensors drops invalid coordinates and validates the view", () => {
  const view = { north: 10, west: -10, south: -10, east: 10 };
  const sensors = [
    { id: "valid", latitude: 0, longitude: 0, aqi: 50 },
    { id: "lat", latitude: 91, longitude: 0, aqi: 1 },
    { id: "lon", latitude: 0, longitude: -181, aqi: 1 },
    { id: "nan", latitude: NaN, longitude: 0, aqi: 1 },
  ];
  assert.deepEqual(AirloomCore.summarySensors(sensors, view).map((sensor) => sensor.id), ["valid"]);
  assert.throws(() => AirloomCore.summarySensors(sensors, { ...view, east: Infinity }));
});

test("core exposes the same API as a direct-file browser global", () => {
  const context = {};
  vm.runInNewContext(fs.readFileSync(corePath, "utf8"), context, { filename: "core.js" });
  assert.equal(typeof context.AirloomCore.splitBounds, "function");
  assert.equal(typeof context.AirloomCore.summarySensors, "function");
  assert.equal(context.AirloomCore.nearestLongitude(-179, 179), 181);
});
