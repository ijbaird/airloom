# Hidden Sensors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user hide individual sensors (erroneous ones) everywhere in the app, with a Settings section listing hidden sensors to unhide them individually or all at once.

**Architecture:** Python-side filtering. `Store` persists a `hidden` dict of `str(sensor_id) → last-known name`. `app.py` keeps everything fetched in `self.sensors` but filters hidden sensors out in `_send_sensor_state`, so the web UI never renders them (map, lists, heat-map, counts all correct for free) and unhide restores instantly without a refetch. Alerts skip hidden sensors. New bridge actions: `hide` (toggle), `unhide`, `unhide-all`. The web UI adds eye-off hide buttons (detail pane + map popup) and a "Hidden sensors" section in Settings rendered from `config.hidden`.

**Tech Stack:** Python stdlib + PyGObject (no pip packages), hand-written vanilla JS/CSS (no JS dependencies). Tests: stdlib `unittest`, GUI-free modules only.

**Spec:** `docs/superpowers/specs/2026-08-08-hidden-sensors-design.md`

## Global Constraints

- Zero third-party dependencies: Python stdlib + system PyGObject only; vanilla JS/CSS only (CLAUDE.md).
- Tests must run without GTK — only GUI-free modules get unit tests; `app.py` is verified via `compileall` and the debug port.
- Never touch GTK/WebKit from a worker thread (not applicable here — all new Python code runs in bridge handlers on the main loop).
- Data-behavior changes need tests (CONTRIBUTING.md) — `store.py` changes are TDD'd in `tests/test_store.py`.
- `make check` (unittest discover + `python3 -m compileall -q airloom` + `node --check airloom/resources/app.js`) must pass before the PR.
- Hidden names cap at 80 chars, same convention as `location_name`.
- Working directory is the worktree root (`.claude/worktrees/hidden-sensors`); all paths below are relative to it.

---

### Task 1: Store persistence for hidden sensors

**Files:**
- Modify: `airloom/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces (used by Task 2):
  - `Store.hide(sensor_id: int, name: str) -> None` — records the sensor and saves.
  - `Store.unhide(sensor_id: int) -> None` — removes if present, saves only when something changed.
  - `Store.unhide_all() -> None` — clears, saves only when something changed.
  - `Store.is_hidden(sensor_id: int) -> bool`
  - `Store.hidden_ids() -> set[int]`
  - `Store.public_config()["hidden"]` — `[{"id": int, "name": str}, ...]` sorted case-insensitively by name, then id.
- Data shape on disk: `config["hidden"]` is `{"12345": "Sensor name", ...}` (JSON object keys are strings).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store.py` (inside `StoreTest`, before `if __name__ == "__main__":`):

```python
    def test_hidden_defaults_empty_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            self.assertEqual(store.data["hidden"], {})
            self.assertEqual(store.public_config()["hidden"], [])
            store.hide(4242, "Backyard PurpleAir")
            self.assertTrue(store.is_hidden(4242))
            self.assertEqual(store.hidden_ids(), {4242})
            loaded = Store(path)
            self.assertEqual(loaded.data["hidden"], {"4242": "Backyard PurpleAir"})
            self.assertEqual(
                loaded.public_config()["hidden"],
                [{"id": 4242, "name": "Backyard PurpleAir"}],
            )

    def test_unhide_and_unhide_all(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            store.hide(1, "One")
            store.hide(2, "Two")
            store.unhide(1)
            self.assertFalse(store.is_hidden(1))
            self.assertEqual(Store(path).hidden_ids(), {2})
            store.unhide(999)  # no-op on unknown id, must not raise
            store.unhide_all()
            self.assertEqual(store.hidden_ids(), set())
            self.assertEqual(Store(path).data["hidden"], {})

    def test_hidden_sanitize_drops_garbage_and_truncates(self):
        corrupt = {
            "hidden": {
                "123": "Valid",
                "007": "Padded key",
                "abc": "bad key",
                "9": 42,
                "10": "x" * 200,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(corrupt), encoding="utf-8")
            store = Store(path)
            self.assertEqual(
                store.data["hidden"],
                {"123": "Valid", "7": "Padded key", "10": "x" * 80},
            )

    def test_hidden_wrong_type_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"hidden": ["12"]}), encoding="utf-8")
            self.assertEqual(Store(path).data["hidden"], {})

    def test_hidden_names_sort_case_insensitively_in_public_config(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "config.json")
            store.hide(3, "zebra")
            store.hide(1, "Alpha")
            store.hide(2, "beta")
            self.assertEqual(
                [item["name"] for item in store.public_config()["hidden"]],
                ["Alpha", "beta", "zebra"],
            )

    def test_hide_keeps_favorite_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = Store(path)
            store.toggle_favorite(77)
            store.hide(77, "Starred and hidden")
            self.assertEqual(store.data["favorites"], [77])
            store.unhide(77)
            self.assertEqual(Store(path).data["favorites"], [77])

    def test_hide_truncates_and_strips_name(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "config.json")
            store.hide(5, "  padded  ")
            self.assertEqual(store.data["hidden"]["5"], "padded")
            store.hide(6, "y" * 200)
            self.assertEqual(store.data["hidden"]["6"], "y" * 80)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_store -v`
Expected: the seven new tests FAIL (`KeyError: 'hidden'` / `AttributeError: 'Store' object has no attribute 'hide'`); all pre-existing tests still pass.

- [ ] **Step 3: Implement in `airloom/store.py`**

Add to `DEFAULT_CONFIG` (after `"alert_states": {}`):

```python
    "hidden": {},
```

Add to `_sanitize` (after the `alert_states` block, before `return clean`):

```python
    hidden = data.get("hidden")
    if isinstance(hidden, dict):
        clean["hidden"] = {
            str(int(key)): value.strip()[:80]
            for key, value in hidden.items()
            if isinstance(key, str) and key.isdigit() and isinstance(value, str)
        }
```

Add to `public_config()` returned dict (after `"api_key_hint"`):

```python
            "hidden": sorted(
                ({"id": int(key), "name": name} for key, name in self.data["hidden"].items()),
                key=lambda item: (item["name"].lower(), item["id"]),
            ),
```

Add methods after `toggle_favorite` (mirroring its style — mutate, then `save()`):

```python
    def hide(self, sensor_id: int, name: str) -> None:
        self.data["hidden"][str(sensor_id)] = str(name).strip()[:80]
        self.save()

    def unhide(self, sensor_id: int) -> None:
        if self.data["hidden"].pop(str(sensor_id), None) is not None:
            self.save()

    def unhide_all(self) -> None:
        if self.data["hidden"]:
            self.data["hidden"] = {}
            self.save()

    def is_hidden(self, sensor_id: int) -> bool:
        return str(sensor_id) in self.data["hidden"]

    def hidden_ids(self) -> set[int]:
        return {int(key) for key in self.data["hidden"]}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_store -v`
Expected: ALL pass (pre-existing + 7 new).

- [ ] **Step 5: Run the full suite**

Run: `make check`
Expected: all tests pass, compileall and node --check clean.

- [ ] **Step 6: Commit**

```bash
git add airloom/store.py tests/test_store.py
git commit -m "store: persist hidden sensors with sanitize + public_config list"
```

---

### Task 2: Bridge actions and Python-side filtering in app.py

**Files:**
- Modify: `airloom/app.py` (dispatch in `_on_script_message` ~line 452; `_send_sensor_state` ~line 699; `_check_alerts` ~line 708)

**Interfaces:**
- Consumes (from Task 1): `store.hide(id, name)`, `store.unhide(id)`, `store.unhide_all()`, `store.is_hidden(id)`, `store.hidden_ids()`.
- Produces (relied on by Task 3):
  - Bridge actions `hide {id}` (toggle), `unhide {id}`, `unhide-all` — each responds with a full `sensors` payload (`_send_sensor_state`), whose `config` carries the refreshed `hidden` list.
  - The `sensors` payload `items` never contains hidden sensors; `selected_id` is never a hidden sensor's id.
- No unit tests: `app.py` imports `gi` at module level and is excluded from the GUI-free test suite. Verified by `python3 -m compileall` here and end-to-end over the debug port in Task 4.

- [ ] **Step 1: Add bridge actions to `_on_script_message`**

In `airloom/app.py`, after the `elif action == "favorite":` block (ends `self._send_sensor_state()`), insert:

```python
        elif action == "hide":
            sensor_id = self._message_sensor_id(message)
            if sensor_id is not None:
                if self.store.is_hidden(sensor_id):
                    self.store.unhide(sensor_id)
                    self._send_sensor_state()
                else:
                    sensor = next((s for s in self.sensors if s.sensor_id == sensor_id), None)
                    if sensor is not None:
                        self.store.hide(sensor_id, sensor.name)
                        self._send_sensor_state()
        elif action == "unhide":
            sensor_id = self._message_sensor_id(message)
            if sensor_id is not None:
                self.store.unhide(sensor_id)
                self._send_sensor_state()
        elif action == "unhide-all":
            self.store.unhide_all()
            self._send_sensor_state()
```

(The `sensor is not None` guard mirrors the `favorite` action's rule: only sensors we actually have can be acted on, and it supplies the last-known name.)

- [ ] **Step 2: Filter hidden sensors in `_send_sensor_state`**

Replace the body of `_send_sensor_state` with:

```python
    def _send_sensor_state(self, source: str | None = None) -> None:
        hidden = self.store.hidden_ids()
        visible = [sensor for sensor in self.sensors if sensor.sensor_id not in hidden]
        # Hiding the selected sensor must not leave a dangling selection —
        # same reconciliation rule _finish_refresh applies after a fetch.
        if self.selected_id not in {sensor.sensor_id for sensor in visible}:
            self.selected_id = visible[0].sensor_id if visible else None
        payload = {
            "items": [sensor.to_dict() for sensor in visible],
            "selected_id": self.selected_id,
            "source": source or self.last_source or ("PurpleAir live" if self.store.data.get("api_key") else "Demo data"),
            "config": self.store.public_config(),
        }
        self._send("sensors", payload)
```

- [ ] **Step 3: Skip hidden sensors in `_check_alerts`**

In `_check_alerts`, add one line before the `for sensor in self.sensors:` loop and extend its guard:

```python
        hidden = self.store.hidden_ids()
        for sensor in self.sensors:
            if not sensor.favorite or sensor.aqi is None or sensor.sensor_id in hidden:
                continue
```

(The rest of the loop is unchanged. The `states` cleanup above it keys off favorites and stays as is.)

- [ ] **Step 4: Verify**

Run: `make check`
Expected: all tests pass (none cover app.py), `compileall` clean — proves the file parses and the edits are syntactically sound.

- [ ] **Step 5: Commit**

```bash
git add airloom/app.py
git commit -m "app: hide/unhide bridge actions, filter hidden sensors from UI and alerts"
```

---

### Task 3: Web UI — hide buttons, Settings section, preview fallback

**Files:**
- Modify: `airloom/resources/index.html`
- Modify: `airloom/resources/app.js`
- Modify: `airloom/resources/app.css`

**Interfaces:**
- Consumes (from Task 2): bridge actions `hide`/`unhide`/`unhide-all`; `config.hidden` as `[{id, name}, ...]`; hidden sensors absent from `sensors` payloads.
- Produces: `#popup-hide`, `#hide-button`, `#hidden-list`, `#unhide-all` DOM ids (referenced by Task 4's debug-port checks); `debugState().hiddenCount`.

- [ ] **Step 1: index.html — popup hide button**

In `#map-popup`'s `.popup-head`, immediately after the `#popup-favorite` button, add:

```html
              <button class="icon-button popup-star" id="popup-hide" title="Hide this sensor" aria-label="Hide this sensor">
                <svg viewBox="0 0 24 24"><path d="M12 7a5 5 0 0 1 5 5c0 .65-.13 1.26-.36 1.83l2.92 2.92A11.8 11.8 0 0 0 22.99 12C21.26 7.61 17 4.5 12 4.5c-1.4 0-2.74.25-3.98.7l2.16 2.16C10.74 7.13 11.35 7 12 7ZM2 4.27l2.74 2.74C3.08 8.3 1.78 10.02 1 12c1.73 4.39 6 7.5 11 7.5 1.55 0 3.03-.3 4.38-.84L19.73 22 21 20.73 3.27 3 2 4.27ZM7.53 9.8l1.55 1.55a3 3 0 0 0 3.57 3.57l1.55 1.55A5 5 0 0 1 7.53 9.8Zm4.31-.78 3.13 3.13.03-.15a3 3 0 0 0-3-3l-.16.02Z"/></svg>
              </button>
```

- [ ] **Step 2: index.html — detail pane hide button**

In `#detail-card`'s `.detail-actions`, between `#favorite-button` and `#close-detail`, add:

```html
            <button class="icon-button" id="hide-button" title="Hide this sensor" aria-label="Hide this sensor">
              <svg viewBox="0 0 24 24"><path d="M12 7a5 5 0 0 1 5 5c0 .65-.13 1.26-.36 1.83l2.92 2.92A11.8 11.8 0 0 0 22.99 12C21.26 7.61 17 4.5 12 4.5c-1.4 0-2.74.25-3.98.7l2.16 2.16C10.74 7.13 11.35 7 12 7ZM2 4.27l2.74 2.74C3.08 8.3 1.78 10.02 1 12c1.73 4.39 6 7.5 11 7.5 1.55 0 3.03-.3 4.38-.84L19.73 22 21 20.73 3.27 3 2 4.27ZM7.53 9.8l1.55 1.55a3 3 0 0 0 3.57 3.57l1.55 1.55A5 5 0 0 1 7.53 9.8Zm4.31-.78 3.13 3.13.03-.15a3 3 0 0 0-3-3l-.16.02Z"/></svg>
            </button>
```

The `.icon-button` class already sizes/hovers it; the svg needs a width rule (Step 6).

- [ ] **Step 3: index.html — Settings "Hidden sensors" section**

In `#settings-form`'s `.form-grid`, after the Temperature fieldset (line ~140), add:

```html
          <fieldset class="wide hidden-fieldset"><legend>Hidden sensors</legend>
            <div class="hidden-list" id="hidden-list"></div>
            <button type="button" id="unhide-all" class="unhide-button" hidden>Unhide all</button>
          </fieldset>
```

(`type="button"` is required — bare `<button>` inside the form would submit the dialog.)

- [ ] **Step 4: app.js — state, helpers, rendering**

1. In the `state` literal, add `previewHidden: [],` after `popupId: null,` and add `hidden: []` to the `config` default object (after `location_filter: "outdoor"`).

2. In `debugState`, add after `location_filter: state.config.location_filter,`:

```js
      hiddenCount: (state.config.hidden || []).length,
```

3. After the `openSettings` function, add:

```js
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
```

4. In `openSettings`, before `$("#settings-dialog").showModal();`, add:

```js
    renderHiddenList();
```

5. In `applySensors`, after `state.config = { ...state.config, ...(payload.config || {}) };`, add:

```js
    if ($("#settings-dialog").open) renderHiddenList();
```

- [ ] **Step 5: app.js — button wiring and preview boot**

1. With the other listeners (after the `#popup-favorite` line ~818), add:

```js
  $("#hide-button").addEventListener("click", () => {
    if (state.selectedId === null) return;
    const id = state.selectedId;
    $("#detail-card").hidden = true; // deterministic close; the resend reselects another sensor
    requestHide(id);
  });
  $("#popup-hide").addEventListener("click", () => {
    if (state.popupId === null) return;
    const id = state.popupId;
    hidePopup();
    requestHide(id);
  });
  $("#unhide-all").addEventListener("click", () => {
    if (window.webkit?.messageHandlers?.airloom) { bridge({ action: "unhide-all" }); return; }
    state.sensors.push(...state.previewHidden.splice(0));
    previewSyncHidden();
  });
```

2. Replace the preview boot line at the bottom:

```js
  if (!window.webkit?.messageHandlers?.airloom) setTimeout(() => applySensors({ items: browserPreviewData(), selected_id: 8000, source: "Browser preview · Demo data", config: state.config }), 30);
```

with:

```js
  if (!window.webkit?.messageHandlers?.airloom) setTimeout(() => {
    const preview = browserPreviewData();
    // Two sensors start hidden so the Settings section is explorable in preview.
    state.previewHidden = preview.splice(10, 2);
    state.config.hidden = state.previewHidden.map((s) => ({ id: s.id, name: s.name }));
    applySensors({ items: preview, selected_id: 8000, source: "Browser preview · Demo data", config: state.config });
  }, 30);
```

- [ ] **Step 6: app.css — styles**

After the `.empty-state.small` rule (~line 112), add:

```css
.hidden-fieldset { flex-direction: column; gap: 8px; }
.hidden-list { display: flex; flex-direction: column; gap: 6px; width: 100%; }
.hidden-list .empty-state.small { padding: 8px; }
.hidden-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 12px; }
.hidden-row span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.unhide-button { border: 0; border-radius: 8px; padding: 6px 10px; background: var(--soft); color: var(--ink); font: inherit; font-size: 11px; font-weight: 700; cursor: pointer; flex: none; }
.unhide-button:hover { background: var(--line); }
#unhide-all { align-self: flex-start; }
```

And with the icon sizing rules near `.favorite-button, .icon-button` (~line 119), add:

```css
#hide-button svg, #popup-hide svg { width: 17px; fill: currentColor; }
```

- [ ] **Step 7: Verify syntax and preview**

Run: `node --check airloom/resources/app.js && make check`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add airloom/resources/index.html airloom/resources/app.js airloom/resources/app.css
git commit -m "ui: hide buttons, hidden-sensors settings section, preview fallback"
```

---

### Task 4: End-to-end verification over the debug port

**Files:** none modified (verification only; fix regressions if found).

**Interfaces:**
- Consumes: everything above, plus `scripts/debug-run` / `scripts/debug-client` and `window.Airloom.debugState()`.

- [ ] **Step 1: Launch the app on the debug socket**

```bash
scripts/debug-run &   # picks $XDG_RUNTIME_DIR/airloom-debug.sock, kills stray instances
sleep 4 && scripts/debug-client ping
```

Expected: JSON with `"debug": true` and the version.

- [ ] **Step 2: Hide the selected sensor via the detail-pane button path**

```bash
scripts/debug-client state   # note sensorCount, selectedId, hiddenCount (expect 0)
scripts/debug-client eval '{"js": "window.Airloom.debugState().selectedId"}'
# Hide the selected sensor exactly as the button handler would:
scripts/debug-client eval '{"js": "(() => { const id = window.Airloom.debugState().selectedId; document.querySelector(\"#detail-card\").hidden = false; document.querySelector(\"#hide-button\").click(); return id; })()"}'
sleep 1 && scripts/debug-client state
```

Expected: `sensorCount` down by 1, `hiddenCount` = 1, `selectedId` changed to a different (visible) sensor, `popupHidden` true.

- [ ] **Step 3: Verify Settings lists it and unhide restores instantly**

```bash
# No payload on purpose: openSettings(config = state.config) defaults only on
# undefined, so passing null would crash it.
scripts/debug-client eval '{"js": "(() => { window.Airloom.receive(\"open-settings\"); return document.querySelector(\"#hidden-list\").textContent; })()"}'
```

Expected: the hidden sensor's name appears (not "No hidden sensors.").

```bash
scripts/debug-client eval '{"js": "(() => { document.querySelector(\"#hidden-list .unhide-button\").click(); return true; })()"}'
sleep 1
scripts/debug-client eval '{"js": "document.querySelector(\"#hidden-list\").textContent"}'
scripts/debug-client state
```

Expected: list shows "No hidden sensors.", `sensorCount` back to original, `hiddenCount` 0 — with no network refetch (instant).

- [ ] **Step 4: Verify favorite survives hide/unhide**

Favorite the selected sensor, hide it, then unhide-all — all over the bridge — and confirm it returns starred. Capture the id printed by the first command and substitute it as `$ID` below:

```bash
scripts/debug-client eval '{"js": "(() => { const id = window.Airloom.debugState().selectedId; window.webkit.messageHandlers.airloom.postMessage(JSON.stringify({action: \"favorite\", id})); return id; })()"}'
sleep 1
ID=<id printed above>
scripts/debug-client eval "{\"js\": \"window.webkit.messageHandlers.airloom.postMessage(JSON.stringify({action: 'hide', id: ${ID}}))\"}"
sleep 1
scripts/debug-client eval "{\"js\": \"[...document.querySelectorAll('.sensor-row')].some(r => Number(r.dataset.id) === ${ID})\"}"   # expect false — hidden
scripts/debug-client eval '{"js": "window.webkit.messageHandlers.airloom.postMessage(JSON.stringify({action: \"unhide-all\"}))"}'
sleep 1
scripts/debug-client eval "{\"js\": \"(() => { const row = [...document.querySelectorAll('.sensor-row')].find(r => Number(r.dataset.id) === ${ID}); return row ? row.innerHTML.includes('row-star') : 'MISSING'; })()\"}"
```

Expected: final command returns `true` — the sensor is back in the list with its star intact. (The sensors panel must be open for `.sensor-row` nodes to exist: `scripts/debug-client eval '{"js": "document.querySelector(\"#sensors-panel\").hidden = false"}'` first if needed.)

- [ ] **Step 5: Screenshot for the PR and cleanup**

```bash
scripts/debug-client screenshot '{"path": "/tmp/claude-1000/-home-ian-Source-airloom/b7e00ed6-cda0-41ad-8cc9-0c751e121e74/scratchpad/hidden-sensors.png"}'
scripts/debug-client quit
```

- [ ] **Step 6: Final gate**

Run: `make check`
Expected: clean. Only then is the branch PR-ready.
