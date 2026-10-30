"""Authoritative, GTK-free refresh targeting and completion decisions."""

from __future__ import annotations

from dataclasses import dataclass

from .geography import Bounds, bounds_around
from .presentation import visible_sensors
from .requests import RequestRegistry, RequestToken


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


class ApplicationController:
    """Own refresh intent while adapters own fetching and cache writes."""

    def __init__(self, config: dict):
        self._config = dict(config)
        self._view: tuple[Bounds, tuple[float, float]] | None = None
        self._generation = 0
        self._requests = RequestRegistry()

    def set_view(self, view: dict) -> None:
        bounds = Bounds(
            float(view["north"]),
            float(view["west"]),
            float(view["south"]),
            float(view["east"]),
        )
        center = (float(view["lat"]), float(view["lon"]))
        self._view = (bounds, center)

    def set_config(self, config: dict) -> None:
        self._config = dict(config)
        self._generation += 1

    def begin_refresh(self, force=False) -> RefreshRequest:
        if self._view is None:
            center = (
                float(self._config["latitude"]),
                float(self._config["longitude"]),
            )
            bounds = bounds_around(
                *center,
                float(self._config["radius_km"]),
            )
        else:
            bounds, center = self._view

        token = self._requests.begin("refresh", self._generation)
        return RefreshRequest(
            request_id=token.request_id,
            generation=token.generation,
            bounds=bounds,
            center=center,
            force=bool(force),
            favorite_ids=tuple(
                int(sensor_id)
                for sensor_id in self._config.get("favorites", ())
            ),
        )

    def complete_refresh(
        self,
        request: RefreshRequest,
        sensors,
        origin,
        delivery,
    ) -> RefreshDecision:
        token = RequestToken(request.request_id, "refresh", request.generation)
        if (
            request.generation != self._generation
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
            hidden_ids={
                int(sensor_id)
                for sensor_id in self._config.get("hidden", {})
            },
            confidence_filter=bool(self._config.get("confidence_filter", True)),
            location_filter=self._config.get("location_filter", "outdoor"),
        )
        trend_ids = (published[0].sensor_id,) if published else ()
        return RefreshDecision(True, tuple(published), trend_ids)
