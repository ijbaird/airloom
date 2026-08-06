(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const state = {
    sensors: [],
    selectedId: null,
    config: { latitude: 45.5152, longitude: -122.6784, location_name: "Portland, Oregon", radius_km: 22, temperature_unit: "F", alert_threshold: 101, has_api_key: false, api_key_hint: "" },
    source: "Starting Airloom",
    center: { lat: 45.5152, lon: -122.6784 },
    home: { lat: 45.5152, lon: -122.6784 },
    zoom: 12,
    drag: null,
    query: "",
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
    },
  };

  function applyConfig(config) {
    state.config = { ...state.config, ...config };
    state.home = { lat: Number(config.latitude), lon: Number(config.longitude) };
    state.center = { ...state.home };
    $("#place-name").textContent = config.location_name;
    renderMap();
  }

  function applySensors(payload) {
    state.sensors = payload.items || [];
    state.selectedId = payload.selected_id ?? state.sensors[0]?.id ?? null;
    state.source = payload.source || "Demo data";
    state.config = { ...state.config, ...(payload.config || {}) };
    $("#place-name").textContent = state.config.location_name;
    $("#data-source").textContent = state.source;
    renderAll();
  }

  function renderAll() {
    renderSummary();
    renderLists();
    renderMap();
    renderDetail();
  }

  function renderSummary() {
    const valid = state.sensors.filter((s) => Number.isFinite(s.aqi)).sort((a, b) => a.aqi - b.aqi);
    const sensor = valid[Math.floor(valid.length / 2)];
    if (!sensor) return;
    $("#summary-aqi").textContent = sensor.aqi;
    $("#summary-aqi").style.background = sensor.color;
    $("#summary-aqi").style.color = sensor.foreground;
    $("#summary-label").textContent = sensor.category;
    $("#summary-subtitle").textContent = `${valid.length} outdoor sensors in range`;
  }

  function visibleSensors() {
    const query = state.query.trim().toLowerCase();
    return query ? state.sensors.filter((s) => s.name.toLowerCase().includes(query) || String(s.aqi).includes(query)) : state.sensors;
  }

  function renderLists() {
    const visible = visibleSensors();
    const favorites = visible.filter((sensor) => sensor.favorite);
    $("#sensor-count").textContent = visible.length;
    $("#favorite-count").textContent = favorites.length;
    $("#favorites-list").innerHTML = favorites.length ? favorites.map(sensorRow).join("") : '<div class="empty-state small">Star a sensor to keep it close.</div>';
    $("#sensor-list").innerHTML = visible.length ? visible.map(sensorRow).join("") : '<div class="empty-state">No sensors match that search.</div>';
    document.querySelectorAll(".sensor-row").forEach((row) => row.addEventListener("click", () => selectSensor(Number(row.dataset.id), true)));
  }

  function sensorRow(sensor) {
    const selected = sensor.id === state.selectedId ? " selected" : "";
    const meta = [formatTemperature(sensor.temperature_f), formatHumidity(sensor.humidity)].filter(Boolean).join(" · ");
    return `<button class="sensor-row${selected}" data-id="${sensor.id}" style="--sensor:${sensor.color};--sensor-fg:${sensor.foreground}">
      <span class="sensor-badge">${sensor.aqi ?? "—"}</span>
      <span class="sensor-copy"><strong>${escapeHtml(sensor.name)}</strong><small>${meta || "Recent outdoor reading"}</small></span>
      ${sensor.favorite ? '<span class="row-star"><svg viewBox="0 0 24 24"><path d="m12 17.3-6.2 3.5 1.4-6.9-5.1-4.7 7-.8L12 2l2.9 6.4 7 .8-5.1 4.7 1.4 6.9-6.2-3.5Z"/></svg></span>' : ""}
    </button>`;
  }

  function selectSensor(id, revealDetail = false) {
    state.selectedId = id;
    bridge({ action: "select", id });
    renderLists();
    renderMapMarkers();
    renderDetail();
    if (revealDetail) document.body.classList.add("show-detail");
  }

  function selectedSensor() {
    return state.sensors.find((sensor) => sensor.id === state.selectedId);
  }

  function renderDetail() {
    const sensor = selectedSensor();
    if (!sensor) return;
    $("#sensor-name").textContent = sensor.name;
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
    $("#updated-time").textContent = relativeTime(sensor.last_seen);
    renderChart(sensor);
  }

  function renderChart(sensor) {
    const points = (sensor.trend || []).filter((point) => Number.isFinite(point.aqi));
    if (!points.length) { $("#chart").innerHTML = '<div class="empty-state">No trend available</div>'; return; }
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
  function worldPoint(lat, lon, zoom) {
    const size = 256 * 2 ** zoom;
    const sin = Math.sin(lat * Math.PI / 180);
    return { x: (lon + 180) / 360 * size, y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * size };
  }

  function inverseWorld(x, y, zoom) {
    const size = 256 * 2 ** zoom;
    const lon = x / size * 360 - 180;
    const n = Math.PI - 2 * Math.PI * y / size;
    const lat = 180 / Math.PI * Math.atan(Math.sinh(n));
    return { lat, lon };
  }

  function renderMap() {
    const panel = $("#map-panel");
    if (!panel.clientWidth || !panel.clientHeight) return;
    const center = worldPoint(state.center.lat, state.center.lon, state.zoom);
    const left = center.x - panel.clientWidth / 2;
    const top = center.y - panel.clientHeight / 2;
    const tiles = [];
    const maxTile = 2 ** state.zoom;
    for (let ty = Math.floor(top / 256); ty <= Math.floor((top + panel.clientHeight) / 256); ty++) {
      if (ty < 0 || ty >= maxTile) continue;
      for (let tx = Math.floor(left / 256); tx <= Math.floor((left + panel.clientWidth) / 256); tx++) {
        const wrappedX = ((tx % maxTile) + maxTile) % maxTile;
        tiles.push(`<img class="tile" draggable="false" alt="" src="https://tile.openstreetmap.org/${state.zoom}/${wrappedX}/${ty}.png" style="left:${tx * 256 - left}px;top:${ty * 256 - top}px">`);
      }
    }
    $("#tiles").innerHTML = tiles.join("");
    renderMapMarkers();
  }

  function renderMapMarkers() {
    const panel = $("#map-panel");
    if (!panel.clientWidth) return;
    const center = worldPoint(state.center.lat, state.center.lon, state.zoom);
    const markers = visibleSensors().map((sensor) => {
      const point = worldPoint(sensor.latitude, sensor.longitude, state.zoom);
      const x = point.x - center.x + panel.clientWidth / 2;
      const y = point.y - center.y + panel.clientHeight / 2;
      if (x < -40 || y < -40 || x > panel.clientWidth + 40 || y > panel.clientHeight + 40) return "";
      return `<button class="map-marker${sensor.id === state.selectedId ? " selected" : ""}" data-id="${sensor.id}" title="${escapeHtml(sensor.name)} · AQI ${sensor.aqi ?? "unavailable"}" style="left:${x}px;top:${y}px;--sensor:${sensor.color};--sensor-fg:${sensor.foreground}">${sensor.aqi ?? "—"}</button>`;
    });
    $("#markers").innerHTML = markers.join("");
    document.querySelectorAll(".map-marker").forEach((marker) => marker.addEventListener("click", (event) => { event.stopPropagation(); selectSensor(Number(marker.dataset.id), true); }));
  }

  function panBy(dx, dy) {
    const center = worldPoint(state.center.lat, state.center.lon, state.zoom);
    state.center = inverseWorld(center.x - dx, center.y - dy, state.zoom);
    renderMap();
  }

  function zoom(delta) {
    state.zoom = Math.max(3, Math.min(17, state.zoom + delta));
    renderMap();
  }

  function openSettings(config = state.config) {
    const form = $("#settings-form");
    for (const field of ["location_name", "latitude", "longitude", "radius_km", "alert_threshold"]) form.elements[field].value = config[field];
    form.elements.api_key.value = "";
    form.elements.clear_api_key.checked = false;
    form.elements.temperature_unit.value = config.temperature_unit || "F";
    $("#key-status").textContent = config.has_api_key ? `Saved key ${config.api_key_hint}` : "No key saved — demo mode";
    $("#settings-dialog").showModal();
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

  function browserPreviewData() {
    const readings = [18, 34, 47, 56, 72, 88, 109, 43, 63, 31, 81, 52];
    const names = ["Alberta Arts", "Laurelhurst Park", "Mount Tabor", "Sellwood Garden", "Buckman School", "Overlook Bluff", "St. Johns North", "Hawthorne Ridge", "Woodstock Library", "Council Crest", "Irvington Air", "Rose City Park"];
    const colors = (aqi) => aqi <= 50 ? ["Good", "#35b779", "#08271b"] : aqi <= 100 ? ["Moderate", "#f6c945", "#332400"] : ["Unhealthy for sensitive groups", "#f39c3d", "#341900"];
    return readings.map((aqi, index) => {
      const [category, color, foreground] = colors(aqi);
      const angle = index * 2.399963;
      return { id: 8000 + index, name: names[index], latitude: 45.5152 + Math.sin(angle) * (.018 + index % 3 * .012), longitude: -122.6784 + Math.cos(angle) * (.024 + index % 3 * .016), aqi, category, color, foreground, pm25: Math.round((aqi / 3.1) * 10) / 10, pm10: Math.round((aqi / 2.3) * 10) / 10, temperature_f: 64 + index % 8, humidity: 43 + index % 6 * 5, last_seen: Math.round(Date.now() / 1000) - index * 24, favorite: index < 2, guidance: aqi <= 50 ? "Air quality is satisfactory. It is a good time to be outside." : aqi <= 100 ? "Unusually sensitive people may want to reduce prolonged outdoor exertion." : "Sensitive groups should reduce prolonged or heavy outdoor exertion.", trend: ["1w", "1d", "6h", "1h", "30m", "10m", "Now"].map((label, point) => ({ label, aqi: Math.max(4, aqi + Math.round(Math.sin(index + point) * 11)) })) };
    });
  }

  $("#search").addEventListener("input", (event) => { state.query = event.target.value; renderLists(); renderMapMarkers(); });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); $("#search").focus(); }
    if ((event.ctrlKey || event.metaKey) && event.key === ",") { event.preventDefault(); openSettings(); }
    if (event.key === "Escape") document.body.classList.remove("show-detail");
  });
  $("#footer-refresh").addEventListener("click", () => bridge({ action: "refresh" }));
  $("#favorite-button").addEventListener("click", () => { if (state.selectedId !== null) bridge({ action: "favorite", id: state.selectedId }); });
  $("#zoom-in").addEventListener("click", (event) => { event.stopPropagation(); zoom(1); });
  $("#zoom-out").addEventListener("click", (event) => { event.stopPropagation(); zoom(-1); });
  $("#recenter").addEventListener("click", (event) => { event.stopPropagation(); state.center = { ...state.home }; renderMap(); });
  $("#show-map").addEventListener("click", () => { document.body.classList.add("show-map"); setTimeout(renderMap, 250); });
  $("#show-list").addEventListener("click", (event) => { event.stopPropagation(); document.body.classList.remove("show-map"); });
  $("#back-to-map").addEventListener("click", () => document.body.classList.remove("show-detail"));
  $("#map-panel").addEventListener("pointerdown", (event) => { if (event.target.closest("button")) return; state.drag = { x: event.clientX, y: event.clientY }; event.currentTarget.setPointerCapture(event.pointerId); event.currentTarget.classList.add("dragging"); });
  $("#map-panel").addEventListener("pointermove", (event) => { if (!state.drag) return; const dx = event.clientX - state.drag.x, dy = event.clientY - state.drag.y; state.drag = { x: event.clientX, y: event.clientY }; panBy(dx, dy); });
  $("#map-panel").addEventListener("pointerup", (event) => { state.drag = null; event.currentTarget.classList.remove("dragging"); });
  $("#map-panel").addEventListener("wheel", (event) => { event.preventDefault(); zoom(event.deltaY < 0 ? 1 : -1); }, { passive: false });
  $("#settings-form").addEventListener("submit", (event) => {
    const submitter = event.submitter;
    if (!submitter || submitter.id !== "save-settings") return;
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    bridge({
      action: "save-settings",
      api_key: form.get("api_key"), clear_api_key: form.get("clear_api_key") === "on",
      location_name: form.get("location_name"), latitude: form.get("latitude"), longitude: form.get("longitude"),
      radius_km: form.get("radius_km"), alert_threshold: form.get("alert_threshold"), temperature_unit: form.get("temperature_unit"),
    });
    $("#settings-dialog").close();
  });
  new ResizeObserver(() => renderMap()).observe($("#map-panel"));
  bridge({ action: "ready" });
  // Makes the interface directly previewable in an ordinary browser while the
  // desktop build continues to receive its state from the Python bridge.
  if (!window.webkit?.messageHandlers?.airloom) setTimeout(() => applySensors({ items: browserPreviewData(), selected_id: 8000, source: "Browser preview · Demo data", config: state.config }), 30);
})();
