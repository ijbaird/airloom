"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const indexPath = require.resolve("../../airloom/resources/index.html");
const cssPath = require.resolve("../../airloom/resources/app.css");

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
    this.attributes = {};
    this.style = { setProperty: (name, value) => { this.style[name] = value; } };
    this.classList = new FakeClassList();
    this.listeners = new Map();
    this.childNodes = [];
    this.isConnected = true;
    this.elements = {};
    this.ownerDocument = null;
    this.parentNode = null;
    this._innerHTML = "";
  }

  set className(value) {
    this._className = value;
    this.classList = new FakeClassList();
    for (const name of String(value).split(/\s+/).filter(Boolean)) this.classList.add(name);
  }
  get className() { return this._className || ""; }
  get children() { return this.childNodes; }
  set textContent(value) {
    this._textContent = String(value);
    if (value === "") this.clearChildren();
  }
  get textContent() { return this._textContent || ""; }
  set innerHTML(value) {
    this._innerHTML = String(value);
    this.clearChildren();
    for (const match of this._innerHTML.matchAll(/<button\b([^>]*)>/g)) {
      const button = new FakeElement("button");
      button.ownerDocument = this.ownerDocument;
      for (const attribute of match[1].matchAll(/([:\w-]+)="([^"]*)"/g)) {
        button.setAttribute(attribute[1], attribute[2]);
      }
      this.appendChild(button);
    }
  }
  get innerHTML() { return this._innerHTML; }

  clearChildren() {
    for (const child of this.childNodes) {
      if (this.ownerDocument?.activeElement && child.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = null;
      child.parentNode = null;
      child.isConnected = false;
    }
    this.childNodes = [];
  }
  get firstChild() { return this.childNodes[0] || null; }
  appendChild(child) { return this.insertBefore(child, null); }
  insertBefore(child, reference) {
    if (child.parentNode) {
      const siblings = child.parentNode.childNodes;
      const index = siblings.indexOf(child);
      if (index >= 0) siblings.splice(index, 1);
    }
    child.parentNode = this;
    child.ownerDocument = this.ownerDocument;
    child.isConnected = true;
    const index = reference === null ? -1 : this.childNodes.indexOf(reference);
    if (index < 0) this.childNodes.push(child);
    else this.childNodes.splice(index, 0, child);
    return child;
  }
  remove() {
    if (this.ownerDocument?.activeElement && this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = null;
    this.isConnected = false;
    if (this.parentNode) {
      const index = this.parentNode.childNodes.indexOf(this);
      if (index >= 0) this.parentNode.childNodes.splice(index, 1);
    }
    this.parentNode = null;
  }
  contains(other) {
    if (other === this) return true;
    return this.childNodes.some((child) => child.contains(other));
  }
  focus() {
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
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
  setAttribute(name, value) {
    const setting = String(value);
    this.attributes[name] = setting;
    if (name === "class") this.className = setting;
    else if (name === "style") {
      for (const declaration of setting.split(";")) {
        const separator = declaration.indexOf(":");
        if (separator > 0) this.style[declaration.slice(0, separator).trim()] = declaration.slice(separator + 1).trim();
      }
    } else if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      this.dataset[key] = setting;
    } else this[name] = setting;
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
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
    for (const attribute of match[0].matchAll(/([:\w-]+)="([^"]*)"/g)) element.setAttribute(attribute[1], attribute[2]);
    element.hidden = /\shidden(?:\s|>)/.test(match[0]);
    if (element.id === "map-panel") {
      element.clientWidth = 512;
      element.clientHeight = 512;
    }
    elements.set(element.id, element);
  }

  const document = new FakeElement("document");
  document.body = new FakeElement("body");
  document.body.ownerDocument = document;
  document.documentElement = new FakeElement("html");
  document.documentElement.ownerDocument = document;
  document.activeElement = null;
  for (const element of elements.values()) element.ownerDocument = document;
  document.querySelector = (selector) => selector.startsWith("#") ? elements.get(selector.slice(1)) || null : null;
  document.querySelectorAll = (selector) => {
    const found = [];
    const seen = new Set();
    for (const element of elements.values()) {
      for (const match of element.querySelectorAll(selector)) {
        if (!seen.has(match)) { seen.add(match); found.push(match); }
      }
    }
    return found;
  };
  document.createElement = (tagName) => {
    const element = new FakeElement(tagName);
    element.ownerDocument = document;
    return element;
  };
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
    document,
    elements,
    messages,
    runAnimationFrames(now) {
      const pending = [...animationFrames.values()];
      animationFrames.clear();
      for (const callback of pending) callback(now);
    },
  };
}

function configureMap(app, extra = {}) {
  app.context.Airloom.receive("config", {
    latitude: 0,
    longitude: 0,
    location_name: "Test area",
    heatmap_threshold_km: 40,
    ...extra,
  });
}

function fixtureSensor(id, extra = {}) {
  return {
    id,
    name: `Sensor ${id}`,
    latitude: 0,
    longitude: 0,
    aqi: 20 + id,
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

function receiveSensors(app, items, extra = {}) {
  app.context.Airloom.receive("sensors", {
    items,
    selected_id: items[0]?.id ?? null,
    source: "PurpleAir live",
    ...extra,
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

test("mixed tile settlement ending in failure retains retired coverage", () => {
  const app = loadApp();
  configureMap(app);
  const live = app.elements.get("tiles");
  const old = app.elements.get("tiles-old");
  for (const image of [...live.childNodes]) image.onload();
  app.context.Airloom.debugZoomTo(13);
  assert.ok(old.childNodes.length > 0, "successful imagery should move to the retired layer");

  const replacements = [...live.childNodes];
  assert.ok(replacements.length > 2, "the new zoom should request several tiles");
  const successful = replacements[0];
  const finalFailure = replacements.at(-1);
  successful.onload();
  for (const image of replacements.slice(1, -1)) image.onerror();
  assert.ok(old.childNodes.length > 0, "the final pending request must not yet retire imagery");

  finalFailure.onerror();

  assert.equal(successful.classList.contains("loaded"), true);
  assert.ok(old.childNodes.length > 0, "a partially failed replacement must not clear retired imagery");
  assert.equal(old.hidden, false);
});

test("reconcileKeyed preserves keyed controls and returns removed logical focus", () => {
  const { reconcileKeyed } = require("../../airloom/resources/state.js");
  assert.equal(typeof reconcileKeyed, "function");
  const document = new FakeElement("document");
  document.activeElement = null;
  const container = new FakeElement("div", "sensor-list");
  container.ownerDocument = document;
  const nodes = new Map();
  const create = (item) => {
    const node = new FakeElement("button");
    node.ownerDocument = document;
    node.dataset.id = String(item.id);
    nodes.set(item.id, node);
    return node;
  };
  const update = (node, item) => { node.textContent = item.name; };
  const key = (item) => item.id;

  reconcileKeyed(container, [{ id: 1, name: "One" }, { id: 2, name: "Two" }], key, create, update);
  const second = nodes.get(2);
  second.focus();
  reconcileKeyed(container, [{ id: 2, name: "Two updated" }, { id: 3, name: "Three" }, { id: 1, name: "One" }], key, create, update);

  assert.deepEqual(container.childNodes.map((node) => node.dataset.id), ["2", "3", "1"]);
  assert.equal(container.childNodes[0], second);
  assert.equal(container.childNodes[0].textContent, "Two updated");
  assert.equal(document.activeElement, second);

  const result = reconcileKeyed(container, [{ id: 1, name: "One" }, { id: 3, name: "Three" }], key, create, update);
  assert.deepEqual(result.removedFocus, { key: "2", surface: "sensor-list" });
});

test("sensor refresh preserves a focused nearby row and its node", () => {
  const app = loadApp();
  configureMap(app);
  app.elements.get("sensors-panel").hidden = false;
  receiveSensors(app, [fixtureSensor(1), fixtureSensor(2)]);
  const row = app.elements.get("sensor-list").querySelectorAll(".sensor-row")[0];
  row.focus();

  receiveSensors(app, [fixtureSensor(1, { trend: [{ label: "Now", aqi: 21 }] }), fixtureSensor(2)]);

  assert.equal(app.elements.get("sensor-list").querySelectorAll(".sensor-row")[0], row);
  assert.equal(app.document.activeElement, row);
});

test("trend refresh preserves a focused map marker and its node", () => {
  const app = loadApp();
  configureMap(app);
  receiveSensors(app, [fixtureSensor(1)]);
  const marker = app.elements.get("markers").querySelectorAll(".map-marker")[0];
  marker.focus();

  receiveSensors(app, [fixtureSensor(1, { trend: [{ label: "Now", aqi: 21 }] })]);

  assert.equal(app.elements.get("markers").querySelectorAll(".map-marker")[0], marker);
  assert.equal(app.document.activeElement, marker);
});

test("selecting a row focuses detail and closing detail restores that row", () => {
  const app = loadApp();
  configureMap(app);
  app.elements.get("sensors-panel").hidden = false;
  receiveSensors(app, [fixtureSensor(1)]);
  const row = app.elements.get("sensor-list").querySelectorAll(".sensor-row")[0];
  row.focus();
  row.click();

  assert.equal(app.elements.get("detail-card").hidden, false);
  assert.equal(app.document.activeElement, app.elements.get("close-detail"));
  app.elements.get("close-detail").click();
  assert.equal(app.document.activeElement, row);
});

test("popup opening focuses Details and Escape restores the originating marker", () => {
  const app = loadApp();
  configureMap(app);
  receiveSensors(app, [fixtureSensor(1)]);
  const marker = app.elements.get("markers").querySelectorAll(".map-marker")[0];
  marker.focus();
  marker.click();

  assert.equal(app.document.activeElement, app.elements.get("popup-details"));
  app.document.dispatch("keydown", { key: "Escape", ctrlKey: false, metaKey: false });
  assert.equal(app.elements.get("map-popup").hidden, true);
  assert.equal(app.document.activeElement, marker);
});

test("hiding the selected detail moves focus to the stable Sensors control", () => {
  const app = loadApp();
  configureMap(app);
  app.elements.get("sensors-panel").hidden = false;
  receiveSensors(app, [fixtureSensor(1)]);
  app.elements.get("sensor-list").querySelectorAll(".sensor-row")[0].click();
  app.elements.get("sensors-panel").hidden = true;
  app.elements.get("hide-button").focus();
  app.elements.get("hide-button").click();

  assert.equal(app.elements.get("detail-card").hidden, true);
  assert.equal(app.document.activeElement, app.elements.get("sensors-button"));
});

test("removing a focused row during refresh advances to the remaining row", () => {
  const app = loadApp();
  configureMap(app);
  app.elements.get("sensors-panel").hidden = false;
  receiveSensors(app, [fixtureSensor(1), fixtureSensor(2)]);
  const removed = app.elements.get("sensor-list").querySelectorAll(".sensor-row")[0];
  removed.focus();

  receiveSensors(app, [fixtureSensor(2)]);

  const remaining = app.elements.get("sensor-list").querySelectorAll(".sensor-row")[0];
  assert.equal(remaining.dataset.id, "2");
  assert.equal(app.document.activeElement, remaining);
});

test("search and settings have accessible names and Ctrl K focuses search", () => {
  const app = loadApp();
  const search = app.elements.get("search");
  assert.equal(search.getAttribute("aria-label"), "Search sensors or places");
  assert.equal(app.elements.get("settings-dialog").getAttribute("aria-labelledby"), "settings-title");
  assert.ok(app.elements.get("settings-title"));

  app.document.dispatch("keydown", { key: "k", ctrlKey: true, metaKey: false });
  assert.equal(app.document.activeElement, search);
});

test("status and error feedback expose live regions", () => {
  const app = loadApp();
  const status = app.elements.get("ui-status");
  const errors = app.elements.get("toast-stack");
  assert.ok(status);
  assert.equal(status.getAttribute("role"), "status");
  assert.equal(status.getAttribute("aria-live"), "polite");
  assert.equal(errors.getAttribute("role"), "alert");
  assert.equal(errors.getAttribute("aria-live"), "assertive");
});

test("favorite controls expose synchronized pressed state", () => {
  const app = loadApp();
  configureMap(app);
  receiveSensors(app, [fixtureSensor(1, { favorite: true })]);
  app.elements.get("markers").querySelectorAll(".map-marker")[0].click();
  assert.equal(app.elements.get("favorite-button").getAttribute("aria-pressed"), "true");
  assert.equal(app.elements.get("popup-favorite").getAttribute("aria-pressed"), "true");

  receiveSensors(app, [fixtureSensor(1, { favorite: false })]);
  assert.equal(app.elements.get("favorite-button").getAttribute("aria-pressed"), "false");
  assert.equal(app.elements.get("popup-favorite").getAttribute("aria-pressed"), "false");
});

test("interface scale config updates the root scale hook without page zoom", () => {
  const app = loadApp();
  configureMap(app, { interface_scale: 1.5 });
  assert.equal(app.document.documentElement.style["--interface-scale"], "1.5");

  receiveSensors(app, [fixtureSensor(1)], { config: { interface_scale: 1.25 } });
  assert.equal(app.document.documentElement.style["--interface-scale"], "1.25");
  assert.equal(app.context.Airloom.debugState().viewportScale, null);
});

test("popup placement clamps horizontally and flips below near the viewport top", () => {
  const app = loadApp();
  configureMap(app, { heatmap_threshold_km: 1000 });
  app.context.Airloom.debugZoomTo(8);
  receiveSensors(app, [fixtureSensor(1, { latitude: 1.4, longitude: -1.4 })]);
  const popup = app.elements.get("map-popup");
  popup.clientWidth = 232;
  popup.clientHeight = 120;
  app.elements.get("markers").querySelectorAll(".map-marker")[0].click();

  assert.equal(Number.parseFloat(popup.style.left), 32640);
  assert.equal(popup.dataset.placement, "below");
  assert.equal(popup.classList.contains("below"), true);
});

test("search overlay stacks above ordinary sensor and detail panels", () => {
  const css = fs.readFileSync(cssPath, "utf8");
  const declaration = (selector, property) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const rule = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
    return rule?.[1].match(new RegExp(`${property}:\\s*([^;]+)`))?.[1];
  };
  const searchZ = Number(declaration(".search-overlay", "z-index"));
  const panelZ = Number(declaration(".sensors-panel", "z-index"));
  const detailZ = Number(declaration(".detail-card", "z-index"));
  assert.ok(searchZ > panelZ, `search z-index ${searchZ} must exceed ${panelZ}`);
  assert.ok(searchZ > detailZ, `search z-index ${searchZ} must exceed ${detailZ}`);
});

test("narrow layout stacks summary below a full-width top search", () => {
  const css = fs.readFileSync(cssPath, "utf8");
  const narrow = css.split("@media (max-width: 640px) {")[1]?.split("@media (prefers-color-scheme:")[0] || "";
  assert.match(narrow, /\.search-overlay\s*\{[^}]*width:\s*calc\(100vw - 20px\)/);
  assert.match(narrow, /\.summary-chip\s*\{[^}]*top:\s*64px;[^}]*left:\s*10px;[^}]*right:\s*auto;/);
});

test("demo, cached, stale, and coverage provenance remain visible near AQI", () => {
  const app = loadApp();
  configureMap(app);
  const summary = app.elements.get("summary-provenance");
  const detail = app.elements.get("detail-provenance");
  assert.ok(summary);
  assert.ok(detail);

  receiveSensors(app, [fixtureSensor(1, { origin: "demo" })], { source: "Demo data" });
  assert.equal(summary.hidden, false);
  assert.equal(summary.textContent, "Demo data");
  assert.equal(detail.hidden, false);
  assert.equal(detail.textContent, "Demo data");

  receiveSensors(app, [fixtureSensor(1, { delivery: "cache" })], { source: "PurpleAir · cached" });
  assert.equal(summary.textContent, "Cached data");
  assert.equal(detail.textContent, "Cached data");
  receiveSensors(app, [fixtureSensor(1, { delivery: "cache" })], {
    source: "PurpleAir · stale cached data",
    coverage: { capped: true, complete: false },
  });
  assert.equal(summary.textContent, "Stale cached data");
  assert.equal(app.elements.get("coverage-status").hidden, false);

  receiveSensors(app, [fixtureSensor(1)], { coverage: { capped: false, complete: true } });
  assert.equal(summary.hidden, true);
  assert.equal(detail.hidden, true);
  assert.equal(app.elements.get("coverage-status").hidden, true);
});

test("Sensors count matches nearby rows when favorites are a separate collection", () => {
  const app = loadApp();
  configureMap(app);
  app.context.Airloom.receive("sensors", {
    area: [fixtureSensor(1), fixtureSensor(2)],
    favorites: [fixtureSensor(3, { favorite: true, remote_favorite: true })],
    selected_id: 1,
    source: "PurpleAir live",
  });

  assert.equal(app.elements.get("favorite-count").textContent, "1");
  assert.equal(app.elements.get("sensor-list").querySelectorAll(".sensor-row").length, 2);
  assert.equal(app.elements.get("sensor-count").textContent, "2");
});
