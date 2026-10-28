(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const state = {
    sensors: [],
    areaSensors: [],
    favoriteSensors: null,
    coverage: null,
    selectedId: null,
    previewHidden: [],
    config: { latitude: 45.5152, longitude: -122.6784, location_name: "Portland, Oregon", radius_km: 22, heatmap_threshold_km: 40, temperature_unit: "F", alert_threshold: 101, refresh_minutes: 2, has_api_key: false, api_key_hint: "", location_filter: "outdoor", interface_scale: 1, hidden: [] },
    source: "Starting Airloom",
    center: { lat: 45.5152, lon: -122.6784 },
    home: { lat: 45.5152, lon: -122.6784 },
    zoom: 12,
    drag: null,
    query: "",
    placeResults: [],
    homeSearchActive: false,
    positioned: false,
    // Name of the area currently in view (reverse-geocoded by Python);
    // overrides the configured home name on the summary chip until replaced.
    viewName: null,
    popupId: null,
    popupReturnFocus: null,
    detailReturnFocus: null,
  };

  const bridge = (message) => {
    if (window.webkit?.messageHandlers?.airloom) {
      window.webkit.messageHandlers.airloom.postMessage(JSON.stringify(message));
    }
  };

  window.Airloom = {
    receive(event, payload) {
      if (event === "config") applyConfig(payload);
      if (event === "sensors") applySensors(payload);
      if (event === "loading") document.body.classList.toggle("loading", payload.active);
      if (event === "error") toast(payload.message);
      if (event === "open-settings") openSettings(payload);
      if (event === "location") applyLocation(payload);
      if (event === "view-name" && payload.name) { state.viewName = payload.name; stampPlaceName(); }
      if (event === "pinch") nativePinch(payload);
      if (event === "places") {
        if (state.homeSearchActive) {
          if (payload.query === $("#home-place-input").value.trim()) renderHomePlaceResults(payload.results || [], payload.error);
        } else if (payload.query === state.query.trim()) renderSearchResults(payload.results || [], payload.error);
      }
    },
    // Small, legitimate debug accessors used by the debug port (see
    // airloom/debugport.py and app.py's _debug_* handlers) to read and
    // drive page state without reaching into module-private variables.
    debugState: () => ({
      zoom: state.zoom,
      center: state.center,
      sensorCount: state.sensors.length,
      visibleCount: visibleSensors().length,
      heatmapActive: heatmapActive(),
      viewportKm: viewportWidthKm(),
      selectedId: state.selectedId,
      popupId: state.popupId,
      popupHidden: $("#map-popup").hidden,
      location_filter: state.config.location_filter,
      hiddenCount: (state.config.hidden || []).length,
      source: state.source,
      viewportScale: window.visualViewport ? window.visualViewport.scale : null,
    }),
    debugSearch(query) {
      const input = $("#search");
      input.value = query;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return visibleSensors().length;
    },
    debugKey(key) {
      document.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
    },
    debugTap(x, y) {
      const el = document.elementFromPoint(x, y);
      if (!el) return null;
      const init = { bubbles: true, cancelable: true, composed: true, isPrimary: true, button: 0, clientX: x, clientY: y };
      el.dispatchEvent(new PointerEvent("pointerdown", init));
      el.dispatchEvent(new PointerEvent("pointerup", init));
      // Real trackpad/touch input on GNOME resolves to a synthesized click
      // after pointerup, and most of our listeners (marker taps, buttons)
      // are bound to "click" rather than the pointer events, so pointer
      // events alone wouldn't trigger them. Call .click() too, but only if
      // pointerup didn't already remove the element from the document
      // (e.g. closing a popup it belonged to) — clicking a detached node
      // is a no-op at best and an error in some engines at worst.
      if (el.isConnected) el.click();
      return { tag: el.tagName, id: el.id || null, className: el.className || null };
    },
    debugZoomTo(zoom) {
      beginDirectManipulation();
      applyZoom(Number(zoom), centerAnchor());
      renderMapMarkers();
      scheduleViewChanged();
      return state.zoom;
    },
  };

  function stampPlaceName() {
    $("#place-name").textContent = state.viewName || state.config.location_name;
  }

  function applyInterfaceScale() {
    const requested = Number(state.config.interface_scale);
    const scale = [1, 1.25, 1.5].includes(requested) ? requested : 1;
    document.documentElement.style.setProperty("--interface-scale", String(scale));
    document.documentElement.dataset.interfaceScale = String(scale);
  }

  function announce(message) {
    const region = $("#ui-status");
    if (region.textContent !== message) region.textContent = message;
  }

  const FILTER_LABELS = { outdoor: "Outdoor", indoor: "Indoor", both: "All sensors" };
  const FILTER_NEXT = { outdoor: "indoor", indoor: "both", both: "outdoor" };
  function stampFilterChip() {
    $("#filter-chip").textContent = FILTER_LABELS[state.config.location_filter] || "Outdoor";
  }

  function applyLocation(payload) {
    const lat = Number(payload.latitude), lon = Number(payload.longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    state.home = { lat, lon };
    state.positioned = true;
    // A location event re-homes the map, so the view label is the home label
    // again; a later view-name event relabels it if the user navigates away.
    state.viewName = null;
    if (payload.name) state.config.location_name = payload.name;
    stampPlaceName();
    if (payload.source === "geoclue" || payload.source === "fixed") flyTo(lat, lon);
  }

  function applyConfig(config) {
    state.config = { ...state.config, ...config };
    const lat = Number(state.config.latitude);
    const lon = Number(state.config.longitude);
    if (Number.isFinite(lat) && Number.isFinite(lon)) {
      state.home = { lat, lon };
      if (!state.positioned && Number.isFinite(state.home.lat) && Number.isFinite(state.home.lon)) {
        state.center = { ...state.home };
        state.positioned = true;
      }
    }
    stampPlaceName();
    stampFilterChip();
    applyInterfaceScale();
    renderMap();
  }

  function applySensors(payload) {
    const area = Array.isArray(payload.area) ? payload.area : (payload.items || []);
    const favorites = Array.isArray(payload.favorites) ? payload.favorites : [];
    const merged = new Map(area.map((sensor) => [sensor.id, sensor]));
    for (const favorite of favorites) {
      const local = merged.get(favorite.id);
      merged.set(favorite.id, local ? { ...local, favorite: local.favorite || favorite.favorite } : { ...favorite, remote_favorite: true });
    }
    state.sensors = [...merged.values()];
    state.areaSensors = Array.isArray(payload.area) ? area : state.sensors;
    state.favoriteSensors = Array.isArray(payload.favorites) ? favorites : null;
    state.coverage = payload.coverage || null;
    state.selectedId = payload.selected_id ?? state.sensors[0]?.id ?? null;
    state.source = payload.source || "Demo data";
    state.stale = payload.stale;
    state.config = { ...state.config, ...(payload.config || {}) };
    stampPlaceName();
    applyInterfaceScale();
    $("#data-source").textContent = state.source;
    renderAll();
    reconcilePopup();
    if ($("#settings-dialog").open) renderHiddenList();
  }

  function renderAll() {
    renderSummary();
    renderLists();
    renderMap();
    renderDetail();
  }

  function hasLimitedCoverage() {
    return state.coverage?.capped === true || state.coverage?.complete === false;
  }

  function summaryTitle(base) {
    return hasLimitedCoverage() ? `${base} · Limited coverage` : base;
  }

  function renderProvenance() {
    const source = (state.source || "").toLowerCase();
    const legacyStale = state.stale === undefined && state.source === "PurpleAir · cached";
    let label = "";
    if (source.includes("demo") || state.areaSensors.some((sensor) => sensor.origin === "demo")) label = "Demo data";
    else if (state.stale === true || legacyStale) label = "Stale cached data";
    else if (source.includes("cached") || state.areaSensors.some((sensor) => sensor.delivery === "cache")) label = "Cached data";
    for (const id of ["summary-provenance", "detail-provenance"]) {
      const element = $(`#${id}`);
      element.hidden = !label;
      element.textContent = label;
    }
  }

  function renderSummary(view = mapViewport()) {
    renderProvenance();
    const limited = hasLimitedCoverage();
    $("#coverage-status").hidden = !limited;
    $("#coverage-status").textContent = limited ? "Limited coverage" : "";
    const valid = view.width ? AirloomCore.summarySensors(locationFiltered(state.areaSensors), geographicBounds(view)) : [];
    const sensor = valid[Math.floor(valid.length / 2)];
    if (!sensor) {
      $("#summary-aqi").textContent = "—";
      $("#summary-aqi").style.background = "";
      $("#summary-aqi").style.color = "";
      $("#summary-label").textContent = "No readings";
      $("#summary-chip").title = summaryTitle("No sensors reporting in range");
      return;
    }
    const band = AirloomCore.bandForAQI(sensor.aqi);
    $("#summary-aqi").textContent = sensor.aqi;
    $("#summary-aqi").style.background = band.color;
    $("#summary-aqi").style.color = band.foreground;
    $("#summary-label").textContent = band.label;
    $("#summary-chip").title = summaryTitle(`${valid.length} sensors reporting`);
  }

  // Preview mode filters sensors locally by indoor/outdoor; app mode leaves
  // that filtering to the Python fetch, so this is a no-op there. Shared by
  // visibleSensors() (which layers the search query on top) and
  // renderSummary() (which must reflect the location filter but not search).
  function locationFiltered(sensors) {
    if (!window.webkit?.messageHandlers?.airloom && state.config.location_filter !== "both") {
      return sensors.filter((s) => Boolean(s.indoor) === (state.config.location_filter === "indoor"));
    }
    return sensors;
  }

  function visibleSensors(sensors = state.sensors) {
    const eligible = locationFiltered(sensors);
    const query = state.query.trim().toLowerCase();
    return query ? eligible.filter((s) => s.name.toLowerCase().includes(query) || String(s.aqi).includes(query)) : eligible;
  }

  // A sensor filtered out by the location filter or search query must not
  // leave a stale popup open for a marker that's no longer shown.
  function reconcilePopup() {
    if (state.popupId === null) return;
    const open = visibleSensors().find((s) => s.id === state.popupId);
    open ? showPopup(open) : hidePopup();
  }

  function restoreRemovedFocus(metadata) {
    if (!metadata) return;
    if ((metadata.surface === "sensor-list" || metadata.surface === "favorites-list") && !$("#sensors-panel").hidden) {
      const preferred = $(`#${metadata.surface}`).querySelector(".sensor-row");
      const fallback = preferred || $("#sensor-list").querySelector(".sensor-row") || $("#favorites-list").querySelector(".sensor-row");
      if (fallback) { fallback.focus(); return; }
    }
    $("#sensors-button").focus();
  }

  function restoreLogicalFocus(origin) {
    if (origin?.surface === "search") { $("#search").focus(); return; }
    if (origin && (origin.surface === "sensor-list" || origin.surface === "favorites-list" || origin.surface === "markers")) {
      const panelHidden = origin.surface !== "markers" && $("#sensors-panel").hidden;
      const container = $(`#${origin.surface}`);
      const target = [...container.children].find((node) => node.dataset.id === String(origin.key));
      if (!panelHidden && target) { target.focus(); return; }
    }
    $("#sensors-button").focus();
  }

  function renderKeyedCollection(container, items, emptyMarkup) {
    const result = AirloomState.reconcileKeyed(container, items, (sensor) => sensor.id, createSensorRow, updateSensorRow);
    if (!items.length) container.innerHTML = emptyMarkup;
    restoreRemovedFocus(result.removedFocus);
  }

  function renderLists() {
    const visible = visibleSensors();
    const separateFavorites = state.favoriteSensors !== null;
    const favorites = separateFavorites ? visibleSensors(state.favoriteSensors) : visible.filter((sensor) => sensor.favorite);
    const nearby = separateFavorites ? visibleSensors(state.areaSensors) : visible;
    $("#sensor-count").textContent = nearby.length;
    $("#favorite-count").textContent = favorites.length;
    renderKeyedCollection($("#favorites-list"), favorites, '<div class="empty-state small">Star a sensor to keep it close.</div>');
    renderKeyedCollection($("#sensor-list"), nearby, '<div class="empty-state">No sensors match that search.</div>');
  }

  function createSensorRow() {
    const row = document.createElement("button");
    row.type = "button";
    row.addEventListener("click", () => selectSensor(Number(row.dataset.id), true, { key: row.dataset.id, surface: row.parentNode.id }));
    return row;
  }

  function updateSensorRow(row, sensor) {
    row.className = `sensor-row${sensor.id === state.selectedId ? " selected" : ""}`;
    row.dataset.id = String(sensor.id);
    row.style.setProperty("--sensor", sensor.color || "");
    row.style.setProperty("--sensor-fg", sensor.foreground || "");
    row.setAttribute("aria-label", `${sensor.name}, AQI ${sensor.aqi ?? "unavailable"}${sensor.favorite ? ", favorite" : ""}`);
    const meta = [formatTemperature(sensor.temperature_f), formatHumidity(sensor.humidity)].filter(Boolean).join(" · ");
    row.innerHTML = `<span class="sensor-badge">${sensor.aqi ?? "—"}</span>
      <span class="sensor-copy"><strong>${escapeHtml(sensor.name)}</strong><small>${meta || "Recent outdoor reading"}</small></span>
      ${sensor.favorite ? '<span class="row-star"><svg viewBox="0 0 24 24"><path d="m12 17.3-6.2 3.5 1.4-6.9-5.1-4.7 7-.8L12 2l2.9 6.4 7 .8-5.1 4.7 1.4 6.9-6.2-3.5Z"/></svg></span>' : ""}`;
  }

  function selectSensor(id, revealDetail = false, origin = null) {
    state.selectedId = id;
    bridge({ action: "select", id });
    renderLists();
    renderMapMarkers();
    renderDetail();
    if (revealDetail) {
      hidePopup(false);
      state.detailReturnFocus = origin || { surface: "search" };
      $("#detail-card").hidden = false;
      $("#close-detail").focus();
    }
    const sensor = selectedSensor();
    if (sensor) announce(`${sensor.name}. AQI ${sensor.aqi ?? "unavailable"}.`);
  }

  function selectedSensor() {
    return state.sensors.find((sensor) => sensor.id === state.selectedId);
  }

  function positionPopup(sensor, view = mapViewport()) {
    if (!view.width || !view.height) return;
    const popup = $("#map-popup");
    const point = sensorWorldPoint(sensor);
    const margin = 12, gap = 30;
    const width = popup.clientWidth || 232;
    const height = popup.clientHeight || 140;
    const screenX = point.x - view.left;
    const screenY = point.y - view.top;
    const halfWidth = width / 2;
    const x = view.width <= width + margin * 2
      ? view.width / 2
      : Math.max(margin + halfWidth, Math.min(view.width - margin - halfWidth, screenX));
    const aboveSpace = screenY - gap - margin;
    const belowSpace = view.height - screenY - gap - margin;
    const below = aboveSpace < height && belowSpace > aboveSpace;
    popup.classList.toggle("below", below);
    popup.dataset.placement = below ? "below" : "above";
    popup.style.left = `${view.left + x}px`;
    popup.style.top = `${point.y}px`;
  }

  function showPopup(sensor, { origin = null, focus = false } = {}) {
    const popup = $("#map-popup");
    if (origin) state.popupReturnFocus = origin;
    else if (state.popupId !== sensor.id) state.popupReturnFocus = { key: String(sensor.id), surface: "markers" };
    $("#popup-aqi").textContent = sensor.aqi ?? "—";
    $("#popup-aqi").style.background = sensor.color || "";
    $("#popup-aqi").style.color = sensor.foreground || "";
    $("#popup-name").textContent = sensor.name;
    $("#popup-meta").textContent = [sensor.category, sensor.indoor ? "Indoor" : "Outdoor", relativeTime(sensor.last_seen)].filter(Boolean).join(" · ");
    $("#popup-favorite").classList.toggle("active", Boolean(sensor.favorite));
    $("#popup-favorite").setAttribute("aria-pressed", String(Boolean(sensor.favorite)));
    const demo = (state.source || "").toLowerCase().includes("demo") || sensor.origin === "demo";
    const link = $("#popup-purpleair");
    link.hidden = demo; // demo sensor ids do not exist on the public map
    if (!demo) link.href = `https://map.purpleair.com/1/mAQI/a10/p604800/cC0?select=${sensor.id}#14/${sensor.latitude}/${sensor.longitude}`;
    popup.hidden = false;
    state.popupId = sensor.id;
    positionPopup(sensor);
    if (focus) $("#popup-details").focus();
  }

  function hidePopup(restoreFocus) {
    const popup = $("#map-popup");
    const shouldRestore = restoreFocus ?? (state.popupId !== null && popup.contains(document.activeElement));
    const origin = state.popupReturnFocus;
    popup.hidden = true;
    state.popupId = null;
    state.popupReturnFocus = null;
    if (shouldRestore && origin) restoreLogicalFocus(origin);
  }

  function hideDetail(restoreFocus = true) {
    const origin = state.detailReturnFocus;
    $("#detail-card").hidden = true;
    state.detailReturnFocus = null;
    if (restoreFocus && origin) restoreLogicalFocus(origin);
  }

  function renderDetail() {
    const sensor = selectedSensor();
    if (!sensor) {
      hideDetail();
      return;
    }
    $("#sensor-name").textContent = sensor.name;
    $("#detail-indoor").hidden = !sensor.indoor;
    $("#aqi-number").textContent = sensor.aqi ?? "—";
    $("#aqi-number").style.color = sensor.color;
    $("#aqi-category").textContent = sensor.category;
    $("#temperature").textContent = formatTemperature(sensor.temperature_f) || "—";
    $("#humidity").textContent = formatHumidity(sensor.humidity) || "—";
    $("#pm25").textContent = numberOrDash(sensor.pm25);
    $("#pm10").textContent = numberOrDash(sensor.pm10);
    $("#guidance").textContent = sensor.guidance;
    $("#guidance-card").style.setProperty("--guidance-color", sensor.color);
    $("#favorite-button").classList.toggle("active", sensor.favorite);
    $("#favorite-button").setAttribute("aria-pressed", String(Boolean(sensor.favorite)));
    $("#updated-time").textContent = relativeTime(sensor.last_seen);
    renderChart(sensor);
  }

  function renderChart(sensor) {
    const points = (sensor.trend || []).filter((point) => Number.isFinite(point.aqi));
    if (!points.length) {
      const message = state.config.has_api_key ? "Loading trend…" : "No trend available";
      $("#chart").innerHTML = `<div class="empty-state">${message}</div>`;
      $("#trend-direction").textContent = "—";
      return;
    }
    const width = 300, height = 104, padX = 8, padTop = 9, padBottom = 20;
    const max = Math.max(60, ...points.map((point) => point.aqi)) * 1.12;
    const coordinates = points.map((point, index) => ({
      ...point,
      x: padX + index * ((width - padX * 2) / Math.max(1, points.length - 1)),
      y: padTop + (max - point.aqi) / max * (height - padTop - padBottom),
    }));
    const line = coordinates.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
    const area = `${line} L${coordinates.at(-1).x},${height - padBottom} L${coordinates[0].x},${height - padBottom} Z`;
    const labels = coordinates.map((point) => `<text x="${point.x}" y="${height - 4}" text-anchor="middle">${point.label}</text>`).join("");
    const dots = coordinates.map((point) => `<circle class="dot" cx="${point.x}" cy="${point.y}" r="3.5" fill="${sensor.color}"/>`).join("");
    $("#chart").innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <line class="grid" x1="0" y1="${height - padBottom}" x2="${width}" y2="${height - padBottom}"/>
      <path class="area" d="${area}" fill="${sensor.color}"/><path class="line" d="${line}" stroke="${sensor.color}"/>${dots}${labels}
    </svg>`;
    const delta = points.at(-1).aqi - points[0].aqi;
    $("#trend-direction").textContent = Math.abs(delta) < 4 ? "Holding steady" : delta > 0 ? `Up ${delta} points` : `Down ${Math.abs(delta)} points`;
  }

  // Minimal slippy-map renderer: bundled code, standard OSM raster tiles, no JS CDN.
  const MAX_MAP_LAT = 85.0511; // Web Mercator limit; beyond it the projection diverges
  const clampLat = (lat) => Math.max(-MAX_MAP_LAT, Math.min(MAX_MAP_LAT, lat));
  const wrapLon = (lon) => ((lon + 180) % 360 + 360) % 360 - 180;

  function worldPoint(lat, lon, zoom) {
    const size = 256 * 2 ** zoom;
    const sin = Math.sin(clampLat(lat) * Math.PI / 180);
    return { x: (lon + 180) / 360 * size, y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * size };
  }

  function sensorWorldPoint(sensor) {
    const longitude = AirloomCore.nearestLongitude(sensor.longitude, state.center.lon);
    return worldPoint(sensor.latitude, longitude, state.zoom);
  }

  function inverseWorld(x, y, zoom) {
    const size = 256 * 2 ** zoom;
    const lon = x / size * 360 - 180;
    const n = Math.PI - 2 * Math.PI * y / size;
    const lat = clampLat(180 / Math.PI * Math.atan(Math.sinh(n)));
    return { lat, lon };
  }

  function geographicBounds(view) {
    const nw = inverseWorld(view.left, view.top, state.zoom);
    const se = inverseWorld(view.left + view.width, view.top + view.height, state.zoom);
    if (se.lon - nw.lon >= 360) return { north: nw.lat, west: -180, south: se.lat, east: 180 };
    return { north: nw.lat, west: wrapLon(nw.lon), south: se.lat, east: wrapLon(se.lon) };
  }

  // --- Heat map mode (spec: docs/superpowers/specs/2026-08-07-heatmap-mode-design.md) ---
  const EARTH_CIRCUMFERENCE_KM = 40075.016686;
  const HEATMAP_CELL = 8;        // CSS px per field-grid cell
  const HEATMAP_RADIUS = 90;     // screen-px influence radius per sensor
  const HEATMAP_MAX_ALPHA = 0.55;
  let heatmapWasActive = false;

  function viewportWidthKm() {
    const panel = $("#map-panel");
    if (!panel.clientWidth) return 0;
    return panel.clientWidth / (256 * 2 ** state.zoom) * EARTH_CIRCUMFERENCE_KM *
      Math.cos(clampLat(state.center.lat) * Math.PI / 180);
  }

  function heatmapActive() {
    return viewportWidthKm() > Number(state.config.heatmap_threshold_km || 40);
  }

  // Interpolated AQI values take the color of their shared core band, so
  // every on-screen color has a matching legend entry.
  function aqiBucketColor(aqi) {
    const color = AirloomCore.bandForAQI(aqi).color;
    return [1, 3, 5].map((offset) => Number.parseInt(color.slice(offset, offset + 2), 16));
  }

  // Re-render markers exactly once per threshold crossing; a marker popup
  // cannot survive into heat-map mode.
  function syncHeatmapMode() {
    const active = heatmapActive();
    if (active === heatmapWasActive) return active;
    heatmapWasActive = active;
    if (active) hidePopup();
    renderMapMarkers();
    return active;
  }

  function clearHeatmap() {
    const canvas = $("#heatmap");
    if (canvas.hidden) return;
    canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
    canvas.hidden = true;
  }

  function renderHeatmap(view) {
    const canvas = $("#heatmap");
    const cols = Math.max(1, Math.ceil(view.width / HEATMAP_CELL));
    const rows = Math.max(1, Math.ceil(view.height / HEATMAP_CELL));
    if (canvas.width !== cols || canvas.height !== rows) { canvas.width = cols; canvas.height = rows; }
    const ctx = canvas.getContext("2d");
    const image = ctx.createImageData(cols, rows);
    const pad = HEATMAP_RADIUS + HEATMAP_CELL;
    const points = [];
    for (const sensor of visibleSensors()) {
      if (!Number.isFinite(sensor.aqi)) continue;
      const point = sensorWorldPoint(sensor);
      const x = point.x - view.left, y = point.y - view.top;
      if (x < -pad || x > view.width + pad || y < -pad || y > view.height + pad) continue;
      points.push({ x, y, aqi: sensor.aqi });
    }
    const radius2 = HEATMAP_RADIUS * HEATMAP_RADIUS;
    // Scatter instead of gather: each sensor only touches the cell bounding
    // box within HEATMAP_RADIUS (~23x23 cells), not every cell on screen.
    // Points are still visited in array order and, within a cell, each
    // point's contribution is added in that same order — identical
    // summation order to the old gather loop, so weightSum/aqiSum/nearest2
    // land on the exact same floating-point values per cell.
    const cellCount = cols * rows;
    const weightSum = points.length ? new Float64Array(cellCount) : null;
    const aqiSum = points.length ? new Float64Array(cellCount) : null;
    const nearest2 = points.length ? new Float64Array(cellCount).fill(radius2) : null;
    for (const point of points) {
      const gxMin = Math.max(0, Math.floor((point.x - HEATMAP_RADIUS) / HEATMAP_CELL) - 1);
      const gxMax = Math.min(cols - 1, Math.ceil((point.x + HEATMAP_RADIUS) / HEATMAP_CELL) + 1);
      const gyMin = Math.max(0, Math.floor((point.y - HEATMAP_RADIUS) / HEATMAP_CELL) - 1);
      const gyMax = Math.min(rows - 1, Math.ceil((point.y + HEATMAP_RADIUS) / HEATMAP_CELL) + 1);
      for (let gy = gyMin; gy <= gyMax; gy++) {
        const cy = (gy + 0.5) * HEATMAP_CELL;
        const rowOffset = gy * cols;
        for (let gx = gxMin; gx <= gxMax; gx++) {
          const cx = (gx + 0.5) * HEATMAP_CELL;
          const dx = cx - point.x, dy = cy - point.y;
          const d2 = dx * dx + dy * dy;
          if (d2 > radius2) continue;
          const idx = rowOffset + gx;
          const w = 1 / (d2 + 4); // +4 keeps the weight finite directly over a sensor
          weightSum[idx] += w;
          aqiSum[idx] += point.aqi * w;
          if (d2 < nearest2[idx]) nearest2[idx] = d2;
        }
      }
    }
    for (let gy = 0; points.length && gy < rows; gy++) {
      const rowOffset = gy * cols;
      for (let gx = 0; gx < cols; gx++) {
        const idx = rowOffset + gx;
        const ws = weightSum[idx];
        if (!ws) continue;
        const [r, g, b] = aqiBucketColor(aqiSum[idx] / ws);
        // Feather toward the influence edge so blobs fade out instead of
        // ending in a hard circle.
        const edge = 1 - Math.sqrt(nearest2[idx]) / HEATMAP_RADIUS;
        const alpha = HEATMAP_MAX_ALPHA * Math.min(1, edge * 1.6);
        const offset = idx * 4;
        image.data[offset] = r;
        image.data[offset + 1] = g;
        image.data[offset + 2] = b;
        image.data[offset + 3] = Math.round(alpha * 255);
      }
    }
    ctx.putImageData(image, 0, 0);
    canvas.hidden = false;
  }

  let tileLayer = { zoom: null, tiles: new Map(), pending: 0 };
  let retireTimer = null;
  const MARKER_CULL_PAD = 300; // generous cull margin so mid-pan gaps are rare

  function tileZoomLevel() { return Math.max(3, Math.min(17, Math.round(state.zoom))); }

  function mapViewport() {
    const panel = $("#map-panel");
    const center = worldPoint(state.center.lat, state.center.lon, state.zoom);
    return {
      panel,
      left: center.x - panel.clientWidth / 2,
      top: center.y - panel.clientHeight / 2,
      width: panel.clientWidth,
      height: panel.clientHeight,
    };
  }

  function retireTiles() {
    const live = $("#tiles");
    const old = $("#tiles-old");
    if (!live.querySelector(".tile.loaded")) {
      // The outgoing layer never finished loading (e.g. a fast double zoom
      // crossed tile levels before any tile arrived) — discard its blank
      // tiles but keep whatever imagery #tiles-old is already showing rather
      // than replacing it with nothing.
      while (live.firstChild) { live.firstChild.onload = live.firstChild.onerror = null; live.firstChild.remove(); }
      clearTimeout(retireTimer);
      retireTimer = setTimeout(clearRetiredTiles, 1500);
      return;
    }
    old.textContent = "";
    while (live.firstChild) {
      const img = live.firstChild;
      img.onload = img.onerror = null;
      old.appendChild(img);
    }
    old.dataset.zoom = tileLayer.zoom;
    old.hidden = !old.firstChild;
    clearTimeout(retireTimer);
    retireTimer = setTimeout(clearRetiredTiles, 1500);
  }

  function clearRetiredTiles() {
    clearTimeout(retireTimer);
    const old = $("#tiles-old");
    old.textContent = "";
    old.hidden = true;
  }

  function clearRetiredTilesWhenCovered(owner) {
    if (owner !== tileLayer || owner.pending > 0) return;
    // Settlement is not coverage: only a completely successful current layer
    // can replace the last usable imagery.
    const tiles = [...owner.tiles.values()];
    if (tiles.length && tiles.every((img) => img.classList.contains("loaded"))) clearRetiredTiles();
  }

  function renderFrame() {
    const view = mapViewport();
    if (!view.width || !view.height) return;
    renderSummary(view);
    const tz = tileZoomLevel();
    if (tileLayer.zoom !== tz) {
      if (tileLayer.zoom !== null) retireTiles();
      tileLayer = { zoom: tz, tiles: new Map(), pending: 0 };
    }
    const scale = 2 ** (state.zoom - tz);
    const maxTile = 2 ** tz;
    // Visible range in tile-zoom space (view coords are fractional-zoom space).
    const left = view.left / scale, top = view.top / scale;
    const right = (view.left + view.width) / scale, bottom = (view.top + view.height) / scale;
    const layer = $("#tiles");
    const needed = new Set();
    for (let ty = Math.floor(top / 256); ty <= Math.floor(bottom / 256); ty++) {
      if (ty < 0 || ty >= maxTile) continue;
      for (let tx = Math.floor(left / 256); tx <= Math.floor(right / 256); tx++) {
        const key = `${tx}/${ty}`;
        needed.add(key);
        if (!tileLayer.tiles.has(key)) {
          const wrappedX = ((tx % maxTile) + maxTile) % maxTile;
          const img = document.createElement("img");
          img.className = "tile";
          img.draggable = false;
          img.alt = "";
          // Capture the layer this tile belongs to: tileLayer gets reassigned
          // wholesale on a zoom-level change, and a stale in-flight load must
          // decrement its own (possibly retired) layer's counter, not
          // whatever layer happens to be current when it fires.
          const owner = tileLayer;
          owner.pending++;
          img.dataset.pending = "true";
          const settle = (loaded) => {
            if (loaded) img.classList.add("loaded");
            delete img.dataset.pending;
            img.onload = img.onerror = null;
            owner.pending--;
            clearRetiredTilesWhenCovered(owner);
          };
          img.onload = () => settle(true);
          img.onerror = () => settle(false);
          img.src = `https://tile.openstreetmap.org/${tz}/${wrappedX}/${ty}.png`;
          img.style.left = `${tx * 256}px`;
          img.style.top = `${ty * 256}px`;
          layer.appendChild(img);
          tileLayer.tiles.set(key, img);
        }
      }
    }
    for (const [key, img] of tileLayer.tiles) {
      if (!needed.has(key)) {
        if (img.dataset.pending === "true") {
          tileLayer.pending--;
          delete img.dataset.pending;
        }
        img.onload = img.onerror = null;
        img.remove();
        tileLayer.tiles.delete(key);
      }
    }
    clearRetiredTilesWhenCovered(tileLayer);
    layer.style.transform = `translate(${-view.left}px, ${-view.top}px) scale(${scale})`;
    const old = $("#tiles-old");
    if (!old.hidden) {
      const oldScale = 2 ** (state.zoom - Number(old.dataset.zoom));
      old.style.transform = `translate(${-view.left}px, ${-view.top}px) scale(${oldScale})`;
    }
    layoutMarkers(view);
    if (syncHeatmapMode()) renderHeatmap(view);
    else clearHeatmap();
  }

  function layoutMarkers(view = mapViewport()) {
    const markers = $("#markers");
    markers.style.transform = `translate(${-view.left}px, ${-view.top}px)`;
    $("#popup-layer").style.transform = markers.style.transform;
    const byId = new Map(state.sensors.map((sensor) => [sensor.id, sensor]));
    markers.querySelectorAll(".map-marker").forEach((element) => {
      const sensor = byId.get(Number(element.dataset.id));
      if (!sensor) return;
      const point = sensorWorldPoint(sensor);
      element.style.left = `${point.x}px`;
      element.style.top = `${point.y}px`;
    });
    // Zoom gestures close the popup up front (hideTransientOverlays), so this
    // reposition only matters for flyTo/pan frames while a popup stays open.
    if (state.popupId !== null) {
      const open = byId.get(state.popupId);
      if (open) positionPopup(open, view);
    }
  }

  function renderMap() {
    renderFrame();
    renderMapMarkers();
  }

  // Transient overlays hook: called at the start of every zoom gesture/drag.
  function hideTransientOverlays() { hidePopup(); }

  function applyZoom(z, anchor) {
    const view = mapViewport();
    if (!view.width) return;
    z = Math.max(3, Math.min(17, z));
    const geo = inverseWorld(view.left + anchor.x, view.top + anchor.y, state.zoom);
    const point = worldPoint(geo.lat, geo.lon, z);
    state.zoom = z;
    state.center = inverseWorld(
      point.x - anchor.x + view.width / 2,
      point.y - anchor.y + view.height / 2,
      z,
    );
    renderFrame();
  }

  function centerAnchor() {
    const view = mapViewport();
    return { x: view.width / 2, y: view.height / 2 };
  }

  const navigation = new AirloomState.NavigationGate();
  const prefersReducedMotion = () => Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches);
  let zoomAnim = null;

  function cancelZoomAnimation() {
    navigation.cancel("zoom");
    zoomAnim = null;
  }

  function beginDirectManipulation() {
    clearTimeout(pinchSettleTimer);
    navigation.begin("direct");
    zoomAnim = null;
  }

  function animateZoomTo(target, anchor) {
    hideTransientOverlays();
    // A pending pinch/ctrl+wheel settle must never fire mid-flight and
    // hijack this animation's target/anchor (covers the wheel, button, and
    // gestureend entry points in one place).
    clearTimeout(pinchSettleTimer);
    target = Math.max(3, Math.min(17, Math.round(target)));
    if (zoomAnim && navigation.isCurrent("zoom", zoomAnim.token)) {
      zoomAnim.target = target;
      zoomAnim.anchor = anchor;
      return;
    }
    const token = navigation.begin("zoom");
    zoomAnim = { target, anchor, token };
    if (prefersReducedMotion()) {
      applyZoom(target, anchor);
      zoomAnim = null;
      renderMapMarkers();
      scheduleViewChanged();
      return;
    }
    const step = () => {
      if (!zoomAnim || !navigation.isCurrent("zoom", token)) return;
      const diff = zoomAnim.target - state.zoom;
      if (Math.abs(diff) < 0.02) {
        applyZoom(zoomAnim.target, zoomAnim.anchor);
        zoomAnim = null;
        renderMapMarkers();
        scheduleViewChanged();
        return;
      }
      // Exponential ease-out: retargeting mid-flight stays smooth.
      applyZoom(state.zoom + diff * 0.22, zoomAnim.anchor);
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function createMapMarker() {
    const marker = document.createElement("button");
    marker.type = "button";
    marker.addEventListener("click", (event) => {
      event.stopPropagation();
      const id = Number(marker.dataset.id);
      selectSensor(id, false);
      const sensor = state.sensors.find((item) => item.id === id);
      if (sensor) showPopup(sensor, { origin: { key: marker.dataset.id, surface: "markers" }, focus: true });
    });
    return marker;
  }

  function updateMapMarker(marker, sensor) {
    const point = sensorWorldPoint(sensor);
    marker.className = `map-marker${sensor.indoor ? " indoor" : ""}${sensor.id === state.selectedId ? " selected" : ""}`;
    marker.dataset.id = String(sensor.id);
    marker.title = `${sensor.name} · AQI ${sensor.aqi ?? "unavailable"}`;
    marker.setAttribute("aria-label", marker.title);
    marker.style.left = `${point.x}px`;
    marker.style.top = `${point.y}px`;
    marker.style.setProperty("--sensor", sensor.color || "");
    marker.style.setProperty("--sensor-fg", sensor.foreground || "");
    marker.textContent = sensor.aqi ?? "—";
  }

  function renderMapMarkers() {
    const view = mapViewport();
    if (!view.width) return;
    const container = $("#markers");
    if (heatmapActive()) {
      const result = AirloomState.reconcileKeyed(container, [], (sensor) => sensor.id, createMapMarker, updateMapMarker);
      restoreRemovedFocus(result.removedFocus);
      markerDriftX = markerDriftY = 0;
      renderHeatmap(view);
      return;
    }
    markerDriftX = markerDriftY = 0;
    const pad = MARKER_CULL_PAD;
    const markers = visibleSensors().filter((sensor) => {
      const point = sensorWorldPoint(sensor);
      return point.x >= view.left - pad && point.x <= view.left + view.width + pad &&
        point.y >= view.top - pad && point.y <= view.top + view.height + pad;
    });
    const result = AirloomState.reconcileKeyed(container, markers, (sensor) => sensor.id, createMapMarker, updateMapMarker);
    restoreRemovedFocus(result.removedFocus);
    layoutMarkers(view);
  }

  let viewTimer = null;
  function scheduleViewChanged() {
    clearTimeout(viewTimer);
    viewTimer = setTimeout(sendViewChanged, 1200);
  }
  function sendViewChanged() {
    const view = mapViewport();
    if (!view.width) return;
    bridge({
      action: "view-changed",
      ...geographicBounds(view),
      lat: state.center.lat,
      lon: wrapLon(state.center.lon),
      zoom: state.zoom,
    });
  }

  function flyTo(lat, lon, durationMs = 600) {
    const from = { ...state.center };
    const targetLon = AirloomCore.nearestLongitude(lon, from.lon);
    clearTimeout(pinchSettleTimer);
    cancelZoomAnimation();
    const token = navigation.begin("fly");
    if (prefersReducedMotion() || durationMs <= 0) {
      state.center = { lat, lon: targetLon };
      renderMap();
      scheduleViewChanged();
      return;
    }
    const start = performance.now();
    function step(now) {
      if (!navigation.isCurrent("fly", token)) return;
      const t = Math.min(1, (now - start) / durationMs);
      const ease = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
      state.center = { lat: from.lat + (lat - from.lat) * ease, lon: from.lon + (targetLon - from.lon) * ease };
      renderFrame();
      if (t < 1) requestAnimationFrame(step);
      else { renderMapMarkers(); scheduleViewChanged(); }
    }
    requestAnimationFrame(step);
  }

  let markerDriftX = 0, markerDriftY = 0;
  function panBy(dx, dy) {
    const center = worldPoint(state.center.lat, state.center.lon, state.zoom);
    state.center = inverseWorld(center.x - dx, center.y - dy, state.zoom);
    renderFrame();
    // renderFrame() only repositions already-rendered markers; a long drag
    // can carry the view past the cull pad before a full re-render happens,
    // so force one once accumulated drift since the last full render exceeds it.
    markerDriftX += dx; markerDriftY += dy;
    if (Math.hypot(markerDriftX, markerDriftY) > MARKER_CULL_PAD) renderMapMarkers();
    scheduleViewChanged();
  }

  function openSettings(config = state.config) {
    if ($("#settings-dialog").open) return;
    const form = $("#settings-form");
    for (const field of ["radius_km", "alert_threshold", "heatmap_threshold_km", "refresh_minutes"]) form.elements[field].value = config[field];
    form.elements.api_key.value = "";
    form.elements.clear_api_key.checked = false;
    // "!== false" so the box is checked in browser preview, where config has
    // no confidence_filter key — matching the Python default of on.
    form.elements.confidence_filter.checked = config.confidence_filter !== false;
    form.elements.temperature_unit.value = config.temperature_unit || "F";
    form.elements.home_mode.value = config.home_mode || "auto";
    $("#home-place-row").hidden = form.elements.home_mode.value !== "fixed";
    form.elements.home_lat.value = config.latitude;
    form.elements.home_lon.value = config.longitude;
    form.elements.location_name.value = config.location_name || "";
    $("#home-place-status").textContent = config.home_mode === "fixed" ? `Fixed: ${config.location_name}` : "No fixed home chosen";
    $("#key-status").textContent = config.has_api_key ? `Saved key ${config.api_key_hint}` : "No key saved — demo mode";
    renderHiddenList();
    $("#settings-dialog").showModal();
  }

  function renderHiddenList() {
    const items = state.config.hidden || [];
    $("#hidden-list").innerHTML = items.length
      ? items.map((item) => `<div class="hidden-row"><span>${escapeHtml(item.name || `Sensor ${item.id}`)}</span><button type="button" class="unhide-button" data-id="${item.id}">Unhide</button></div>`).join("")
      : '<div class="empty-state small">No hidden sensors.</div>';
    const all = $("#unhide-all");
    all.hidden = !items.length;
    all.textContent = `Unhide all (${items.length})`;
    document.querySelectorAll("#hidden-list .unhide-button").forEach((button) => button.addEventListener("click", () => requestUnhide(Number(button.dataset.id))));
  }

  // Bridge mode: Python owns hiding and answers with a fresh sensors payload.
  // Preview mode: emulate it locally so the UI stays explorable in a browser.
  function requestHide(id) {
    if (window.webkit?.messageHandlers?.airloom) { bridge({ action: "hide", id }); return; }
    const index = state.sensors.findIndex((s) => s.id === id);
    if (index < 0) return;
    const [sensor] = state.sensors.splice(index, 1);
    state.previewHidden.push(sensor);
    previewSyncHidden();
  }

  function requestUnhide(id) {
    if (window.webkit?.messageHandlers?.airloom) { bridge({ action: "unhide", id }); return; }
    const index = state.previewHidden.findIndex((s) => s.id === id);
    if (index < 0) return;
    state.sensors.push(...state.previewHidden.splice(index, 1));
    previewSyncHidden();
  }

  function previewSyncHidden() {
    state.config.hidden = state.previewHidden
      .map((s) => ({ id: s.id, name: s.name }))
      .sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()) || a.id - b.id);
    if (!state.sensors.some((s) => s.id === state.selectedId)) state.selectedId = state.sensors[0]?.id ?? null;
    renderAll();
    reconcilePopup();
    if ($("#settings-dialog").open) renderHiddenList();
  }

  function toast(message) {
    const item = document.createElement("div");
    item.className = "toast";
    item.textContent = message;
    $("#toast-stack").appendChild(item);
    setTimeout(() => item.remove(), 6500);
  }

  function formatTemperature(fahrenheit) {
    if (!Number.isFinite(fahrenheit)) return "";
    if (state.config.temperature_unit === "C") return `${Math.round((fahrenheit - 32) * 5 / 9)}°C`;
    return `${Math.round(fahrenheit)}°F`;
  }
  const formatHumidity = (humidity) => Number.isFinite(humidity) ? `${Math.round(humidity)}%` : "";
  const numberOrDash = (number) => Number.isFinite(number) ? Number(number).toFixed(1) : "—";
  function relativeTime(timestamp) {
    if (!timestamp) return "Update time unavailable";
    const seconds = Math.max(0, Math.round(Date.now() / 1000 - timestamp));
    if (seconds < 90) return "Updated just now";
    if (seconds < 3600) return `Updated ${Math.round(seconds / 60)} min ago`;
    return `Updated ${Math.round(seconds / 3600)} hr ago`;
  }
  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
  }

  function renderSearchResults(places, error) {
    const box = $("#search-results");
    const query = state.query.trim().toLowerCase();
    const sensors = query ? state.sensors.filter((s) => s.name.toLowerCase().includes(query)).slice(0, 4) : [];
    if (!sensors.length && !places.length && !error) { box.hidden = true; box.innerHTML = ""; return; }
    let html = "";
    if (sensors.length) html += '<div class="group">Sensors</div>' + sensors.map((s) => `<button data-kind="sensor" data-id="${s.id}">${escapeHtml(s.name)} · AQI ${s.aqi ?? "—"}</button>`).join("");
    if (places.length) html += '<div class="group">Places</div>' + places.map((p, i) => `<button data-kind="place" data-index="${i}">${escapeHtml(p.name)}</button>`).join("");
    if (error) html += `<div class="group">${escapeHtml(error)}</div>`;
    box.innerHTML = html;
    box.hidden = false;
    state.placeResults = places;
    box.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => {
      if (button.dataset.kind === "sensor") { selectSensor(Number(button.dataset.id), true, { surface: "search" }); }
      else {
        const place = state.placeResults[Number(button.dataset.index)];
        if (place) {
          // The query was a navigation target, not a sensor filter; leaving it
          // set would hide every sensor fetched at the destination.
          state.query = "";
          $("#search").value = "";
          renderLists();
          renderMapMarkers();
          flyTo(place.latitude, place.longitude);
        }
      }
      box.hidden = true;
    }));
  }

  function renderHomePlaceResults(places, error) {
    const box = $("#home-place-results");
    if (!places.length && !error) { box.hidden = true; box.innerHTML = ""; return; }
    box.innerHTML = error ? `<div class="group">${escapeHtml(error)}</div>` : places.map((p, i) => `<button type="button" data-index="${i}">${escapeHtml(p.name)}</button>`).join("");
    box.hidden = false;
    box.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => {
      const place = places[Number(button.dataset.index)];
      const form = $("#settings-form");
      form.elements.home_lat.value = place.latitude;
      form.elements.home_lon.value = place.longitude;
      form.elements.location_name.value = place.name.split(",")[0];
      $("#home-place-status").textContent = `Fixed: ${place.name}`;
      box.hidden = true;
      state.homeSearchActive = false;
    }));
  }

  function browserPreviewData() {
    const readings = [18, 34, 47, 56, 72, 88, 109, 43, 63, 31, 81, 52];
    const names = ["Alberta Arts", "Laurelhurst Park", "Mount Tabor", "Sellwood Garden", "Buckman School", "Overlook Bluff", "St. Johns North", "Hawthorne Ridge", "Woodstock Library", "Council Crest", "Irvington Air", "Rose City Park"];
    return readings.map((aqi, index) => {
      const band = AirloomCore.bandForAQI(aqi);
      const angle = index * 2.399963;
      return { id: 8000 + index, name: names[index], latitude: 45.5152 + Math.sin(angle) * (.018 + index % 3 * .012), longitude: -122.6784 + Math.cos(angle) * (.024 + index % 3 * .016), aqi, category: band.label, color: band.color, foreground: band.foreground, pm25: Math.round((aqi / 3.1) * 10) / 10, pm10: Math.round((aqi / 2.3) * 10) / 10, temperature_f: 64 + index % 8, humidity: 43 + index % 6 * 5, last_seen: Math.round(Date.now() / 1000) - index * 24, favorite: index < 2, indoor: index % 5 === 2, guidance: band.guidance, trend: ["1w", "1d", "6h", "1h", "30m", "10m", "Now"].map((label, point) => ({ label, aqi: Math.max(4, aqi + Math.round(Math.sin(index + point) * 11)) })) };
    });
  }

  let placeTimer = null;
  $("#search").addEventListener("input", (event) => {
    state.query = event.target.value;
    renderLists();
    renderMapMarkers();
    reconcilePopup();
    renderSearchResults([]);
    clearTimeout(placeTimer);
    if (state.query.trim().length >= 3) placeTimer = setTimeout(() => bridge({ action: "place-search", query: state.query.trim() }), 450);
  });
  $("#search").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && state.query.trim().length >= 2) bridge({ action: "place-search", query: state.query.trim() });
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); $("#search").focus(); }
    if ((event.ctrlKey || event.metaKey) && event.key === ",") { event.preventDefault(); openSettings(); }
    if (event.key === "Escape" && !$("#settings-dialog").open) {
      if (!$("#map-popup").hidden) hidePopup(true);
      else if (!$("#search-results").hidden) { $("#search-results").hidden = true; $("#search").focus(); }
      else if (!$("#detail-card").hidden) hideDetail();
      else if (!$("#sensors-panel").hidden) {
        $("#sensors-panel").hidden = true;
        $("#sensors-button").setAttribute("aria-expanded", "false");
        $("#sensors-button").focus();
      }
    }
  });
  $("#footer-refresh").addEventListener("click", () => bridge({ action: "refresh" }));
  $("#favorite-button").addEventListener("click", () => { if (state.selectedId !== null) bridge({ action: "favorite", id: state.selectedId }); });
  $("#hide-button").addEventListener("click", () => {
    if (state.selectedId === null) return;
    const id = state.selectedId;
    hideDetail(false); // deterministic close; the resend reselects another sensor
    $("#sensors-button").focus();
    requestHide(id);
  });
  $("#popup-details").addEventListener("click", () => {
    const origin = state.popupReturnFocus || (state.popupId !== null ? { key: String(state.popupId), surface: "markers" } : null);
    hidePopup(false);
    state.detailReturnFocus = origin;
    $("#detail-card").hidden = false;
    renderDetail();
    $("#close-detail").focus();
  });
  $("#popup-favorite").addEventListener("click", () => { if (state.popupId !== null) { if (window.webkit?.messageHandlers?.airloom) bridge({ action: "favorite", id: state.popupId }); else { const sensor = state.sensors.find((s) => s.id === state.popupId); if (sensor) { sensor.favorite = !sensor.favorite; renderAll(); showPopup(sensor); } } } });
  $("#popup-hide").addEventListener("click", () => {
    if (state.popupId === null) return;
    const id = state.popupId;
    hidePopup(false);
    $("#sensors-button").focus();
    requestHide(id);
  });
  $("#unhide-all").addEventListener("click", () => {
    if (window.webkit?.messageHandlers?.airloom) { bridge({ action: "unhide-all" }); return; }
    state.sensors.push(...state.previewHidden.splice(0));
    previewSyncHidden();
  });
  $("#zoom-in").addEventListener("click", (event) => { event.stopPropagation(); animateZoomTo((zoomAnim ? zoomAnim.target : Math.round(state.zoom)) + 1, centerAnchor()); });
  $("#zoom-out").addEventListener("click", (event) => { event.stopPropagation(); animateZoomTo((zoomAnim ? zoomAnim.target : Math.round(state.zoom)) - 1, centerAnchor()); });
  $("#recenter").addEventListener("click", (event) => { event.stopPropagation(); flyTo(state.home.lat, state.home.lon); });
  $("#sensors-button").addEventListener("click", () => {
    const open = $("#sensors-panel").hidden;
    $("#sensors-panel").hidden = !open;
    $("#sensors-button").setAttribute("aria-expanded", String(open));
    (open ? $("#close-sensors") : $("#sensors-button")).focus();
  });
  $("#close-sensors").addEventListener("click", () => {
    $("#sensors-panel").hidden = true;
    $("#sensors-button").setAttribute("aria-expanded", "false");
    $("#sensors-button").focus();
  });
  $("#close-detail").addEventListener("click", () => hideDetail());
  $("#legend-chip").addEventListener("click", () => {
    const legend = $("#legend");
    legend.hidden = !legend.hidden;
    $("#legend-chip").setAttribute("aria-expanded", String(!legend.hidden));
  });
  $("#filter-chip").addEventListener("click", () => {
    state.config.location_filter = FILTER_NEXT[state.config.location_filter] || "indoor";
    stampFilterChip();
    if (window.webkit?.messageHandlers?.airloom) bridge({ action: "set-location-filter", value: state.config.location_filter });
    else { renderAll(); reconcilePopup(); } // preview: filter locally, no bridge round-trip
  });
  $("#map-panel").addEventListener("pointerdown", (event) => { if (event.target.closest("button, a, .map-popup")) return; beginDirectManipulation(); hideTransientOverlays(); state.drag = { x: event.clientX, y: event.clientY }; event.currentTarget.setPointerCapture(event.pointerId); event.currentTarget.classList.add("dragging"); });
  $("#map-panel").addEventListener("pointermove", (event) => { if (!state.drag || !(event.buttons & 1)) return; const dx = event.clientX - state.drag.x, dy = event.clientY - state.drag.y; state.drag = { x: event.clientX, y: event.clientY }; panBy(dx, dy); });
  // Rebuild (re-cull) markers only after an actual drag: rebuilding on every
  // pointerup destroys a pressed marker between pointerup and click, so the
  // click never fires and markers are unclickable. state.drag is never set
  // for marker presses (pointerdown early-returns on buttons).
  $("#map-panel").addEventListener("pointerup", (event) => { const dragged = state.drag !== null; state.drag = null; event.currentTarget.classList.remove("dragging"); if (dragged) renderMapMarkers(); });
  $("#map-panel").addEventListener("pointercancel", (event) => { state.drag = null; event.currentTarget.classList.remove("dragging"); });
  function isZoomWheel(event) {
    return Boolean(event.deltaY) && Math.abs(event.deltaX) <= Math.abs(event.deltaY);
  }

  $("#map-panel").addEventListener("wheel", (event) => {
    if (event.ctrlKey) return; // pinch path; the document-level handler owns it
    if (!isZoomWheel(event)) return;
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    const base = zoomAnim ? zoomAnim.target : Math.round(state.zoom);
    animateZoomTo(base + (event.deltaY < 0 ? 1 : -1), { x: event.clientX - rect.left, y: event.clientY - rect.top });
  }, { passive: false });

  let pinch = null;
  let pinchSettleTimer = null;
  let lastZoomAnchor = null;

  function mapAnchorFromClient(clientX, clientY) {
    const rect = $("#map-panel").getBoundingClientRect();
    return { x: clientX - rect.left, y: clientY - rect.top };
  }

  // Pinch/ctrl+wheel should drive the map only when the gesture is over it;
  // ctrl-scrolling over the sensors list, detail card, search results, or
  // the open settings dialog must not zoom the map underneath them.
  function overBlockingOverlay(target) {
    return !!(target && target.closest && target.closest("#settings-dialog, #sensors-panel, #detail-card, #search-results"));
  }

  // WebKitGTK delivers trackpad pinch either as ctrl+wheel or as proprietary
  // gesture* events depending on version/compositor. preventDefault on both,
  // unconditionally, so the engine can never apply page zoom (which scaled the
  // whole document and clipped the fixed overlays).
  document.addEventListener("wheel", (event) => {
    if (!event.ctrlKey) return;
    event.preventDefault();
    if (overBlockingOverlay(event.target) || !isZoomWheel(event)) return;
    beginDirectManipulation();
    hideTransientOverlays();
    lastZoomAnchor = mapAnchorFromClient(event.clientX, event.clientY);
    applyZoom(state.zoom - event.deltaY * 0.01, lastZoomAnchor);
    clearTimeout(pinchSettleTimer);
    pinchSettleTimer = setTimeout(() => animateZoomTo(Math.round(state.zoom), lastZoomAnchor), 180);
  }, { passive: false });

  document.addEventListener("gesturestart", (event) => {
    event.preventDefault();
    if (overBlockingOverlay(event.target)) return;
    beginDirectManipulation();
    hideTransientOverlays();
    lastZoomAnchor = null;
    pinch = { startZoom: state.zoom };
  }, { passive: false });
  document.addEventListener("gesturechange", (event) => {
    event.preventDefault();
    if (!pinch) return;
    lastZoomAnchor = mapAnchorFromClient(event.clientX, event.clientY);
    applyZoom(pinch.startZoom + Math.log2(Math.max(0.05, event.scale)), lastZoomAnchor);
  }, { passive: false });
  document.addEventListener("gestureend", (event) => {
    event.preventDefault();
    if (!pinch) return;
    pinch = null;
    animateZoomTo(Math.round(state.zoom), lastZoomAnchor || centerAnchor());
  }, { passive: false });

  // Native (GTK-level) pinch path: on this platform WebKitGTK's internal
  // gesture controller consumes trackpad pinch as page scale before the DOM
  // ever sees ctrl+wheel or gesture* events, so app.py intercepts the
  // GtkGestureZoom in capture phase and forwards begin/change/end over the
  // bridge as a "pinch" event. x/y arrive as widget (CSS-pixel) coordinates
  // for the webview, which fills the whole content area — #map-panel is
  // `position: absolute; inset: 0` inside that same page, so those
  // coordinates are already map-panel-relative and need no translation
  // before being passed to applyZoom()'s anchor.
  let nativePinchStartZoom = null;
  function nativePinch(payload) {
    if (payload.phase === "begin") {
      const blocked = document.elementFromPoint(payload.x, payload.y)?.closest("#settings-dialog, #sensors-panel, #detail-card, #search-results");
      nativePinchStartZoom = blocked ? null : state.zoom;
      if (nativePinchStartZoom === null) return;
      beginDirectManipulation();
      hideTransientOverlays();
    } else if (payload.phase === "change") {
      if (nativePinchStartZoom === null) return;
      lastZoomAnchor = { x: payload.x, y: payload.y };
      applyZoom(nativePinchStartZoom + Math.log2(Math.max(0.05, payload.scale)), lastZoomAnchor);
    } else if (payload.phase === "end") {
      if (nativePinchStartZoom === null) return;
      nativePinchStartZoom = null;
      animateZoomTo(Math.round(state.zoom), lastZoomAnchor || centerAnchor());
    }
  }

  $("#close-settings").addEventListener("click", () => $("#settings-dialog").close("cancel"));
  $("#cancel-settings").addEventListener("click", () => $("#settings-dialog").close("cancel"));
  $("#settings-dialog").addEventListener("close", () => { state.homeSearchActive = false; });
  document.querySelectorAll('input[name="home_mode"]').forEach((radio) => radio.addEventListener("change", () => {
    $("#home-place-row").hidden = $("#settings-form").elements.home_mode.value !== "fixed";
  }));
  let homePlaceTimer = null;
  $("#home-place-input").addEventListener("input", (event) => {
    clearTimeout(homePlaceTimer);
    const query = event.target.value.trim();
    if (query.length >= 3) homePlaceTimer = setTimeout(() => { state.homeSearchActive = true; bridge({ action: "place-search", query }); }, 450);
  });
  $("#settings-form").addEventListener("submit", (event) => {
    const submitter = event.submitter;
    if (!submitter || submitter.id !== "save-settings") return;
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    bridge({
      action: "save-settings",
      api_key: form.get("api_key"), clear_api_key: form.get("clear_api_key") === "on",
      confidence_filter: form.get("confidence_filter") === "on",
      home_mode: form.get("home_mode"), home_lat: form.get("home_lat"), home_lon: form.get("home_lon"), location_name: form.get("location_name"),
      radius_km: form.get("radius_km"), heatmap_threshold_km: form.get("heatmap_threshold_km"), alert_threshold: form.get("alert_threshold"), refresh_minutes: form.get("refresh_minutes"), temperature_unit: form.get("temperature_unit"),
    });
    $("#settings-dialog").close();
  });
  new ResizeObserver(() => renderMap()).observe($("#map-panel"));
  bridge({ action: "ready" });
  // Makes the interface directly previewable in an ordinary browser while the
  // desktop build continues to receive its state from the Python bridge.
  if (!window.webkit?.messageHandlers?.airloom) setTimeout(() => {
    const preview = browserPreviewData();
    // Two sensors start hidden so the Settings section is explorable in preview.
    state.previewHidden = preview.splice(10, 2);
    state.config.hidden = state.previewHidden.map((s) => ({ id: s.id, name: s.name }));
    applySensors({ items: preview, selected_id: 8000, source: "Browser preview · Demo data", config: state.config });
  }, 30);
})();
