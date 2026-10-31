"""Authoritative, GTK-free application state and decisions.

The GTK adapter (``app.py``) owns threads, GLib delivery, GTK/WebKit, and
notifications. It reports events here and acts on the returned decisions.
Configuration reads, persistence, fetch launching, and clocks are injected,
so refresh ordering, preference transactions, alerts, and shutdown are
exercised with stdlib ``unittest`` alone. Network and database I/O stay
outside: workers fetch, and the adapter reads/writes the trend cache.
"""

from __future__ import annotations

import math
import time
from copy import deepcopy
from dataclasses import dataclass

from .geography import Bounds, bounds_around, bounds_contains
from .models import Sensor
from .presentation import alert_transitions, visible_sensors
from .requests import RequestRegistry, RequestToken

LOCATION_FILTERS = ("outdoor", "indoor", "both")
INTERFACE_SCALES = (1.0, 1.25, 1.5)
REFRESH_MINUTES = (2, 5, 10, 30)


@dataclass(frozen=True)
class RefreshRequest:
    request_id: int
    generation: int
    bounds: Bounds
    center: tuple[float, float]
    force: bool
    favorite_ids: tuple[int, ...]


@dataclass(frozen=True)
class RefreshDecision:
    publish: bool
    sensors: tuple
    trend_ids: tuple[int, ...]
    # Error/degradation notices to show alongside an authoritative publication.
    messages: tuple[str, ...] = ()


@dataclass(slots=True)
class RefreshOutcome:
    """Worker-owned readings and transport metadata for one refresh request."""

    request: RefreshRequest
    sensors: list[Sensor]
    area_ids: set[int]
    monitoring_ids: set[int]
    source: str
    origin: str
    delivery: str
    error: str | None
    monitoring_error: str | None
    stale: bool
    bounds: Bounds
    capped: bool
    provenance: dict[int, tuple[str, str]]
    cache_warning: str | None = None


@dataclass(frozen=True)
class ChangeResult:
    """A preference transaction: committed, or rolled back with an error."""

    ok: bool
    error: str | None = None
    # The committed change needs a refresh of the current target.
    refresh: bool = False


@dataclass(frozen=True)
class SettingsResult:
    status: str  # "success", "error" (persistence), or "invalid" (input)
    error: str | None = None
    entered_auto: bool = False
    fixed_home: bool = False
    home_pinned: bool = False


@dataclass(frozen=True)
class AlertDecision:
    notify: tuple[Sensor, ...]
    error: str | None = None


@dataclass(frozen=True)
class TrendPlan:
    # "idle" (no such sensor), "local" (demo/no key: publish sensor.trend),
    # or "remote" (consult the trend cache, then fetch).
    kind: str
    sensor_id: int
    token: RequestToken
    reading_last_seen: int | None
    status: str | None = None
    trend: list | None = None


@dataclass(frozen=True)
class TrendUpdate:
    status: str
    trend: list | None
    store: bool = False
    sensor_changed: bool = False


def view_from_message(message: dict) -> tuple[Bounds, tuple[float, float]]:
    view = Bounds(
        float(message["north"]),
        float(message["west"]),
        float(message["south"]),
        float(message["east"]),
    )
    center = (float(message["lat"]), float(message["lon"]))
    if not (
        all(math.isfinite(value) for value in center)
        and -90 <= center[0] <= 90
        and -180 <= center[1] <= 180
    ):
        raise ValueError("view center is outside its valid range")
    return view, center


def coverage_payload(requested: Bounds, fetched: Bounds) -> dict:
    return {
        "north": fetched.north,
        "west": fetched.west,
        "south": fetched.south,
        "east": fetched.east,
        "capped": requested != fetched,
    }


def sensor_payload(sensor: Sensor, *, remote_favorite: bool) -> dict:
    payload = sensor.to_dict()
    payload["remote_favorite"] = remote_favorite
    return payload


def _persistence_error(action: str, exc: Exception) -> str:
    return (
        f"Could not {action}: {exc}. Check disk space and file permissions, "
        "then try again."
    )


def _finite_number(value, field: str) -> float:
    """Accept a JSON number or a form's numeric string; never a boolean."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{field} must be a finite number.")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{field} must be a finite number.") from None
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number.")
    return number


def _whole_number(value, field: str) -> int:
    number = _finite_number(value, field)
    if number != int(number):
        raise ValueError(f"{field} must be a whole number.")
    return int(number)


def _real_bool(value, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be true or false.")
    return value


class ApplicationController:
    """Own refresh intent, collections, selection, preferences, and lifecycle.

    ``config`` is either a dict (held here) or a callable returning the
    committed configuration; ``persist(candidate)`` saves a candidate and
    returns the saved configuration or raises; ``start_fetch(request)``
    launches a worker and returns whether it started.
    """

    def __init__(
        self,
        config,
        *,
        persist=None,
        start_fetch=None,
        monotonic=None,
        wall_clock=None,
        requests: RequestRegistry | None = None,
    ):
        self._config_source = config if callable(config) else None
        self._config_value = None if callable(config) else dict(config)
        self._persist = persist
        self._start_fetch = start_fetch
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._requests = requests if requests is not None else RequestRegistry()
        self._generation = 0
        self._stopped = False
        # Refresh work: at most one active worker and one queued replacement.
        self.refreshing = False
        self.active_fetch: RefreshRequest | None = None
        self.pending_fetch: RefreshRequest | None = None
        # Where the user is looking, stamped as one (bounds, center) pair so a
        # refresh can never combine a stale half.
        self.current_view: tuple[Bounds, tuple[float, float]] | None = None
        # Fetched coverage that lets a return to a fresh view skip a fetch.
        self.view_bounds: Bounds | None = None
        self.view_fetched_at = 0.0
        self.coverage_generation: int | None = None
        # Published readings: one list, split into area and monitoring IDs.
        self.sensors: list[Sensor] = []
        self.area_ids: set[int] = set()
        self.monitoring_ids: set[int] = set()
        self.selected_id: int | None = None
        # Last real fetch outcome, so republishing without refetching (e.g. a
        # favorite toggle) never guesses a label contradicting the fetch.
        self.last_source: str | None = None
        self.last_origin = "live"
        self.last_stale = False
        self.coverage: dict | None = None
        self.trend_inflight: dict[int, tuple[RequestToken, int | None]] = {}

    # -- Configuration and lifecycle ----------------------------------------

    @property
    def config(self) -> dict:
        if self._config_source is not None:
            return self._config_source()
        return self._config_value

    @property
    def stopped(self) -> bool:
        return self._stopped

    def shutdown(self) -> None:
        """Reject new work and invalidate every outstanding callback."""
        self._stopped = True
        self.pending_fetch = None
        self.active_fetch = None
        self.refreshing = False
        self._requests.invalidate()
        self._generation += 1

    def set_config(self, config: dict) -> None:
        """Adopt externally committed configuration as a new generation."""
        if self._config_source is None:
            self._config_value = dict(config)
        self._generation += 1

    def refresh_seconds(self) -> int:
        return int(self.config.get("refresh_minutes", 2)) * 60

    def home_bounds(self) -> Bounds:
        config = self.config
        return bounds_around(
            float(config["latitude"]),
            float(config["longitude"]),
            float(config["radius_km"]),
        )

    def _now(self) -> float:
        return self._monotonic() if self._monotonic else time.monotonic()

    def _wall_now(self) -> float:
        return self._wall_clock() if self._wall_clock else time.time()

    def _save(self, candidate: dict) -> dict:
        saved = self._persist(candidate) if self._persist else deepcopy(candidate)
        if self._config_source is None:
            self._config_value = dict(saved)
        return saved

    def _commit(self, *, reissue: bool) -> None:
        """Start a configuration generation after a successful save.

        Display changes (favorites, hiding) keep active or queued refresh work
        authorized by replacing it under the committed configuration; the
        replacement inherits a manual Refresh's force intent from either.
        """
        self._generation += 1
        if reissue and (self.refreshing or self.pending_fetch is not None):
            force = any(
                request is not None and request.force
                for request in (self.active_fetch, self.pending_fetch)
            )
            self.pending_fetch = self.begin_refresh(force=force)

    # -- Refresh targeting and supersession ---------------------------------

    def set_view(self, view: dict) -> None:
        bounds = Bounds(
            float(view["north"]),
            float(view["west"]),
            float(view["south"]),
            float(view["east"]),
        )
        self.current_view = (bounds, (float(view["lat"]), float(view["lon"])))

    def begin_refresh(self, force=False) -> RefreshRequest:
        """Register refresh intent, superseding every earlier request."""
        if self.current_view is None:
            config = self.config
            center = (float(config["latitude"]), float(config["longitude"]))
            bounds = bounds_around(*center, float(config["radius_km"]))
        else:
            bounds, center = self.current_view

        token = self._requests.begin("refresh", self._generation)
        return RefreshRequest(
            request_id=token.request_id,
            generation=token.generation,
            bounds=bounds,
            center=center,
            force=bool(force),
            favorite_ids=tuple(
                int(sensor_id) for sensor_id in self.config.get("favorites", ())
            ),
        )

    def refresh(self, force=False) -> RefreshRequest | None:
        """Refresh the current view (home before one exists).

        Returns the request if a worker started now; queued or rejected work
        returns None.
        """
        if self._stopped:
            return None
        return self._admit(self.begin_refresh(force=force))

    def refresh_home(self, force=False) -> RefreshRequest | None:
        """Explicit recentering is separate from ordinary Refresh actions."""
        if self._stopped:
            return None
        config = self.config
        center = (float(config["latitude"]), float(config["longitude"]))
        self.current_view = (bounds_around(*center, float(config["radius_km"])), center)
        return self.refresh(force=force)

    def view_changed(self, message: dict) -> tuple[float, float] | None:
        """Adopt a new viewport; return its center when it needs fetching.

        New view intent is registered even when fresh cached coverage needs no
        worker: the token supersedes active and queued work for the old view.
        """
        if self._stopped:
            return None
        try:
            view, center = view_from_message(message)
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        self.current_view = (view, center)
        request = self.begin_refresh()
        self.pending_fetch = None
        fresh = (self._now() - self.view_fetched_at) < self.refresh_seconds()
        if (
            self.view_bounds is not None
            and self.coverage_generation == request.generation
            and fresh
            and bounds_contains(self.view_bounds, view)
        ):
            return None
        self._admit(request)
        return center

    def _admit(self, request: RefreshRequest) -> RefreshRequest | None:
        if self.refreshing:
            # Only one worker runs; the newer registered identity already
            # supersedes the active completion.
            self.pending_fetch = request
            return None
        return self._launch(request)

    def _launch(self, request: RefreshRequest) -> RefreshRequest | None:
        if self._start_fetch is None or not self._start_fetch(request):
            return None
        self.refreshing = True
        self.active_fetch = request
        return request

    def settle_refresh(self) -> RefreshRequest | None:
        """Finalize the active worker, then launch any queued replacement."""
        self.refreshing = False
        self.active_fetch = None
        if self._stopped or self.pending_fetch is None:
            return None
        pending, self.pending_fetch = self.pending_fetch, None
        return self._launch(pending)

    # -- Completion authority and publication -------------------------------

    def complete_refresh(
        self,
        request: RefreshRequest,
        sensors,
        origin,
        delivery,
    ) -> RefreshDecision:
        token = RequestToken(request.request_id, "refresh", request.generation)
        if (
            self._stopped
            or request.generation != self._generation
            or not self._requests.is_current(token)
        ):
            return RefreshDecision(False, (), ())

        # A completion is consumed even if it is delivered more than once.
        self._requests.finish(token)
        favorite_ids = set(request.favorite_ids)
        for sensor in sensors:
            sensor.favorite = sensor.sensor_id in favorite_ids
            sensor.origin = origin
            sensor.delivery = delivery

        published = visible_sensors(
            list(sensors),
            hidden_ids=self._hidden_ids(),
            confidence_filter=bool(self.config.get("confidence_filter", True)),
            location_filter=self.config.get("location_filter", "outdoor"),
        )
        trend_ids = (published[0].sensor_id,) if published else ()
        return RefreshDecision(True, tuple(published), trend_ids)

    def accept_refresh(self, outcome: RefreshOutcome) -> RefreshDecision:
        """Adopt an authoritative worker outcome as the published state."""
        decision = self.complete_refresh(
            outcome.request, outcome.sensors, outcome.origin, outcome.delivery
        )
        if not decision.publish:
            return decision
        for sensor in outcome.sensors:
            sensor.origin, sensor.delivery = outcome.provenance.get(
                sensor.sensor_id, (outcome.origin, outcome.delivery)
            )
        self.sensors = outcome.sensors
        self.area_ids = set(outcome.area_ids)
        self.monitoring_ids = set(outcome.monitoring_ids)
        self.last_source = outcome.source
        self.last_origin = outcome.origin
        self.last_stale = outcome.stale
        self.coverage = coverage_payload(outcome.request.bounds, outcome.bounds)
        if outcome.error is None:
            # Failed live retrieval must not suppress the next retry.
            self.view_bounds = outcome.bounds
            self.view_fetched_at = self._now()
            self.coverage_generation = outcome.request.generation
        visible = self._reconcile_selection()
        messages = tuple(
            message
            for message in (outcome.error, outcome.monitoring_error, outcome.cache_warning)
            if message
        )
        trend_ids = (self.selected_id,) if self.selected_id is not None else ()
        return RefreshDecision(True, tuple(visible), trend_ids, messages)

    def wants_cached_startup(self) -> bool:
        return not self._stopped and not self.sensors and bool(self.config.get("api_key"))

    def apply_cached_startup(self, sensors: list[Sensor], bounds: Bounds) -> bool:
        """First paint from cached rows; the following refresh replaces it."""
        if not sensors or not self.wants_cached_startup():
            return False
        favorites = set(self.config.get("favorites", []))
        for sensor in sensors:
            sensor.favorite = sensor.sensor_id in favorites
            sensor.origin = "live"
            sensor.delivery = "cache"
        self.sensors = sensors
        self.area_ids = {sensor.sensor_id for sensor in sensors}
        self.monitoring_ids = set()
        self.last_origin = "live"
        self.last_stale = False
        self.coverage = coverage_payload(bounds, bounds)
        return True

    def select(self, sensor_id: int) -> None:
        self.selected_id = sensor_id

    def _hidden_ids(self) -> set[int]:
        return {int(sensor_id) for sensor_id in self.config.get("hidden", {})}

    def _sensor(self, sensor_id: int) -> Sensor | None:
        return next((s for s in self.sensors if s.sensor_id == sensor_id), None)

    def _collections(self) -> tuple[list[Sensor], list[Sensor], list[Sensor]]:
        """Apply display rules once, keeping area and favorite inputs distinct."""
        config = self.config
        area = [sensor for sensor in self.sensors if sensor.sensor_id in self.area_ids]
        remote = [
            sensor
            for sensor in self.sensors
            if sensor.sensor_id in self.monitoring_ids
            and sensor.sensor_id not in self.area_ids
            and sensor.favorite
        ]
        visible = visible_sensors(
            [*area, *remote],
            hidden_ids=self._hidden_ids(),
            confidence_filter=bool(config.get("confidence_filter", True)),
            location_filter=config.get("location_filter", "outdoor"),
        )
        visible_ids = {sensor.sensor_id for sensor in visible}
        visible_area = [sensor for sensor in area if sensor.sensor_id in visible_ids]
        favorites = [sensor for sensor in visible if sensor.favorite]
        return visible, visible_area, favorites

    def _reconcile_selection(self) -> list[Sensor]:
        visible = self._collections()[0]
        # Hiding or filtering the selected sensor must not leave a dangling ID.
        if self.selected_id not in {sensor.sensor_id for sensor in visible}:
            self.selected_id = visible[0].sensor_id if visible else None
        return visible

    def publication(self, source: str | None = None) -> dict:
        """The ``sensors`` event payload, minus the public configuration."""
        visible, area, favorites = self._collections()
        self._reconcile_selection()
        if self.coverage is None:
            bounds = (
                self.current_view[0]
                if self.current_view is not None
                else self.home_bounds()
            )
            self.coverage = coverage_payload(bounds, bounds)
        return {
            "items": [
                sensor_payload(sensor, remote_favorite=sensor.sensor_id not in self.area_ids)
                for sensor in visible
            ],
            "area": [sensor_payload(sensor, remote_favorite=False) for sensor in area],
            "favorites": [
                sensor_payload(sensor, remote_favorite=sensor.sensor_id not in self.area_ids)
                for sensor in favorites
            ],
            "selected_id": self.selected_id,
            "source": source
            or self.last_source
            or ("PurpleAir live" if self.config.get("api_key") else "Demo data"),
            "stale": bool(self.last_stale),
            "coverage": dict(self.coverage),
        }

    # -- Alerts -------------------------------------------------------------

    def evaluate_alerts(self) -> AlertDecision:
        """Decide eligible favorite alerts and persist the next alert state."""
        if self._stopped:
            return AlertDecision(())
        config = self.config
        try:
            threshold = int(config.get("alert_threshold", 101))
        except (TypeError, ValueError):
            threshold = 101
        previous = dict(config.get("alert_states", {}))
        alerted, states = alert_transitions(
            self._collections()[0],
            favorites={int(sensor_id) for sensor_id in config.get("favorites", [])},
            previous=previous,
            threshold=threshold,
            now=self._wall_now(),
            origin=self.last_origin,
        )
        error = None
        if states != previous:
            candidate = deepcopy(config)
            candidate["alert_states"] = states
            try:
                self._save(candidate)
            except Exception as exc:  # noqa: BLE001 — persistence is nonfatal here
                error = f"Could not save alert state: {exc}"
        return AlertDecision(tuple(alerted), error)

    # -- Trends -------------------------------------------------------------

    def begin_trend(self, sensor_id: int) -> TrendPlan | None:
        """Decide trend work for a sensor reading; None reuses in-flight work.

        A newer reading gets a new token, so neither an old request nor an old
        observation can publish.
        """
        if self._stopped:
            return None
        sensor = self._sensor(sensor_id)
        reading_last_seen = sensor.last_seen if sensor is not None else None
        inflight = self.trend_inflight.get(sensor_id)
        if (
            sensor is not None
            and inflight is not None
            and inflight[1] == reading_last_seen
            and self._requests.is_current(inflight[0])
        ):
            return None

        token = self._requests.begin(f"trend:{sensor_id}", generation=reading_last_seen or 0)
        if sensor is None:
            self._requests.finish(token)
            return TrendPlan("idle", sensor_id, token, None, "idle", [])
        # Provenance, not key presence, decides: demo fallback after an outage
        # keeps the key but its synthetic IDs must never reach PurpleAir.
        if sensor.origin == "demo" or not self.config.get("api_key"):
            self._requests.finish(token)
            status = "loaded" if sensor.trend else "unavailable"
            return TrendPlan("local", sensor_id, token, reading_last_seen, status, sensor.trend)
        return TrendPlan("remote", sensor_id, token, reading_last_seen)

    def cached_trend(self, plan: TrendPlan, cached: list) -> TrendUpdate:
        sensor = self._sensor(plan.sensor_id)
        changed = bool(cached) and sensor is not None and sensor.trend != cached
        if changed:
            sensor.trend = cached
        self._requests.finish(plan.token)
        return TrendUpdate("loaded" if cached else "unavailable", cached, sensor_changed=changed)

    def track_trend(self, plan: TrendPlan) -> None:
        self.trend_inflight[plan.sensor_id] = (plan.token, plan.reading_last_seen)

    def trend_wanted(self, plan: TrendPlan) -> bool:
        """Recheck before paid network I/O; callable from the trend worker."""
        sensor = self._sensor(plan.sensor_id)
        return (
            self._requests.is_current(plan.token)
            and sensor is not None
            and sensor.last_seen == plan.reading_last_seen
        )

    def finish_trend(
        self,
        sensor_id: int,
        reading_last_seen: int | None,
        token: RequestToken,
        trend: list | None,
        failed: bool,
    ) -> TrendUpdate | None:
        inflight = self.trend_inflight.get(sensor_id)
        sensor = self._sensor(sensor_id)
        owns_inflight = inflight is not None and inflight[0] == token
        authorized = (
            not self._stopped
            and owns_inflight
            and self._requests.is_current(token)
            and sensor is not None
            and sensor.last_seen == reading_last_seen
        )
        if owns_inflight:
            # This callback settles the stored request even when its reading is
            # no longer publishable; a later selection must never reuse it.
            del self.trend_inflight[sensor_id]
            self._requests.finish(token)
        if not authorized:
            return None
        status = "error" if failed else "loaded" if trend else "unavailable"
        if status == "loaded":
            sensor.trend = trend
        return TrendUpdate(status, trend, store=status == "loaded", sensor_changed=status == "loaded")

    # -- Preference transactions --------------------------------------------
    # Each builds a candidate from committed configuration, persists it, and
    # commits only after success; a failure leaves memory and disk unchanged.

    def toggle_favorite(self, sensor_id: int) -> ChangeResult | None:
        if self._stopped or self._sensor(sensor_id) is None:
            return None
        candidate = deepcopy(self.config)
        favorites = set(candidate.get("favorites", []))
        enabled = sensor_id not in favorites
        if enabled:
            favorites.add(sensor_id)
        else:
            favorites.remove(sensor_id)
        candidate["favorites"] = sorted(favorites)
        try:
            self._save(candidate)
        except OSError as exc:
            return ChangeResult(False, _persistence_error("save favorite", exc))
        self._commit(reissue=True)
        for sensor in self.sensors:
            if sensor.sensor_id == sensor_id:
                sensor.favorite = enabled
        return ChangeResult(True)

    def toggle_hidden(self, sensor_id: int) -> ChangeResult | None:
        if self._stopped:
            return None
        if str(sensor_id) in self.config.get("hidden", {}):
            return self.unhide(sensor_id)
        sensor = self._sensor(sensor_id)
        if sensor is None:
            return None
        candidate = deepcopy(self.config)
        candidate.setdefault("hidden", {})[str(sensor_id)] = str(sensor.name).strip()[:80]
        try:
            self._save(candidate)
        except OSError as exc:
            return ChangeResult(False, _persistence_error("hide sensor", exc))
        self._commit(reissue=True)
        return ChangeResult(True)

    def unhide(self, sensor_id: int) -> ChangeResult | None:
        if self._stopped:
            return None
        candidate = deepcopy(self.config)
        if candidate.get("hidden", {}).pop(str(sensor_id), None) is not None:
            try:
                self._save(candidate)
            except OSError as exc:
                return ChangeResult(False, _persistence_error("unhide sensor", exc))
        self._commit(reissue=True)
        return ChangeResult(True)

    def unhide_all(self) -> ChangeResult | None:
        if self._stopped:
            return None
        candidate = deepcopy(self.config)
        if candidate.get("hidden"):
            candidate["hidden"] = {}
            try:
                self._save(candidate)
            except OSError as exc:
                return ChangeResult(False, _persistence_error("unhide all sensors", exc))
        self._commit(reissue=True)
        return ChangeResult(True)

    def set_location_filter(self, value: str) -> ChangeResult | None:
        if self._stopped or value not in LOCATION_FILTERS:
            return None
        candidate = deepcopy(self.config)
        candidate["location_filter"] = value
        try:
            self._save(candidate)
        except OSError as exc:
            return ChangeResult(False, _persistence_error("save location filter", exc))
        self._commit(reissue=False)
        return ChangeResult(True, refresh=True)

    def set_detected_home(self, latitude: float, longitude: float) -> ChangeResult | None:
        # A delayed fix from a locator started under auto mode must not
        # clobber coordinates the user has since pinned in fixed mode.
        if self._stopped or self.config.get("home_mode") != "auto":
            return None
        candidate = deepcopy(self.config)
        candidate.update({"latitude": float(latitude), "longitude": float(longitude)})
        try:
            self._save(candidate)
        except OSError as exc:
            return ChangeResult(False, _persistence_error("save detected location", exc))
        self._commit(reissue=False)
        # The adapter recenters on the new home rather than refreshing the view.
        return ChangeResult(True)

    def set_home_label(self, name: str, latitude: float, longitude: float) -> ChangeResult | None:
        """Only the label for the current automatic home may persist."""
        config = self.config
        if (
            self._stopped
            or config.get("home_mode") != "auto"
            or (config.get("latitude"), config.get("longitude")) != (latitude, longitude)
        ):
            return None
        candidate = deepcopy(config)
        candidate["location_name"] = name[:80]
        try:
            self._save(candidate)
        except OSError as exc:
            return ChangeResult(False, _persistence_error("save detected location name", exc))
        return ChangeResult(True)

    def save_settings(self, message: dict) -> SettingsResult | None:
        if self._stopped:
            return None
        config = self.config
        previous_mode = config.get("home_mode")
        previous_home = (config.get("latitude"), config.get("longitude"))
        try:
            updates = self._settings_updates(message, config)
            clear_api_key = (
                _real_bool(message["clear_api_key"], "clear_api_key")
                if "clear_api_key" in message
                else False
            )
            api_key = message.get("api_key")
            if api_key is not None and not isinstance(api_key, str):
                raise ValueError("api_key must be text or null.")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            return SettingsResult("invalid", f"Could not save preferences: {exc}")

        candidate = deepcopy(config)
        candidate.update(updates)
        api_key = (api_key or "").strip()
        if clear_api_key:
            candidate["api_key"] = ""
        elif api_key:
            candidate["api_key"] = api_key
        try:
            self._save(candidate)
        except OSError as exc:
            return SettingsResult("error", _persistence_error("save preferences", exc))
        self._commit(reissue=False)
        home_mode = updates["home_mode"]
        return SettingsResult(
            "success",
            entered_auto=home_mode == "auto" and previous_mode != "auto",
            fixed_home=home_mode != "auto",
            home_pinned=home_mode == "fixed"
            and (
                previous_mode != "fixed"
                or (updates["latitude"], updates["longitude"]) != previous_home
            ),
        )

    @staticmethod
    def _settings_updates(message: dict, config: dict) -> dict:
        home_mode = "fixed" if message.get("home_mode") == "fixed" else "auto"
        radius = _finite_number(message["radius_km"], "radius_km")
        heatmap = _finite_number(message["heatmap_threshold_km"], "heatmap_threshold_km")
        alert = _whole_number(message["alert_threshold"], "alert_threshold")
        updates = {
            "radius_km": max(2.0, min(100.0, radius)),
            "heatmap_threshold_km": max(5.0, min(1000.0, heatmap)),
            "alert_threshold": max(1, min(500, alert)),
            "home_mode": home_mode,
            "temperature_unit": "C" if message.get("temperature_unit") == "C" else "F",
            "confidence_filter": (
                _real_bool(message["confidence_filter"], "confidence_filter")
                if "confidence_filter" in message
                else config.get("confidence_filter")
            ),
            "interface_scale": config.get("interface_scale", 1.0),
        }
        if "interface_scale" in message:
            # The selector's 100% arrives as JSON integer 1.
            scale = message["interface_scale"]
            if isinstance(scale, bool) or float(scale) not in INTERFACE_SCALES:
                raise ValueError("Interface size must be 100%, 125%, or 150%.")
            updates["interface_scale"] = float(scale)
        minutes = _whole_number(message["refresh_minutes"], "refresh_minutes")
        updates["refresh_minutes"] = minutes if minutes in REFRESH_MINUTES else 2
        if home_mode == "fixed":
            latitude = _finite_number(message["home_lat"], "home_lat")
            longitude = _finite_number(message["home_lon"], "home_lon")
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError("Coordinates are outside their valid range.")
            name = message.get("location_name")
            if name is not None and not isinstance(name, str):
                raise ValueError("location_name must be text or null.")
            updates["latitude"] = latitude
            updates["longitude"] = longitude
            updates["location_name"] = (name or "Custom location")[:80]
        return updates
