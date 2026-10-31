"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const indexPath = require.resolve("../../airloom/resources/index.html");
const cssPath = require.resolve("../../airloom/resources/app.css");

function cssDeclaration(selector, property, { narrow = false } = {}) {
  const css = fs.readFileSync(cssPath, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const base = css.split("@media")[0];
  const narrowCss = css.split("@media (max-width: 640px) {")[1]?.split("@media (prefers-color-scheme:")[0] || "";
  const sources = narrow ? [narrowCss, base] : [base];
  const escape = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  for (const source of sources) {
    for (const match of source.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const selectors = match[1].split(",").map((value) => value.trim());
      if (!selectors.includes(selector)) continue;
      const declaration = match[2].match(new RegExp(`(?:^|;)\\s*${escape(property)}\\s*:\\s*([^;]+)`));
      if (declaration) return declaration[1].trim();
    }
  }
  return null;
}

function computedCssPixels(selector, property, scale, options = {}) {
  const value = cssDeclaration(selector, property, options);
  assert.ok(value, `${selector} must declare ${property}`);
  const scaled = value.match(/^calc\(\s*(-?[\d.]+)px\s*\*\s*var\(--interface-scale\)\s*\)$/);
  if (scaled) return Number(scaled[1]) * scale;
  const fixed = value.match(/^(-?[\d.]+)px$/);
  assert.ok(fixed, `${selector} ${property} must resolve to pixels; got ${value}`);
  return Number(fixed[1]);
}

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
    this.disabled = false;
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
  showModal() { this.open = true; }
  close(returnValue = "") {
    this.open = false;
    this.returnValue = returnValue;
    this.dispatch("close");
  }
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

function loadApp({ reducedMotion = false, preview = false } = {}) {
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
  const settingsForm = elements.get("settings-form");
  for (const name of ["radius_km", "alert_threshold", "heatmap_threshold_km", "refresh_minutes", "interface_scale", "home_lat", "home_lon", "location_name"]) {
    settingsForm.elements[name] = new FakeElement("input");
  }
  for (const name of ["api_key", "clear_api_key", "confidence_filter"]) {
    const field = new FakeElement("input");
    field.type = name === "api_key" ? "password" : "checkbox";
    settingsForm.elements[name] = field;
  }
  settingsForm.elements.home_mode = { value: "auto" };
  settingsForm.elements.temperature_unit = { value: "F" };
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
    FormData: class {
      constructor(form) { this.form = form; }
      get(name) {
        const field = this.form.elements[name];
        if (!field) return null;
        if (field.type === "checkbox") return field.checked ? "on" : null;
        return field.value;
      }
    },
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
    document,
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

function configureMap(app, extra = {}) {
  app.context.Airloom.receive("config", {
    latitude: 0,
    longitude: 0,
    location_name: "Test area",
    heatmap_threshold_km: 40,
    ...extra,
  });
}

function openSettings(app, extra = {}) {
  app.context.Airloom.receive("open-settings", {
    latitude: 0,
    longitude: 0,
    location_name: "Test area",
    radius_km: 22,
    alert_threshold: 101,
    heatmap_threshold_km: 40,
    refresh_minutes: 2,
    temperature_unit: "F",
    confidence_filter: true,
    home_mode: "auto",
    has_api_key: false,
    api_key_hint: "",
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

test("flight invalidates active DOM pinch changes and end settlement", () => {
  const app = loadApp();
  configureMap(app);
  app.document.dispatch("gesturestart", { clientX: 256, clientY: 256 });
  app.document.dispatch("gesturechange", { clientX: 256, clientY: 256, scale: 2 });
  app.context.Airloom.receive("location", {
    latitude: 10,
    longitude: 20,
    name: "Flight target",
    source: "fixed",
  });
  const flightZoom = app.context.Airloom.debugState().zoom;

  app.document.dispatch("gesturechange", { clientX: 256, clientY: 256, scale: 8 });
  app.document.dispatch("gestureend", { clientX: 256, clientY: 256 });
  app.runAnimationFrames(600);

  assertCenter(app, { lat: 10, lon: 20 });
  assert.equal(app.context.Airloom.debugState().zoom, flightZoom);
});

test("flight invalidates active native pinch changes and end settlement", () => {
  const app = loadApp();
  configureMap(app);
  app.context.Airloom.receive("pinch", { phase: "begin", x: 256, y: 256 });
  app.context.Airloom.receive("pinch", { phase: "change", x: 256, y: 256, scale: 2 });
  app.context.Airloom.receive("location", {
    latitude: 10,
    longitude: 20,
    name: "Flight target",
    source: "fixed",
  });
  const flightZoom = app.context.Airloom.debugState().zoom;

  app.context.Airloom.receive("pinch", { phase: "change", x: 256, y: 256, scale: 8 });
  app.context.Airloom.receive("pinch", { phase: "end", x: 256, y: 256 });
  app.runAnimationFrames(600);

  assertCenter(app, { lat: 10, lon: 20 });
  assert.equal(app.context.Airloom.debugState().zoom, flightZoom);
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

test("system reduced-motion preference from the shell skips flight animation", () => {
  const app = loadApp();
  configureMap(app);
  app.context.Airloom.receive("motion", { reduced: true });
  assert.equal(app.document.documentElement.classList.contains("reduce-motion"), true);
  assert.equal(app.context.Airloom.debugState().reducedMotion, true);

  app.context.Airloom.receive("location", { latitude: 10, longitude: 20, name: "Target", source: "fixed" });
  assertCenter(app, { lat: 10, lon: 20 });
  app.elements.get("zoom-in").click();
  assert.equal(app.context.Airloom.debugState().zoom, 13);
});

test("re-enabled system animations animate again unless the browser asks otherwise", () => {
  const app = loadApp();
  configureMap(app);
  app.context.Airloom.receive("motion", { reduced: true });
  app.context.Airloom.receive("motion", { reduced: false });
  assert.equal(app.document.documentElement.classList.contains("reduce-motion"), false);
  assert.equal(app.context.Airloom.debugState().reducedMotion, false);
  app.context.Airloom.receive("location", { latitude: 10, longitude: 20, name: "Target", source: "fixed" });
  assert.notEqual(app.context.Airloom.debugState().center.lat, 10);

  const media = loadApp({ reducedMotion: true });
  configureMap(media);
  media.context.Airloom.receive("motion", { reduced: false });
  assert.equal(media.context.Airloom.debugState().reducedMotion, true, "the media query still applies");
  assert.equal(media.document.documentElement.classList.contains("reduce-motion"), true);
});

test("effective reduced motion also stops decorative CSS motion", () => {
  const css = fs.readFileSync(cssPath, "utf8");
  const rule = css.match(/\.reduce-motion \*,[^{]*\{([^}]*)\}/);
  assert.ok(rule, "a .reduce-motion rule must cover every element");
  assert.match(rule[1], /animation:\s*none\s*!important/);
  assert.match(rule[1], /transition:\s*none\s*!important/);
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

  app.runTimers();
  assert.ok(old.childNodes.length > 0, "queued timers must not discard the only usable imagery");
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

  app.runTimers();
  assert.ok(old.childNodes.length > 0, "queued timers must not discard partially replaced coverage");
  assert.equal(old.hidden, false);
});

test("an unloaded intermediate zoom layer keeps retained imagery through timers", () => {
  const app = loadApp();
  configureMap(app);
  const live = app.elements.get("tiles");
  const old = app.elements.get("tiles-old");
  for (const image of [...live.childNodes]) image.onload();
  app.context.Airloom.debugZoomTo(13);
  const retained = [...old.childNodes];
  assert.ok(retained.length > 0);

  app.context.Airloom.debugZoomTo(15);
  app.runTimers();

  assert.deepEqual([...old.childNodes], retained, "blank intermediate tiles must not replace usable imagery");
  assert.equal(old.hidden, false);
});

test("a partially loaded intermediate layer does not replace complete retired imagery", () => {
  const app = loadApp();
  configureMap(app);
  const live = app.elements.get("tiles");
  const old = app.elements.get("tiles-old");
  for (const image of [...live.childNodes]) image.onload();
  app.context.Airloom.debugZoomTo(13);
  const retained = [...old.childNodes];
  const intermediate = [...live.childNodes];
  intermediate[0].onload();
  for (const image of intermediate.slice(1)) image.onerror();

  app.context.Airloom.debugZoomTo(15);
  app.runTimers();

  assert.deepEqual([...old.childNodes], retained);
  assert.equal(old.hidden, false);
});

test("a completely successful replacement retires the old imagery", () => {
  const app = loadApp();
  configureMap(app);
  const live = app.elements.get("tiles");
  const old = app.elements.get("tiles-old");
  for (const image of [...live.childNodes]) image.onload();
  app.context.Airloom.debugZoomTo(13);
  assert.ok(old.childNodes.length > 0);

  for (const image of [...live.childNodes]) image.onload();

  assert.equal(old.childNodes.length, 0);
  assert.equal(old.hidden, true);
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

test("desktop interface scale is left to WebKit zoom instead of scaling CSS twice", () => {
  const app = loadApp();
  configureMap(app, { interface_scale: 1.5 });
  assert.equal(app.document.documentElement.style["--interface-scale"], "1");
  assert.equal(app.document.documentElement.dataset.interfaceScale, "1.5");

  receiveSensors(app, [fixtureSensor(1)], { config: { interface_scale: 1.25 } });
  assert.equal(app.document.documentElement.style["--interface-scale"], "1");
  assert.equal(app.document.documentElement.dataset.interfaceScale, "1.25");
  assert.equal(app.context.Airloom.debugState().viewportScale, null);
});

test("browser preview applies interface scale through CSS", () => {
  const app = loadApp({ preview: true });
  app.runTimers();
  configureMap(app, { interface_scale: 1.5 });
  assert.equal(app.document.documentElement.style["--interface-scale"], "1.5");
  configureMap(app, { interface_scale: 1.25 });
  assert.equal(app.document.documentElement.style["--interface-scale"], "1.25");
  configureMap(app, { interface_scale: 3 });
  assert.equal(app.document.documentElement.style["--interface-scale"], "1");
});

test("shell recovery status offers a bridged reload and clears when ready", () => {
  const app = loadApp();
  const status = app.elements.get("shell-status");
  const message = app.elements.get("shell-status-message");
  const reload = app.elements.get("reload-shell");
  assert.ok(status, "index.html needs a #shell-status region");
  assert.equal(status.hidden, true);

  app.context.Airloom.receive("shell-status", { status: "loading", message: "Reloading Airloom…", can_reload: false });
  assert.equal(status.hidden, false);
  assert.equal(message.textContent, "Reloading Airloom…");
  assert.equal(reload.hidden, true);

  app.context.Airloom.receive("shell-status", { status: "error", message: "Interface stopped", can_reload: true });
  assert.equal(status.hidden, false);
  assert.equal(message.textContent, "Interface stopped");
  assert.equal(reload.hidden, false);
  reload.click();
  assert.deepEqual(app.messages.filter((item) => item.action === "reload-shell"), [{ action: "reload-shell" }]);
  assert.equal(reload.hidden, true);

  app.context.Airloom.receive("shell-status", { status: "ready", message: "", can_reload: false });
  assert.equal(status.hidden, true);
});

test("shell error without reload permission shows no Reload button", () => {
  const app = loadApp();
  app.context.Airloom.receive("shell-status", { status: "error", message: "Broken", can_reload: false });
  assert.equal(app.elements.get("shell-status").hidden, false);
  assert.equal(app.elements.get("reload-shell").hidden, true);
});

test("interface scale changes computed sensor, AQI, chart, preference, and control sizes", () => {
  const cases = [
    [".sensor-copy strong", "font-size", 13],
    [".sensor-copy small", "font-size", 10],
    [".aqi-number", "font-size", 70],
    [".chart-heading span", "font-size", 10],
    [".form-grid > label:not(.checkbox)", "font-size", 11],
    [".form-grid input[type=\"text\"]", "height", 39],
    [".form-grid select", "height", 39],
    [".map-controls button", "height", 38],
    [".dialog-actions button", "height", 38],
  ];
  for (const scale of [1, 1.25, 1.5]) {
    for (const [selector, property, baseline] of cases) {
      assert.equal(computedCssPixels(selector, property, scale), baseline * scale, `${selector} ${property} at ${scale}x`);
    }
    assert.equal(computedCssPixels(".tile", "width", scale), 256, "tile width must stay in map coordinates");
    assert.equal(computedCssPixels(".tile", "height", scale), 256, "tile height must stay in map coordinates");
  }
});

test("scaled preference selects retain visible keyboard focus", () => {
  assert.ok(cssDeclaration("select:focus-visible", "outline"));
  assert.ok(cssDeclaration(".form-grid select:focus", "box-shadow"));
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

test("narrow layout computes scaled top controls below a full-width search", () => {
  const css = fs.readFileSync(cssPath, "utf8");
  const narrow = css.split("@media (max-width: 640px) {")[1]?.split("@media (prefers-color-scheme:")[0] || "";
  assert.match(narrow, /\.search-overlay\s*\{[^}]*width:\s*calc\(100vw - 20px\)/);
  for (const scale of [1, 1.25, 1.5]) {
    assert.equal(computedCssPixels(".summary-chip", "top", scale, { narrow: true }), 64 * scale);
    assert.equal(computedCssPixels(".summary-chip .summary-score", "height", scale, { narrow: true }), 40 * scale);
    assert.equal(computedCssPixels(".search-pill svg", "width", scale, { narrow: true }), 17 * scale);
  }
});

test("scaled narrow controls shrink inside the viewport instead of overflowing", () => {
  // Real-layout coverage lives in scripts/test-ui-regression (layouts); this
  // guards the declarations that let flex/grid children shrink below content.
  assert.equal(cssDeclaration(".search-pill input", "min-width"), "0");
  assert.equal(cssDeclaration(".search-pill", "min-width"), "0");
  assert.equal(cssDeclaration(".search-pill kbd", "flex"), "none");
  assert.equal(cssDeclaration(".map-popup", "max-width"), "calc(100vw - 24px)");
  assert.match(cssDeclaration(".corner-buttons", "flex-wrap"), /wrap/);
  assert.equal(cssDeclaration(".form-grid", "grid-template-columns", { narrow: true }), "minmax(0, 1fr)");
  assert.equal(cssDeclaration("fieldset", "min-width"), "0");
});

test("demo and coverage provenance remain visible near AQI", () => {
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

  receiveSensors(app, [fixtureSensor(1)], { coverage: { capped: true, complete: false } });
  assert.equal(summary.hidden, true);
  assert.equal(detail.hidden, true);
  assert.equal(app.elements.get("coverage-status").hidden, false);
  receiveSensors(app, [fixtureSensor(1)], { coverage: { capped: false, complete: true } });
  assert.equal(app.elements.get("coverage-status").hidden, true);
});

test("selecting a demo favorite does not inherit the live area summary provenance", () => {
  const app = loadApp();
  configureMap(app);
  app.elements.get("sensors-panel").hidden = false;
  app.context.Airloom.receive("sensors", {
    area: [fixtureSensor(1, { origin: "live", delivery: "network" })],
    favorites: [fixtureSensor(2, { origin: "demo", delivery: "network", favorite: true })],
    selected_id: 1,
    source: "PurpleAir live",
    stale: false,
  });
  const summary = app.elements.get("summary-provenance");
  const detail = app.elements.get("detail-provenance");
  assert.equal(summary.hidden, true);
  assert.equal(detail.hidden, true);

  app.elements.get("favorites-list").querySelectorAll(".sensor-row")[0].click();

  assert.equal(summary.hidden, true, "a remote favorite must not change the area summary label");
  assert.equal(detail.hidden, false);
  assert.equal(detail.textContent, "Demo data");
});

test("mixed stale area and cached favorite publications retain their own provenance", () => {
  const app = loadApp();
  configureMap(app);
  app.elements.get("sensors-panel").hidden = false;
  app.context.Airloom.receive("sensors", {
    area: [fixtureSensor(1, { origin: "live", delivery: "cache" })],
    favorites: [fixtureSensor(2, { origin: "live", delivery: "cache", favorite: true })],
    selected_id: 2,
    source: "PurpleAir · cached",
    stale: true,
  });
  const summary = app.elements.get("summary-provenance");
  const detail = app.elements.get("detail-provenance");
  assert.equal(summary.textContent, "Stale cached data");
  assert.equal(detail.textContent, "Cached data", "area stale metadata must not relabel a separate favorite fetch");

  app.elements.get("sensor-list").querySelectorAll(".sensor-row")[0].click();
  assert.equal(summary.textContent, "Stale cached data");
  assert.equal(detail.textContent, "Stale cached data", "selecting the area reading applies the area stale metadata");
});

test("actual legacy cached fallback payload is labeled stale", () => {
  const app = loadApp();
  configureMap(app);
  receiveSensors(app, [fixtureSensor(1, { delivery: "cache" })], { source: "PurpleAir · cached" });

  assert.equal(app.elements.get("summary-provenance").textContent, "Stale cached data");
  assert.equal(app.elements.get("detail-provenance").textContent, "Stale cached data");
});

test("explicit stale provenance overrides the legacy source label", () => {
  const app = loadApp();
  configureMap(app);
  receiveSensors(app, [fixtureSensor(1, { delivery: "cache" })], { source: "PurpleAir · cached", stale: true });

  assert.equal(app.elements.get("summary-provenance").textContent, "Stale cached data");
  assert.equal(app.elements.get("detail-provenance").textContent, "Stale cached data");
});

test("ordinary cached payloads remain cached instead of becoming stale", () => {
  const app = loadApp();
  configureMap(app);
  const summary = app.elements.get("summary-provenance");
  const detail = app.elements.get("detail-provenance");

  receiveSensors(app, [fixtureSensor(1, { delivery: "cache" })], { source: "PurpleAir · cached", stale: false });
  assert.equal(summary.textContent, "Cached data");
  assert.equal(detail.textContent, "Cached data");

  receiveSensors(app, [fixtureSensor(1, { delivery: "cache" })], { source: "PurpleAir · cached startup" });
  assert.equal(summary.textContent, "Cached data");
  assert.equal(detail.textContent, "Cached data");
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

test("reconcileKeyed tolerates text and comment children", () => {
  const { reconcileKeyed } = require("../../airloom/resources/state.js");
  const document = new FakeElement("document");
  document.activeElement = null;
  const container = new FakeElement("div");
  container.ownerDocument = document;
  const makeTextNode = () => ({
    parentNode: container,
    isConnected: true,
    contains: () => false,
    remove() {
      const index = container.childNodes.indexOf(this);
      if (index >= 0) container.childNodes.splice(index, 1);
      this.parentNode = null;
      this.isConnected = false;
    },
  });
  container.childNodes.push(makeTextNode(), makeTextNode());
  const create = (item) => {
    const node = new FakeElement("button");
    node.ownerDocument = document;
    node.textContent = item.name;
    return node;
  };

  assert.doesNotThrow(() => reconcileKeyed(container, [{ id: 1, name: "One" }], (item) => item.id, create, () => {}));
  assert.deepEqual(container.childNodes.map((node) => node.dataset.key), ["1"]);
  assert.equal(container.childNodes[0].textContent, "One");
});

test("main search typing filters locally without remote geocoding", () => {
  const app = loadApp();
  configureMap(app);
  receiveSensors(app, [fixtureSensor(1, { name: "Alpha Station" }), fixtureSensor(2, { name: "Beta Station" })]);
  const search = app.elements.get("search");
  search.value = "Alpha";
  search.dispatch("input", { target: search });

  app.runTimers();

  assert.equal(app.context.Airloom.debugState().visibleCount, 1);
  assert.equal(app.messages.filter((message) => message.action === "place-search").length, 0);
});

test("home search typing sends no remote geocoding", () => {
  const app = loadApp();
  configureMap(app);
  openSettings(app);
  const input = app.elements.get("home-place-input");
  input.value = "Portland";
  input.dispatch("input", { target: input });

  app.runTimers();

  assert.equal(app.messages.filter((message) => message.action === "place-search").length, 0);
});

test("main search submit sends one identified main-context request", () => {
  const app = loadApp();
  const search = app.elements.get("search");
  search.value = "Portland";
  search.dispatch("input", { target: search });
  search.dispatch("keydown", { key: "Enter" });

  const requests = app.messages.filter((message) => message.action === "place-search");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].query, "Portland");
  assert.equal(requests[0].context, "main");
  assert.equal(Number.isInteger(requests[0].request_id), true);
});

test("home search submit sends one identified home-context request", () => {
  const app = loadApp();
  configureMap(app);
  openSettings(app);
  const input = app.elements.get("home-place-input");
  input.value = "Portland";
  input.dispatch("input", { target: input });
  input.dispatch("keydown", { key: "Enter" });

  const requests = app.messages.filter((message) => message.action === "place-search");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].query, "Portland");
  assert.equal(requests[0].context, "home");
  assert.equal(Number.isInteger(requests[0].request_id), true);
});

test("an older same-query place response cannot replace the newest request", () => {
  const app = loadApp();
  const search = app.elements.get("search");
  search.value = "Paris";
  search.dispatch("input", { target: search });
  search.dispatch("keydown", { key: "Enter" });
  search.dispatch("keydown", { key: "Enter" });
  const results = app.elements.get("search-results");

  app.context.Airloom.receive("places", {
    request_id: 1,
    context: "main",
    query: "Paris",
    results: [{ name: "Obsolete Paris", latitude: 48.85, longitude: 2.35 }],
  });
  assert.equal(results.hidden, true);

  app.context.Airloom.receive("places", {
    request_id: 2,
    context: "main",
    query: "Paris",
    results: [{ name: "Current Paris", latitude: 48.85, longitude: 2.35 }],
  });
  assert.equal(results.hidden, false);
  assert.match(results.innerHTML, /Current Paris/);
  assert.doesNotMatch(results.innerHTML, /Obsolete Paris/);
});

test("a place response with the wrong context is ignored", () => {
  const app = loadApp();
  const search = app.elements.get("search");
  search.value = "Portland";
  search.dispatch("input", { target: search });
  search.dispatch("keydown", { key: "Enter" });
  const results = app.elements.get("search-results");

  app.context.Airloom.receive("places", {
    request_id: 1,
    context: "home",
    query: "Portland",
    results: [{ name: "Wrong context", latitude: 45.5, longitude: -122.6 }],
  });
  assert.equal(results.hidden, true);

  app.context.Airloom.receive("places", {
    request_id: 1,
    context: "main",
    query: "Portland",
    results: [{ name: "Main Portland", latitude: 45.5, longitude: -122.6 }],
  });
  assert.equal(results.hidden, false);
  assert.match(results.innerHTML, /Main Portland/);
});

test("main place selection hides results and restores focus to search", () => {
  const app = loadApp();
  configureMap(app);
  const search = app.elements.get("search");
  search.value = "Paris";
  search.dispatch("input", { target: search });
  search.dispatch("keydown", { key: "Enter" });
  app.context.Airloom.receive("places", {
    request_id: 1,
    context: "main",
    query: "Paris",
    results: [{ name: "Paris, France", latitude: 48.85, longitude: 2.35 }],
  });
  const results = app.elements.get("search-results");
  const place = results.querySelectorAll("button")[0];
  place.focus();

  place.click();

  assert.equal(results.hidden, true);
  assert.equal(app.document.activeElement, search);
});

test("home place selection hides results and restores focus to its search input", () => {
  const app = loadApp();
  configureMap(app);
  openSettings(app, { home_mode: "fixed" });
  const input = app.elements.get("home-place-input");
  input.value = "Paris";
  input.dispatch("input", { target: input });
  input.dispatch("keydown", { key: "Enter" });
  app.context.Airloom.receive("places", {
    request_id: 1,
    context: "home",
    query: "Paris",
    results: [{ name: "Paris, France", latitude: 48.85, longitude: 2.35 }],
  });
  const results = app.elements.get("home-place-results");
  const place = results.querySelectorAll("button")[0];
  place.focus();

  place.click();

  assert.equal(results.hidden, true);
  assert.equal(app.document.activeElement, input);
  assert.equal(app.elements.get("settings-form").elements.home_lat.value, 48.85);
});

test("closing Preferences invalidates pending home search responses", () => {
  const app = loadApp();
  configureMap(app);
  const search = app.elements.get("search");
  search.value = "Paris";
  search.dispatch("input", { target: search });
  search.dispatch("keydown", { key: "Enter" });
  openSettings(app);
  const home = app.elements.get("home-place-input");
  home.value = "Paris";
  home.dispatch("input", { target: home });
  home.dispatch("keydown", { key: "Enter" });
  app.elements.get("close-settings").click();

  const cancellations = app.messages.filter((message) => message.action === "cancel-place-search");
  assert.deepEqual(cancellations.map((message) => message.context), ["home"]);

  app.context.Airloom.receive("places", {
    request_id: 2,
    context: "home",
    query: "Paris",
    results: [{ name: "Closed home result", latitude: 48.85, longitude: 2.35 }],
  });

  assert.equal(app.elements.get("home-place-results").hidden, true);
  assert.equal(app.elements.get("search-results").hidden, true);
});

test("trend unavailable is terminal instead of indefinite loading", () => {
  const app = loadApp();
  configureMap(app, { has_api_key: true });
  receiveSensors(app, [fixtureSensor(1, { last_seen: 100 })], { config: { has_api_key: true } });

  app.context.Airloom.receive("trend", { sensor_id: 1, request_id: 1, status: "unavailable", reading_last_seen: 100, trend: [] });

  assert.match(app.elements.get("chart").innerHTML, /Trend unavailable/);
  assert.doesNotMatch(app.elements.get("chart").innerHTML, /Loading trend/);
  assert.equal(app.elements.get("trend-direction").textContent, "—");
});

test("trend loaded renders its rolling-average points", () => {
  const app = loadApp();
  configureMap(app, { has_api_key: true });
  receiveSensors(app, [fixtureSensor(1, { last_seen: 100 })], { config: { has_api_key: true } });

  app.context.Airloom.receive("trend", {
    sensor_id: 1,
    request_id: 1,
    status: "loaded",
    reading_last_seen: 100,
    trend: [{ label: "1w", aqi: 40 }, { label: "Now", aqi: 48 }],
  });

  assert.match(app.elements.get("chart").innerHTML, /<svg/);
  assert.equal(app.elements.get("trend-direction").textContent, "Up 8 points");
});

test("trend errors allow at most three manual retries", () => {
  const app = loadApp();
  configureMap(app, { has_api_key: true });
  receiveSensors(app, [fixtureSensor(1, { last_seen: 100 })], { config: { has_api_key: true } });
  app.context.Airloom.receive("trend", { sensor_id: 1, request_id: 1, status: "error", reading_last_seen: 100, trend: [] });
  const retry = app.elements.get("trend-retry");
  assert.ok(retry, "an error should expose a manual retry control");
  assert.equal(retry.hidden, false);

  for (let attempt = 1; attempt <= 3; attempt++) {
    retry.click();
    assert.equal(app.messages.filter((message) => message.action === "select" && message.id === 1).length, attempt);
    app.context.Airloom.receive("trend", { sensor_id: 1, request_id: attempt + 1, status: "error", reading_last_seen: 100, trend: [] });
  }
  assert.equal(retry.disabled, true);
  retry.click();
  assert.equal(app.messages.filter((message) => message.action === "select" && message.id === 1).length, 3);
  assert.match(app.elements.get("chart").innerHTML, /Trend unavailable/);
});

test("rolling-average charts name the actual current comparison", () => {
  const app = loadApp();
  configureMap(app);
  receiveSensors(app, [fixtureSensor(1, { last_seen: 100, trend: [{ label: "1w", aqi: 40 }, { label: "Now", aqi: 48 }] })]);
  const period = app.elements.get("chart-period");
  assert.ok(period, "the chart should expose its comparison label");
  assert.equal(period.textContent, "Current vs 1-week average");
  assert.equal(app.elements.get("trend-direction").textContent, "Up 8 points");

  receiveSensors(app, [fixtureSensor(1, { last_seen: 200, trend: [{ label: "1d", aqi: 30 }, { label: "Now", aqi: 28 }] })]);
  assert.equal(app.elements.get("chart-period").textContent, "Current vs 1-day average");
  assert.equal(app.elements.get("trend-direction").textContent, "Holding steady");
});

test("preview detail favorite toggles local state and pressed state", () => {
  const app = loadApp({ preview: true });
  app.runTimers();
  const favorite = app.elements.get("favorite-button");
  assert.equal(favorite.getAttribute("aria-pressed"), "true");

  favorite.click();
  assert.equal(favorite.getAttribute("aria-pressed"), "false");
  favorite.click();
  assert.equal(favorite.getAttribute("aria-pressed"), "true");
});

test("preview Refresh reloads demo data locally and says so", () => {
  const app = loadApp({ preview: true });
  app.runTimers();

  app.elements.get("footer-refresh").click();

  assert.equal(app.elements.get("data-source").textContent, "Browser preview · Demo data");
  assert.equal(app.elements.get("ui-status").textContent, "Browser preview demo data refreshed locally.");
});

test("preview preferences apply supported values locally without claiming persistence", () => {
  const app = loadApp({ preview: true });
  app.runTimers();
  openSettings(app);
  const form = app.elements.get("settings-form");
  form.elements.temperature_unit.value = "C";

  form.dispatch("submit", { submitter: app.elements.get("save-settings") });

  assert.equal(app.elements.get("temperature").textContent, "18°C");
  assert.match(app.elements.get("ui-status").textContent, /browser preview only/i);
  assert.equal(app.elements.get("settings-dialog").open, false);
});

test("preview settings disable unsupported API-key persistence with an explanation", () => {
  const app = loadApp({ preview: true });
  app.runTimers();

  openSettings(app);

  assert.equal(app.elements.get("settings-form").elements.api_key.disabled, true);
  assert.match(app.elements.get("settings-intro").textContent, /browser preview/i);
});

test("preview place submission explains that remote search needs the desktop app", () => {
  const app = loadApp({ preview: true });
  app.runTimers();
  const search = app.elements.get("search");
  search.value = "Portland";
  search.dispatch("input", { target: search });

  search.dispatch("keydown", { key: "Enter" });

  assert.match(app.elements.get("ui-status").textContent, /desktop app/i);
  assert.match(app.elements.get("search-results").innerHTML, /unavailable in browser preview/i);
});

test("desktop Save waits for its matching success acknowledgment", () => {
  const app = loadApp();
  configureMap(app);
  openSettings(app);
  const form = app.elements.get("settings-form");
  const dialog = app.elements.get("settings-dialog");
  const save = app.elements.get("save-settings");

  form.dispatch("submit", { submitter: save });
  const requests = app.messages.filter((message) => message.action === "save-settings");
  assert.equal(requests.length, 1);
  assert.equal(Number.isInteger(requests[0].request_id), true);
  assert.equal(dialog.open, true);
  assert.equal(save.disabled, true);

  app.context.Airloom.receive("config", { latitude: 0, longitude: 0, location_name: "Unrelated config" });
  app.context.Airloom.receive("error", { message: "Unrelated error" });
  app.context.Airloom.receive("settings-save-result", { request_id: requests[0].request_id + 1, status: "success", error: null });
  app.context.Airloom.receive("settings-save-result", { request_id: requests[0].request_id + 1, status: "error", error: "Other" });
  assert.equal(dialog.open, true);
  assert.equal(save.disabled, true);

  app.context.Airloom.receive("settings-save-result", { request_id: requests[0].request_id, status: "success", error: null });
  assert.equal(dialog.open, false);
  assert.equal(save.disabled, false);
});

test("matching Save failure keeps Preferences open and a stale success cannot close a retry", () => {
  const app = loadApp();
  configureMap(app);
  openSettings(app);
  const form = app.elements.get("settings-form");
  const dialog = app.elements.get("settings-dialog");
  const save = app.elements.get("save-settings");
  const error = app.elements.get("settings-error");
  assert.ok(error, "Preferences needs an in-dialog save error region");
  assert.equal(error.hidden, true);

  form.dispatch("submit", { submitter: save });
  const firstId = app.messages.filter((message) => message.action === "save-settings")[0].request_id;
  app.context.Airloom.receive("settings-save-result", { request_id: firstId, status: "error", error: "Disk full; try again" });
  assert.equal(dialog.open, true);
  assert.equal(save.disabled, false);
  assert.equal(error.hidden, false);
  assert.match(error.textContent, /Disk full; try again/);

  form.dispatch("submit", { submitter: save });
  const secondId = app.messages.filter((message) => message.action === "save-settings")[1].request_id;
  assert.notEqual(secondId, firstId);
  assert.equal(save.disabled, true);
  assert.equal(error.hidden, true);
  app.context.Airloom.receive("settings-save-result", { request_id: firstId, status: "success", error: null });
  assert.equal(dialog.open, true);
  assert.equal(save.disabled, true);

  app.context.Airloom.receive("settings-save-result", { request_id: secondId, status: "error", error: "Still unwritable" });
  assert.equal(dialog.open, true);
  assert.equal(save.disabled, false);
  assert.match(error.textContent, /Still unwritable/);
});

test("a Save acknowledgment arriving after Preferences closed is ignored", () => {
  const app = loadApp();
  configureMap(app);
  openSettings(app);
  app.elements.get("settings-form").dispatch("submit", { submitter: app.elements.get("save-settings") });
  const requestId = app.messages.find((message) => message.action === "save-settings").request_id;
  app.elements.get("cancel-settings").click();
  openSettings(app);

  app.context.Airloom.receive("settings-save-result", { request_id: requestId, status: "success", error: null });

  assert.equal(app.elements.get("settings-dialog").open, true);
});

test("Preferences offers a labeled Interface size selector with three sizes", () => {
  const html = fs.readFileSync(indexPath, "utf8");
  const label = html.match(/<label[^>]*>\s*<span>Interface size<\/span>\s*<select name="interface_scale"[^>]*>([\s\S]*?)<\/select>/);
  assert.ok(label, "an Interface size label must wrap the interface_scale select");
  const options = [...label[1].matchAll(/<option value="([^"]+)">([^<]+)<\/option>/g)].map((match) => [match[1], match[2]]);
  assert.deepEqual(options, [["1", "100%"], ["1.25", "125%"], ["1.5", "150%"]]);
});

test("desktop Save round-trips the chosen interface size with its request id", () => {
  const app = loadApp();
  configureMap(app, { interface_scale: 1.25 });
  openSettings(app, { interface_scale: 1.25 });
  const form = app.elements.get("settings-form");
  assert.equal(form.elements.interface_scale.value, "1.25");

  form.elements.interface_scale.value = "1.5";
  form.dispatch("submit", { submitter: app.elements.get("save-settings") });

  const request = app.messages.find((message) => message.action === "save-settings");
  assert.equal(request.interface_scale, 1.5);
  assert.equal(Number.isInteger(request.request_id), true);
  app.context.Airloom.receive("config", { interface_scale: 1.5 });
  app.context.Airloom.receive("settings-save-result", { request_id: request.request_id, status: "success", error: null });
  assert.equal(app.elements.get("settings-dialog").open, false);
  assert.equal(app.document.documentElement.dataset.interfaceScale, "1.5");
});

test("preview Interface size applies through CSS without the bridge", () => {
  const app = loadApp({ preview: true });
  app.runTimers();
  openSettings(app);
  const form = app.elements.get("settings-form");
  assert.equal(form.elements.interface_scale.value, "1");
  form.elements.interface_scale.value = "1.25";

  form.dispatch("submit", { submitter: app.elements.get("save-settings") });

  assert.equal(app.document.documentElement.style["--interface-scale"], "1.25");
  assert.equal(app.elements.get("settings-dialog").open, false);
});

test("preview demo Now point equals the displayed current AQI", () => {
  const app = loadApp({ preview: true });
  app.runTimers();
  const row = app.elements.get("sensor-list").querySelectorAll(".sensor-row").find((item) => item.dataset.id === "8001");
  assert.ok(row);

  row.click();

  assert.equal(app.elements.get("aqi-number").textContent, "34");
  assert.equal(app.elements.get("trend-direction").textContent, "Down 9 points");
});

test("a newer empty reading invalidates a loaded trend and rejects its late events", () => {
  const app = loadApp();
  configureMap(app, { has_api_key: true });
  receiveSensors(app, [fixtureSensor(1, {
    aqi: 48,
    last_seen: 100,
    trend: [{ label: "1w", aqi: 40 }, { label: "Now", aqi: 48 }],
  })], { config: { has_api_key: true } });
  app.context.Airloom.receive("trend", {
    sensor_id: 1,
    request_id: 1,
    status: "loaded",
    reading_last_seen: 100,
    trend: [{ label: "1w", aqi: 40 }, { label: "Now", aqi: 48 }],
  });
  assert.equal(app.elements.get("trend-direction").textContent, "Up 8 points");

  receiveSensors(app, [fixtureSensor(1, { aqi: 90, last_seen: 200, trend: [] })], { config: { has_api_key: true } });

  assert.equal(app.elements.get("aqi-number").textContent, "90");
  assert.match(app.elements.get("chart").innerHTML, /Trend not loaded/);
  assert.equal(app.elements.get("trend-direction").textContent, "—");
  assert.equal(app.elements.get("chart-period").textContent, "Rolling averages");

  app.context.Airloom.receive("trend", {
    sensor_id: 1,
    request_id: 2,
    status: "loaded",
    reading_last_seen: 100,
    trend: [{ label: "1w", aqi: 40 }, { label: "Now", aqi: 48 }],
  });
  assert.match(app.elements.get("chart").innerHTML, /Trend not loaded/);
  assert.equal(app.elements.get("trend-direction").textContent, "—");
});

test("same-reading inline arrays cannot replace an explicit loaded event", () => {
  const app = loadApp();
  configureMap(app, { has_api_key: true });
  const publication = () => fixtureSensor(1, {
    aqi: 48,
    last_seen: 100,
    trend: [{ label: "1w", aqi: 40 }, { label: "Now", aqi: 45 }],
  });
  receiveSensors(app, [publication()], { config: { has_api_key: true } });
  app.context.Airloom.receive("trend", {
    sensor_id: 1,
    request_id: 1,
    status: "loaded",
    reading_last_seen: 100,
    trend: [{ label: "1w", aqi: 40 }, { label: "Now", aqi: 48 }],
  });
  assert.equal(app.elements.get("trend-direction").textContent, "Up 8 points");

  receiveSensors(app, [publication()], { config: { has_api_key: true } });

  assert.equal(app.elements.get("trend-direction").textContent, "Up 8 points");
  assert.equal(app.elements.get("chart-period").textContent, "Current vs 1-week average");
});

test("same-reading inline arrays cannot erase unavailable", () => {
  const app = loadApp();
  configureMap(app, { has_api_key: true });
  const publication = fixtureSensor(1, {
    last_seen: 100,
    trend: [{ label: "1w", aqi: 40 }, { label: "Now", aqi: 48 }],
  });
  receiveSensors(app, [publication], { config: { has_api_key: true } });
  app.context.Airloom.receive("trend", { sensor_id: 1, request_id: 1, status: "unavailable", reading_last_seen: 100, trend: [] });

  receiveSensors(app, [publication], { config: { has_api_key: true } });

  assert.match(app.elements.get("chart").innerHTML, /Trend unavailable/);
  assert.doesNotMatch(app.elements.get("chart").innerHTML, /<svg/);
  assert.equal(app.elements.get("trend-direction").textContent, "—");
  assert.equal(app.elements.get("trend-retry").hidden, true);
});

test("same-reading publications preserve retries and only newer success resets them", () => {
  const app = loadApp();
  configureMap(app, { has_api_key: true });
  const publication = fixtureSensor(1, {
    aqi: 48,
    last_seen: 100,
    trend: [{ label: "1w", aqi: 40 }, { label: "Now", aqi: 48 }],
  });
  receiveSensors(app, [publication], { config: { has_api_key: true } });
  app.context.Airloom.receive("trend", { sensor_id: 1, request_id: 1, status: "error", reading_last_seen: 100, trend: [] });
  const retry = app.elements.get("trend-retry");

  receiveSensors(app, [publication], { config: { has_api_key: true } });
  assert.equal(retry.hidden, false);
  assert.match(app.elements.get("chart").innerHTML, /Trend unavailable/);

  for (let attempt = 1; attempt <= 3; attempt++) {
    retry.click();
    assert.equal(app.messages.filter((message) => message.action === "select" && message.id === 1).length, attempt);
    receiveSensors(app, [publication], { config: { has_api_key: true } });
    assert.match(app.elements.get("chart").innerHTML, /Loading trend/);
    assert.equal(retry.hidden, true);

    app.context.Airloom.receive("trend", { sensor_id: 1, request_id: attempt + 1, status: "error", reading_last_seen: 100, trend: [] });
    receiveSensors(app, [publication], { config: { has_api_key: true } });
    assert.match(app.elements.get("chart").innerHTML, /Trend unavailable/);
    assert.equal(retry.hidden, false);
    assert.equal(retry.disabled, attempt === 3);
  }

  receiveSensors(app, [fixtureSensor(1, {
    aqi: 90,
    last_seen: 200,
    trend: [{ label: "1w", aqi: 80 }, { label: "Now", aqi: 90 }],
  })], { config: { has_api_key: true } });
  assert.equal(app.elements.get("trend-direction").textContent, "Up 10 points");
  assert.equal(retry.hidden, true);

  app.context.Airloom.receive("trend", { sensor_id: 1, request_id: 5, status: "error", reading_last_seen: 200, trend: [] });
  assert.equal(retry.disabled, false);
  retry.click();
  assert.equal(app.messages.filter((message) => message.action === "select" && message.id === 1).length, 4);
});

test("an older trend request cannot overwrite newer explicit state", () => {
  const app = loadApp();
  configureMap(app, { has_api_key: true });
  receiveSensors(app, [fixtureSensor(1, { last_seen: 100 })], { config: { has_api_key: true } });
  app.context.Airloom.receive("trend", { sensor_id: 1, request_id: 2, status: "loading", reading_last_seen: 100, trend: [] });

  app.context.Airloom.receive("trend", { sensor_id: 1, request_id: 1, status: "error", reading_last_seen: 100, trend: [] });

  assert.match(app.elements.get("chart").innerHTML, /Loading trend/);
  assert.equal(app.elements.get("trend-retry").hidden, true);

  app.context.Airloom.receive("trend", {
    sensor_id: 1,
    request_id: 3,
    status: "loaded",
    reading_last_seen: 100,
    trend: [{ label: "1w", aqi: 40 }, { label: "Now", aqi: 48 }],
  });
  assert.equal(app.elements.get("trend-direction").textContent, "Up 8 points");
});

test("a newer empty publication preserves an exhausted retry budget", () => {
  const app = loadApp();
  configureMap(app, { has_api_key: true });
  receiveSensors(app, [fixtureSensor(1, {
    last_seen: 100,
    trend: [{ label: "1w", aqi: 40 }, { label: "Now", aqi: 48 }],
  })], { config: { has_api_key: true } });
  app.context.Airloom.receive("trend", { sensor_id: 1, request_id: 1, status: "error", reading_last_seen: 100, trend: [] });
  const retry = app.elements.get("trend-retry");
  for (let attempt = 1; attempt <= 3; attempt++) {
    retry.click();
    app.context.Airloom.receive("trend", { sensor_id: 1, request_id: attempt + 1, status: "error", reading_last_seen: 100, trend: [] });
  }
  assert.equal(retry.disabled, true);

  receiveSensors(app, [fixtureSensor(1, { aqi: 90, last_seen: 200, trend: [] })], { config: { has_api_key: true } });
  assert.match(app.elements.get("chart").innerHTML, /Trend not loaded/);
  assert.equal(retry.hidden, true);

  app.context.Airloom.receive("trend", { sensor_id: 1, request_id: 5, status: "error", reading_last_seen: 200, trend: [] });
  assert.equal(retry.hidden, false);
  assert.equal(retry.disabled, true);
  assert.equal(retry.textContent, "Retry limit reached");
  retry.click();
  assert.equal(app.messages.filter((message) => message.action === "select" && message.id === 1).length, 3);
});

function runPythonAdapter(messages, { failWrites = false, sensors = [] } = {}) {
  const output = require("node:child_process").execFileSync(
    "python3",
    [path.resolve(__dirname, "fixtures/bridge_adapter.py")],
    {
      cwd: path.resolve(__dirname, "../.."),
      env: { ...process.env, PYTHONPATH: path.resolve(__dirname, "../..") },
      input: JSON.stringify({ messages, fail_writes: failWrites, sensors }),
      encoding: "utf8",
    },
  );
  return JSON.parse(output);
}

test("a failed filter save leaves the chip on the filter Python still applies", () => {
  const app = loadApp();
  configureMap(app, { location_filter: "outdoor" });
  const chip = app.elements.get("filter-chip");
  assert.equal(chip.textContent, "Outdoor");

  chip.click();
  const bridged = app.messages.filter((message) => message.action === "set-location-filter");
  assert.deepEqual(bridged, [{ action: "set-location-filter", value: "indoor" }]);
  const result = runPythonAdapter(bridged, { failWrites: true });
  assert.equal(result.location_filter, "outdoor");
  for (const [event, payload] of result.events) app.context.Airloom.receive(event, payload);

  assert.equal(chip.textContent, "Outdoor");
  assert.equal(app.context.Airloom.debugState().location_filter, "outdoor");
  const toasts = app.elements.get("toast-stack").childNodes.map((node) => node.textContent);
  assert.ok(toasts.some((text) => text.includes("Could not save location filter")), toasts.join("|"));
});

test("a successful filter save updates the chip from Python's acknowledgment", () => {
  const app = loadApp();
  configureMap(app, { location_filter: "outdoor" });
  const chip = app.elements.get("filter-chip");

  chip.click();
  const bridged = app.messages.filter((message) => message.action === "set-location-filter");
  const result = runPythonAdapter(bridged);
  assert.equal(result.location_filter, "indoor");
  for (const [event, payload] of result.events) app.context.Airloom.receive(event, payload);

  assert.equal(chip.textContent, "Indoor");
  assert.equal(app.context.Airloom.debugState().location_filter, "indoor");
});

test("rapid filter clicks before acknowledgment advance from the requested filter", () => {
  const app = loadApp();
  configureMap(app, { location_filter: "indoor" });
  const chip = app.elements.get("filter-chip");

  chip.click();
  chip.click();

  const values = app.messages.filter((message) => message.action === "set-location-filter").map((message) => message.value);
  assert.deepEqual(values, ["both", "outdoor"]);
  assert.equal(chip.textContent, "Indoor", "the chip waits for Python's acknowledgment");
  app.context.Airloom.receive("error", { message: "Could not save location filter: denied" });
  chip.click();
  assert.equal(app.messages.at(-1).value, "both", "a failure releases the unacknowledged request");
});

test("the largest safe sensor id round-trips Python to JS to Python exactly", () => {
  const SAFE_MAX = 9007199254740991;
  const seeded = [{ id: SAFE_MAX, origin: "demo" }, { id: 1, origin: "demo" }];
  const { published } = runPythonAdapter([], { sensors: seeded });
  assert.deepEqual(published.map((item) => item.id), [SAFE_MAX, 1]);
  const app = loadApp();
  configureMap(app);
  app.context.Airloom.receive("sensors", { items: published, selected_id: 1, source: "Demo data" });

  const row = app.elements.get("sensor-list").querySelectorAll(".sensor-row")
    .find((node) => node.dataset.id === String(SAFE_MAX));
  assert.ok(row, "the safe-maximum sensor has a list row");
  row.click();
  const bridged = app.messages.filter((message) => message.action === "select");
  assert.deepEqual(bridged.map((message) => message.id), [SAFE_MAX]);

  const result = runPythonAdapter(bridged, { sensors: seeded });
  assert.equal(result.selected_id, SAFE_MAX);
  const trend = result.events.find(([event]) => event === "trend")[1];
  assert.equal(trend.sensor_id, SAFE_MAX);
});

// Shell recovery (W2): restore-view adopts only a canonical viewport. The
// vm timeout bounds each delivery, so a payload that hangs rendering fails
// the test instead of stalling the suite.
function deliverRestoreView(app, payload) {
  app.context.__restorePayload = payload;
  vm.runInContext('Airloom.receive("restore-view", __restorePayload)', app.context, { timeout: 2000 });
}

function mapView(app) {
  const { center, zoom } = app.context.Airloom.debugState();
  return { lat: center.lat, lon: center.lon, zoom };
}

test("restore-view ignores malformed payloads without throwing or navigating", () => {
  const malformed = [
    null,
    "15,-20",
    [15, -20, 9],
    42,
    { lat: Number.NaN, lon: -20, zoom: 9 },
    { lat: 15, lon: Number.POSITIVE_INFINITY, zoom: 9 },
    { lat: 0, lon: 1e308, zoom: 9 },
    { lat: 95, lon: -20, zoom: 9 },
    { lat: 15, lon: -20, zoom: 2 },
    { lat: 15, lon: -20, zoom: 18 },
    { lat: 15, lon: -20, zoom: 2.9 },
    { lat: 15, lon: -20, zoom: 17.2 },
    { lat: 15, lon: -20, zoom: "9" },
    { lat: null, lon: -20, zoom: 9 },
    { lat: "15", lon: -20, zoom: 9 },
    { lat: true, lon: -20, zoom: 9 },
    { lon: -20, zoom: 9 },
    { lat: 15, zoom: 9 },
    { lat: 15, lon: -20 },
  ];
  for (const payload of malformed) {
    const app = loadApp();
    configureMap(app);
    const before = mapView(app);
    assert.doesNotThrow(() => deliverRestoreView(app, payload), `payload ${JSON.stringify(payload)}`);
    assert.deepEqual(mapView(app), before, `payload ${JSON.stringify(payload)} moved the map`);
    app.runTimers();
    assert.deepEqual(app.messages.filter((message) => message.action === "view-changed"), []);
  }
});

test("restore-view adopts a valid viewport without echoing view-changed", () => {
  const app = loadApp();
  configureMap(app);
  deliverRestoreView(app, { lat: 15, lon: -20, zoom: 9 });
  assert.deepEqual(mapView(app), { lat: 15, lon: -20, zoom: 9 });

  deliverRestoreView(app, { lat: 89, lon: 180, zoom: null });
  const clamped = mapView(app);
  closeTo(clamped.lat, 85.0511);
  closeTo(clamped.lon, -180);
  assert.equal(clamped.zoom, 9);

  app.runTimers();
  assert.deepEqual(app.messages.filter((message) => message.action === "view-changed"), []);
});

test("restore-view rounds a fractional zoom to the nearest whole level", () => {
  const app = loadApp();
  configureMap(app);
  deliverRestoreView(app, { lat: 15, lon: -20, zoom: 12.4 });
  assert.deepEqual(mapView(app), { lat: 15, lon: -20, zoom: 12 });
  deliverRestoreView(app, { lat: 16, lon: -21, zoom: 12.6 });
  assert.deepEqual(mapView(app), { lat: 16, lon: -21, zoom: 13 });
});
