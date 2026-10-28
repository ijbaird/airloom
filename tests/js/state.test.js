"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const indexPath = require.resolve("../../airloom/resources/index.html");

function createGate() {
  const { NavigationGate } = require("../../airloom/resources/state.js");
  return new NavigationGate();
}

test("NavigationGate rejects superseded and canceled tokens", () => {
  const gate = createGate();
  const first = gate.begin("fly");
  const current = gate.begin("fly");
  assert.equal(gate.isCurrent("fly", first), false);
  assert.equal(gate.isCurrent("fly", current), true);
  gate.cancel("fly");
  assert.equal(gate.isCurrent("fly", current), false);
});

test("NavigationGate direct manipulation cancels flight", () => {
  const gate = createGate();
  const flight = gate.begin("fly");
  const direct = gate.begin("direct");
  assert.equal(gate.isCurrent("fly", flight), false);
  assert.equal(gate.isCurrent("direct", direct), true);
});

test("NavigationGate flight supersedes zoom animation", () => {
  const gate = createGate();
  const zoom = gate.begin("zoom");
  const flight = gate.begin("fly");
  assert.equal(gate.isCurrent("zoom", zoom), false);
  assert.equal(gate.isCurrent("fly", flight), true);
});

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
  }
  get innerHTML() { return this._innerHTML; }

  get firstChild() { return this.childNodes[0] || null; }
  appendChild(child) {
    if (child.parentNode) {
      const siblings = child.parentNode.childNodes;
      const index = siblings.indexOf(child);
      if (index >= 0) siblings.splice(index, 1);
    }
    child.parentNode = this;
    child.isConnected = true;
    this.childNodes.push(child);
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
  setAttribute(name, value) { this[name] = String(value); }
  setPointerCapture() {}
  getBoundingClientRect() {
    return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight };
  }
  closest() { return null; }
  querySelector(selector) {
    if (selector !== ".tile.loaded") return null;
    return this.childNodes.find((child) => child.classList.contains("tile") && child.classList.contains("loaded")) || null;
  }
  querySelectorAll(selector) { return selector === ".map-marker" ? this._markers : []; }
  getContext() {
    if (this.tagName !== "CANVAS") return null;
    return {
      createImageData: (width, height) => ({ width, height, data: new Uint8ClampedArray(width * height * 4) }),
      putImageData() {},
      clearRect() {},
    };
  }
}

function loadApp({ reducedMotion = false } = {}) {
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
    matchMedia: (query) => ({ media: query, matches: reducedMotion }),
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
    webkit: {
      messageHandlers: {
        airloom: { postMessage: (message) => messages.push(JSON.parse(message)) },
      },
    },
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
    runAnimationFrames(now) {
      const pending = [...animationFrames.values()];
      animationFrames.clear();
      for (const callback of pending) callback(now);
    },
  };
}

function configureMap(app) {
  app.context.Airloom.receive("config", {
    latitude: 0,
    longitude: 0,
    location_name: "Test area",
    heatmap_threshold_km: 40,
  });
}

function startFlight(app) {
  app.context.Airloom.receive("location", {
    latitude: 10,
    longitude: 20,
    name: "Flight target",
    source: "fixed",
  });
  app.runAnimationFrames(300);
}

function closeTo(actual, expected, epsilon = 0.000001) {
  assert.ok(Math.abs(actual - expected) < epsilon, `${actual} was not close to ${expected}`);
}

function assertCenter(app, expected) {
  const center = app.context.Airloom.debugState().center;
  closeTo(center.lat, expected.lat);
  closeTo(center.lon, expected.lon);
}

test("dragging cancels an in-flight recenter", () => {
  const app = loadApp();
  configureMap(app);
  startFlight(app);
  const panel = app.elements.get("map-panel");
  panel.dispatch("pointerdown", { clientX: 256, clientY: 256, pointerId: 1, buttons: 1 });
  panel.dispatch("pointermove", { clientX: 276, clientY: 256, pointerId: 1, buttons: 1 });
  panel.dispatch("pointerup", { clientX: 276, clientY: 256, pointerId: 1, buttons: 0 });
  const expected = { ...app.context.Airloom.debugState().center };

  app.runAnimationFrames(600);
  assertCenter(app, expected);
});

test("vertical wheel zoom cancels an in-flight recenter", () => {
  const app = loadApp();
  configureMap(app);
  startFlight(app);
  app.elements.get("map-panel").dispatch("wheel", {
    clientX: 256,
    clientY: 256,
    deltaX: 0,
    deltaY: -1,
    ctrlKey: false,
  });
  const expected = { ...app.context.Airloom.debugState().center };

  app.runAnimationFrames(600);
  assertCenter(app, expected);
});

test("native pinch cancels an in-flight recenter", () => {
  const app = loadApp();
  configureMap(app);
  startFlight(app);
  app.context.Airloom.receive("pinch", { phase: "begin", x: 256, y: 256 });
  const expected = { ...app.context.Airloom.debugState().center };

  app.runAnimationFrames(600);
  assertCenter(app, expected);
});

test("a new flight supersedes pending zoom animation", () => {
  const app = loadApp();
  configureMap(app);
  app.elements.get("zoom-in").click();
  app.context.Airloom.receive("location", {
    latitude: 10,
    longitude: 20,
    name: "Flight target",
    source: "fixed",
  });

  app.runAnimationFrames(600);
  assert.equal(app.context.Airloom.debugState().zoom, 12);
  assertCenter(app, { lat: 10, lon: 20 });
});

test("reduced motion completes flight immediately", () => {
  const app = loadApp({ reducedMotion: true });
  configureMap(app);
  app.context.Airloom.receive("location", {
    latitude: 10,
    longitude: 20,
    name: "Flight target",
    source: "fixed",
  });

  assertCenter(app, { lat: 10, lon: 20 });
});

test("reduced motion completes button zoom immediately", () => {
  const app = loadApp({ reducedMotion: true });
  configureMap(app);
  app.elements.get("zoom-in").click();
  assert.equal(app.context.Airloom.debugState().zoom, 13);
});

test("zero vertical wheel delta does not start zoom", () => {
  const app = loadApp();
  configureMap(app);
  app.context.Airloom.debugZoomTo(8);
  app.elements.get("map-panel").dispatch("wheel", {
    clientX: 256,
    clientY: 256,
    deltaX: 0,
    deltaY: 0,
    ctrlKey: false,
  });

  app.runAnimationFrames(600);
  assert.equal(app.context.Airloom.debugState().zoom, 8);
});

test("predominantly horizontal wheel input does not start zoom", () => {
  const app = loadApp();
  configureMap(app);
  app.context.Airloom.debugZoomTo(8);
  app.elements.get("map-panel").dispatch("wheel", {
    clientX: 256,
    clientY: 256,
    deltaX: 5,
    deltaY: 1,
    ctrlKey: false,
  });

  app.runAnimationFrames(600);
  assert.equal(app.context.Airloom.debugState().zoom, 8);
});

test("failed tile settlement keeps failed imagery hidden and retains retired coverage", () => {
  const app = loadApp();
  configureMap(app);
  const live = app.elements.get("tiles");
  const old = app.elements.get("tiles-old");
  for (const image of [...live.childNodes]) image.onload();
  app.context.Airloom.debugZoomTo(13);
  assert.ok(old.childNodes.length > 0, "successful imagery should move to the retired layer");
  assert.equal(old.hidden, false);

  const failed = [...live.childNodes];
  assert.ok(failed.length > 0, "the new zoom should request tiles");
  for (const image of failed) image.onerror();

  for (const image of failed) assert.equal(image.classList.contains("loaded"), false);
  assert.ok(old.childNodes.length > 0, "failed replacements must not clear successful retired imagery");
  assert.equal(old.hidden, false);
});
