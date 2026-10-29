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

test("boundsContain compares crossing and full-world geographic coverage", () => {
  const crossing = { north: 20, west: 170, south: -20, east: -170 };
  assert.equal(AirloomCore.boundsContain(crossing, { north: 10, west: 175, south: -10, east: 179 }), true);
  assert.equal(AirloomCore.boundsContain(crossing, { north: 10, west: -179, south: -10, east: -175 }), true);
  assert.equal(AirloomCore.boundsContain(crossing, { north: 10, west: 175, south: -10, east: -175 }), true);
  assert.equal(AirloomCore.boundsContain(crossing, { north: 10, west: -169, south: -10, east: -160 }), false);
  const world = { north: 85, west: -180, south: -85, east: 180 };
  assert.equal(AirloomCore.boundsContain(world, crossing), true);
  assert.equal(AirloomCore.boundsContain(crossing, world), false);
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

const path = require("node:path");
const indexPath = require.resolve("../../airloom/resources/index.html");

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...names) { for (const name of names) this.values.add(name); }
  remove(...names) { for (const name of names) this.values.delete(name); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) {
    const enabled = force === undefined ? !this.values.has(name) : Boolean(force);
    enabled ? this.values.add(name) : this.values.delete(name);
    return enabled;
  }
}

class FakeElement {
  constructor(tagName = "div", id = "") {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.hidden = false;
    this.open = false;
    this.value = "";
    this.checked = false;
    this.clientWidth = 0;
    this.clientHeight = 0;
    this.dataset = {};
    this.style = { setProperty: (name, value) => { this.style[name] = value; } };
    this.classList = new FakeClassList();
    this.listeners = new Map();
    this.childNodes = [];
    this.isConnected = true;
    this.elements = {};
    this._innerHTML = "";
    this._markers = [];
    this.lastImage = null;
  }

  set className(value) {
    this._className = value;
    this.classList = new FakeClassList();
    for (const name of String(value).split(/\s+/).filter(Boolean)) this.classList.add(name);
  }
  get className() { return this._className || ""; }
  set textContent(value) {
    this._textContent = String(value);
    if (value === "") this.childNodes = [];
  }
  get textContent() { return this._textContent || ""; }

  set innerHTML(value) {
    this._innerHTML = String(value);
    this.childNodes = [];
    this._markers = [];
    if (this.id !== "markers") return;
    for (const match of this._innerHTML.matchAll(/<button\b([^>]*)>/g)) {
      const marker = new FakeElement("button");
      for (const attribute of match[1].matchAll(/([:\w-]+)="([^"]*)"/g)) {
        const [, name, value] = attribute;
        if (name === "class") marker.className = value;
        if (name === "style") {
          for (const declaration of value.split(";")) {
            const [property, setting] = declaration.split(":");
            if (property && setting !== undefined) marker.style[property] = setting;
          }
        }
        if (name.startsWith("data-")) {
          const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
          marker.dataset[key] = value;
        }
      }
      this._markers.push(marker);
    }
  }
  get innerHTML() { return this._innerHTML || this.childNodes.map((child) => child.outerHTML).join(""); }
  get outerHTML() {
    const tag = this.tagName.toLowerCase();
    const classes = this.className ? ` class="${this.className}"` : "";
    const data = Object.entries(this.dataset).map(([name, value]) => ` data-${name.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}="${value}"`).join("");
    return `<${tag}${classes}${data}>${this._innerHTML}</${tag}>`;
  }

  get firstChild() { return this.childNodes[0] || null; }
  get children() { return this.childNodes; }
  appendChild(child) { child.parentNode = this; this.childNodes.push(child); return child; }
  insertBefore(child, reference) {
    if (child.parentNode) {
      const siblings = child.parentNode.childNodes;
      const index = siblings.indexOf(child);
      if (index >= 0) siblings.splice(index, 1);
    }
    child.parentNode = this;
    child.isConnected = true;
    const index = reference === null ? -1 : this.childNodes.indexOf(reference);
    if (index < 0) this.childNodes.push(child);
    else this.childNodes.splice(index, 0, child);
    return child;
  }
  remove() {
    this.isConnected = false;
    if (this.parentNode) {
      const index = this.parentNode.childNodes.indexOf(this);
      if (index >= 0) this.parentNode.childNodes.splice(index, 1);
    }
  }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  dispatch(type, properties = {}) {
    const event = {
      type,
      target: properties.target || this,
      currentTarget: this,
      preventDefault() {},
      stopPropagation() {},
      ...properties,
    };
    for (const listener of this.listeners.get(type) || []) listener(event);
  }
  click() { this.dispatch("click"); }
  focus() {}
  setAttribute(name, value) { this[name] = String(value); }
  setPointerCapture() {}
  getBoundingClientRect() {
    return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight };
  }
  closest() { return null; }
  matches(selector) {
    if (selector.startsWith(".")) return selector.slice(1).split(".").every((name) => this.classList.contains(name));
    return this.tagName === selector.toUpperCase();
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const matches = [];
    const visit = (node) => {
      for (const child of node.childNodes) {
        if (child.matches(selector)) matches.push(child);
        visit(child);
      }
    };
    visit(this);
    return matches;
  }
  getContext() {
    if (this.tagName !== "CANVAS") return null;
    return {
      createImageData: (width, height) => ({
        width,
        height,
        data: new Uint8ClampedArray(width * height * 4),
      }),
      putImageData: (image) => { this.lastImage = image; },
      clearRect: () => { this.lastImage = null; },
    };
  }
}

function loadApp({ preview = false } = {}) {
  const html = fs.readFileSync(indexPath, "utf8");
  const elements = new Map();
  for (const match of html.matchAll(/<([a-z][\w-]*)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const element = new FakeElement(match[1], match[2]);
    element.hidden = /\shidden(?:\s|>)/.test(match[0]);
    if (element.id === "map-panel") {
      element.clientWidth = 512;
      element.clientHeight = 512;
    }
    elements.set(element.id, element);
  }

  const document = new FakeElement("document");
  document.body = new FakeElement("body");
  document.documentElement = new FakeElement("html");
  document.querySelector = (selector) => selector.startsWith("#") ? elements.get(selector.slice(1)) || null : null;
  document.querySelectorAll = () => [];
  document.createElement = (tagName) => new FakeElement(tagName);
  document.elementFromPoint = () => null;

  const timers = new Map();
  const animationFrames = new Map();
  const messages = [];
  let nextTimer = 1;
  let nextAnimationFrame = 1;
  const context = {
    console,
    document,
    Uint8ClampedArray,
    Math,
    Number,
    String,
    Boolean,
    Map,
    Set,
    Date,
    JSON,
    performance: { now: () => 0 },
    requestAnimationFrame: (callback) => {
      const id = nextAnimationFrame++;
      animationFrames.set(id, callback);
      return id;
    },
    cancelAnimationFrame: (id) => animationFrames.delete(id),
    setTimeout: (callback) => {
      const id = nextTimer++;
      timers.set(id, callback);
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
    ResizeObserver: class { observe() {} },
    Event: class {},
    KeyboardEvent: class {},
    PointerEvent: class {},
    FormData: class {},
    ...(preview ? {} : {
      webkit: {
        messageHandlers: {
          airloom: { postMessage: (message) => messages.push(JSON.parse(message)) },
        },
      },
    }),
  };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  for (const match of html.matchAll(/<script\s+src="([^"]+)"[^>]*><\/script>/g)) {
    const scriptPath = path.resolve(path.dirname(indexPath), match[1]);
    vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, { filename: path.basename(scriptPath) });
  }

  return {
    context,
    elements,
    messages,
    runTimers() {
      const pending = [...timers.values()];
      timers.clear();
      for (const callback of pending) callback();
    },
    runAnimationFrames(now) {
      const pending = [...animationFrames.values()];
      animationFrames.clear();
      for (const callback of pending) callback(now);
    },
  };
}

function fixtureSensor(id, longitude, aqi, extra = {}) {
  return {
    id,
    name: `Sensor ${id}`,
    latitude: 0,
    longitude,
    aqi,
    pm25: null,
    pm10: null,
    temperature_f: null,
    humidity: null,
    last_seen: null,
    favorite: false,
    indoor: false,
    origin: "live",
    delivery: "network",
    category: "Fixture",
    color: "#7d8590",
    foreground: "#ffffff",
    guidance: "Fixture guidance",
    trend: [],
    ...extra,
  };
}

function configureMap(app, longitude, extra = {}) {
  app.context.Airloom.receive("config", {
    latitude: 0,
    longitude,
    location_name: "Test area",
    heatmap_threshold_km: 40,
    ...extra,
  });
}

function closeTo(actual, expected, epsilon = 0.000001) {
  assert.ok(Math.abs(actual - expected) < epsilon, `${actual} was not close to ${expected}`);
}

test("markers and popups project sensors into the center's nearest world", () => {
  const app = loadApp();
  configureMap(app, 179.9, { heatmap_threshold_km: 1000 });
  app.context.Airloom.debugZoomTo(8);
  app.context.Airloom.receive("sensors", {
    items: [fixtureSensor(1, -179.9, 151)],
    selected_id: 1,
    source: "PurpleAir live",
  });

  const marker = app.elements.get("markers").querySelectorAll(".map-marker")[0];
  assert.ok(marker, "the sensor across the antimeridian should have a visible marker");
  const expectedX = (180.1 + 180) / 360 * 256 * 2 ** 8;
  closeTo(Number.parseFloat(marker.style.left), expectedX);
  marker.dispatch("click");
  closeTo(Number.parseFloat(app.elements.get("map-popup").style.left), expectedX);
  assert.equal(app.elements.get("map-popup").hidden, false);
});

function heatColors(app, aqi, longitude) {
  app.context.Airloom.receive("sensors", {
    items: [fixtureSensor(1, longitude, aqi)],
    selected_id: 1,
    source: "PurpleAir live",
  });
  const image = app.elements.get("heatmap").lastImage;
  assert.ok(image, `AQI ${aqi} should produce a heat image`);
  const colors = new Set();
  for (let offset = 0; offset < image.data.length; offset += 4) {
    if (image.data[offset + 3]) colors.add(JSON.stringify([...image.data.slice(offset, offset + 3)]));
  }
  return colors;
}

test("heat-map points project into the center's nearest world", () => {
  const app = loadApp();
  configureMap(app, 179.9);
  app.context.Airloom.debugZoomTo(8);
  const colors = heatColors(app, 50, -179.9);
  assert.ok(colors.has(JSON.stringify([53, 183, 121])), `nearest-world heat point was missing; got ${[...colors]}`);
});

test("heat-map colors use all six AQI bands at exact boundaries", () => {
  const app = loadApp();
  configureMap(app, 179.9);
  app.context.Airloom.debugZoomTo(8);
  const cases = [
    [50, [53, 183, 121]],
    [51, [246, 201, 69]],
    [100, [246, 201, 69]],
    [101, [243, 156, 61]],
    [150, [243, 156, 61]],
    [151, [230, 91, 101]],
    [200, [230, 91, 101]],
    [201, [155, 107, 195]],
    [300, [155, 107, 195]],
    [301, [126, 57, 77]],
  ];
  for (const [aqi, expected] of cases) {
    const colors = heatColors(app, aqi, 179.9);
    assert.ok(colors.has(JSON.stringify(expected)), `AQI ${aqi} did not produce ${expected}; got ${[...colors]}`);
  }
});

test("dragging preserves an unwrapped center while view payloads use crossing bounds", () => {
  const app = loadApp();
  configureMap(app, 179.9);
  app.context.Airloom.debugZoomTo(8);
  const panel = app.elements.get("map-panel");
  panel.dispatch("pointerdown", { clientX: 256, clientY: 256, pointerId: 1, buttons: 1 });
  panel.dispatch("pointermove", { clientX: 0, clientY: 256, pointerId: 1, buttons: 1 });
  panel.dispatch("pointerup", { clientX: 0, clientY: 256, pointerId: 1, buttons: 0 });
  app.runTimers();

  const center = app.context.Airloom.debugState().center;
  closeTo(center.lon, 181.30625);
  const message = app.messages.filter((item) => item.action === "view-changed").at(-1);
  assert.ok(message, "the drag should emit a view-changed payload");
  closeTo(message.west, 179.9);
  closeTo(message.east, -177.2875);
  closeTo(message.lon, -178.69375);
  assert.ok(message.west > message.east, "crossing bounds must remain explicit");
  assert.equal(AirloomCore.splitBounds(message).length, 2);
});

test("a full-world viewport emits full-world bounds and includes visible summaries", () => {
  const app = loadApp();
  app.elements.get("map-panel").clientWidth = 2048;
  configureMap(app, 0);
  app.context.Airloom.debugZoomTo(3);
  app.context.Airloom.receive("sensors", {
    items: [fixtureSensor(1, 0, 42)],
    selected_id: 1,
    source: "PurpleAir live",
  });
  app.runTimers();

  assert.equal(app.elements.get("summary-aqi").textContent, "42");
  const message = app.messages.filter((item) => item.action === "view-changed").at(-1);
  assert.ok(message, "the full-world view should emit a view-changed payload");
  assert.equal(message.west, -180);
  assert.equal(message.east, 180);
});

test("a wider-than-world viewport keeps full-world bounds and summary coverage", () => {
  const app = loadApp();
  app.elements.get("map-panel").clientWidth = 4096;
  configureMap(app, 0);
  app.context.Airloom.debugZoomTo(3);
  app.context.Airloom.receive("sensors", {
    items: [
      fixtureSensor(1, -170, 20),
      fixtureSensor(2, 170, 40),
    ],
    selected_id: 1,
    source: "PurpleAir live",
  });
  app.runTimers();

  assert.equal(app.elements.get("summary-aqi").textContent, "40");
  assert.equal(app.elements.get("summary-chip").title, "2 sensors reporting");
  const message = app.messages.filter((item) => item.action === "view-changed").at(-1);
  assert.ok(message, "the wider-than-world view should emit a view-changed payload");
  assert.equal(message.west, -180);
  assert.equal(message.east, 180);
});

test("fly-to uses the nearest unwrapped world across the antimeridian", () => {
  const app = loadApp();
  configureMap(app, 179.9);
  app.context.Airloom.receive("location", {
    latitude: 0,
    longitude: -179.9,
    name: "Across the antimeridian",
    source: "fixed",
  });
  app.runAnimationFrames(600);
  closeTo(app.context.Airloom.debugState().center.lon, 180.1);
});

test("area medians update as the viewport moves", () => {
  const app = loadApp();
  configureMap(app, 0);
  app.context.Airloom.debugZoomTo(8);
  app.context.Airloom.receive("sensors", {
    items: [
      fixtureSensor(1, -1, 10),
      fixtureSensor(2, 0.5, 20),
      fixtureSensor(3, 1, 30),
    ],
    selected_id: 1,
    source: "PurpleAir live",
  });
  assert.equal(app.elements.get("summary-aqi").textContent, "20");

  const panel = app.elements.get("map-panel");
  panel.dispatch("pointerdown", { clientX: 256, clientY: 256, pointerId: 1, buttons: 1 });
  panel.dispatch("pointermove", { clientX: 0, clientY: 256, pointerId: 1, buttons: 1 });
  panel.dispatch("pointerup", { clientX: 0, clientY: 256, pointerId: 1, buttons: 0 });
  assert.equal(app.elements.get("summary-aqi").textContent, "30");
});

test("legacy summary items exclude out-of-viewport and remote-favorite readings", () => {
  const app = loadApp();
  configureMap(app, 0);
  app.context.Airloom.debugZoomTo(8);
  app.context.Airloom.receive("sensors", {
    items: [
      fixtureSensor(1, -1, 10),
      fixtureSensor(2, 1, 30),
      fixtureSensor(3, 100, 200),
      fixtureSensor(4, 0.5, 1, { favorite: true, remote_favorite: true }),
    ],
    selected_id: 1,
    source: "PurpleAir live",
  });

  assert.equal(app.elements.get("summary-aqi").textContent, "30");
  assert.equal(app.elements.get("summary-chip").title, "2 sensors reporting");
});

test("area medians use only viewport sensors, never distant favorite inputs", () => {
  const app = loadApp();
  configureMap(app, 0);
  app.context.Airloom.debugZoomTo(8);
  app.context.Airloom.receive("sensors", {
    area: [
      fixtureSensor(1, -1, 10),
      fixtureSensor(2, 1, 30),
      fixtureSensor(3, 100, 200),
    ],
    favorites: [
      fixtureSensor(4, 170, 250, { favorite: true, remote_favorite: true }),
      fixtureSensor(5, 0.5, 1, { favorite: true, remote_favorite: true }),
    ],
    selected_id: 1,
    source: "PurpleAir live",
  });

  assert.equal(app.elements.get("summary-aqi").textContent, "30");
  assert.equal(app.elements.get("summary-chip").title, "2 sensors reporting");
  assert.equal(app.elements.get("favorite-count").textContent, "2");
  assert.match(app.elements.get("favorites-list").innerHTML, /data-id="4"/);
  assert.match(app.elements.get("favorites-list").innerHTML, /data-id="5"/);
  assert.doesNotMatch(app.elements.get("sensor-list").innerHTML, /data-id="[45]"/);
});

test("capped coverage is visible near the summary and clears for complete coverage", () => {
  const app = loadApp();
  configureMap(app, 0);
  const payload = {
    items: [fixtureSensor(1, 0, 20)],
    selected_id: 1,
    source: "PurpleAir live",
  };
  app.context.Airloom.receive("sensors", { ...payload, coverage: { capped: true, complete: false } });
  const status = app.elements.get("coverage-status");
  assert.ok(status, "the summary should include a coverage status element");
  assert.equal(status.hidden, false);
  assert.equal(status.textContent, "Limited coverage");
  assert.match(app.elements.get("summary-chip").title, /limited coverage/i);

  app.context.Airloom.receive("sensors", { ...payload, coverage: { capped: false, complete: true } });
  assert.equal(status.hidden, true);
});

test("viewport departure marks coverage limited before a delayed replacement arrives", () => {
  const app = loadApp();
  configureMap(app, 0);
  const payload = {
    items: [fixtureSensor(1, 0, 20)],
    selected_id: 1,
    source: "PurpleAir live",
    coverage: { north: 0.09, west: -0.09, south: -0.09, east: 0.09, capped: false, complete: true },
  };
  app.context.Airloom.receive("sensors", payload);
  const status = app.elements.get("coverage-status");
  assert.equal(status.hidden, true);

  const panel = app.elements.get("map-panel");
  panel.dispatch("pointerdown", { clientX: 256, clientY: 256, pointerId: 1, buttons: 1 });
  panel.dispatch("pointermove", { clientX: 0, clientY: 256, pointerId: 1, buttons: 1 });
  panel.dispatch("pointerup", { clientX: 0, clientY: 256, pointerId: 1, buttons: 0 });
  assert.equal(status.hidden, false, "leaving fetched bounds must be visible before the fetch timer fires");
  assert.equal(status.textContent, "Limited coverage");

  app.runTimers();
  app.context.Airloom.receive("error", { message: "Replacement fetch failed" });
  app.context.Airloom.receive("loading", { active: false });
  assert.equal(status.hidden, false, "a failed replacement must not restore complete coverage");

  app.context.Airloom.receive("sensors", {
    ...payload,
    coverage: { bounds: { north: 0.2, west: -0.2, south: -0.2, east: 0.2 }, capped: false, complete: true },
  });
  assert.equal(status.hidden, true, "a delayed replacement covering the current viewport clears the warning");
});

test("coverage comparison handles antimeridian views and full-world replacements", () => {
  const app = loadApp();
  configureMap(app, 179.9);
  app.context.Airloom.debugZoomTo(8);
  const payload = {
    items: [fixtureSensor(1, -179.9, 20)],
    selected_id: 1,
    source: "PurpleAir live",
    coverage: { north: 2, west: 178, south: -2, east: -178, capped: false, complete: true },
  };
  app.context.Airloom.receive("sensors", payload);
  const status = app.elements.get("coverage-status");
  assert.equal(status.hidden, true);

  app.context.Airloom.debugZoomTo(3);
  assert.equal(status.hidden, false, "a world viewport exceeds the crossing fetch on both arms");

  app.context.Airloom.receive("sensors", {
    ...payload,
    coverage: { north: 85.0511, west: -180, south: -85.0511, east: 180, capped: false, complete: true },
  });
  assert.equal(status.hidden, true);
});

test("direct-file preview still loads demo data without a bridge", () => {
  const app = loadApp({ preview: true });
  app.context.Airloom.debugZoomTo(8);
  app.runTimers();
  assert.equal(app.elements.get("sensor-count").textContent, "8");
  assert.equal(app.elements.get("data-source").textContent, "Browser preview · Demo data");
  assert.notEqual(app.elements.get("summary-aqi").textContent, "—");
});

test("the direct-file legend renders all six exact AQI ranges", () => {
  const html = fs.readFileSync(indexPath, "utf8");
  const rows = [...html.matchAll(/<div><i style="--c:(#[0-9a-f]+)"><\/i><span>([^<]+)<\/span><b>([^<]+)<\/b><\/div>/g)]
    .map((match) => match.slice(1));
  assert.deepEqual(rows, [
    ["#35b779", "Good", "0–50"],
    ["#f6c945", "Moderate", "51–100"],
    ["#f39c3d", "Sensitive", "101–150"],
    ["#e65b65", "Unhealthy", "151–200"],
    ["#9b6bc3", "Very unhealthy", "201–300"],
    ["#7e394d", "Hazardous", "301–500"],
  ]);
});
