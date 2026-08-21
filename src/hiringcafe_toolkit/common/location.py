"""Geographic helpers.

Job records carry workplace cities as ``"College Park, Maryland, US"`` strings
alongside a parallel ``_geoloc`` list of coordinates. Pairing the two by index
is what makes per-city filtering possible: a posting that lists both an
excluded state and a nearby kept one should survive on the strength of the
kept city, and its distance should come from that city rather than whichever
one happened to be listed first.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

EARTH_RADIUS_METERS = 6_371_000.0
METERS_PER_MILE = 1609.344


@dataclass(frozen=True)
class Place:
    """One workplace location from a posting."""

    label: str
    """The city string as listed, e.g. ``"College Park, Maryland, US"``."""

    state: str | None
    """State or region name parsed from the label, if present."""

    latitude: float | None
    longitude: float | None
    distance_miles: float | None = None

    def with_distance(self, distance_miles: float | None) -> Place:
        return Place(
            label=self.label,
            state=self.state,
            latitude=self.latitude,
            longitude=self.longitude,
            distance_miles=distance_miles,
        )


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles."""
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    return (EARTH_RADIUS_METERS * 2 * math.asin(math.sqrt(a))) / METERS_PER_MILE


def parse_state(city_label: str) -> str | None:
    """Extract the state or region from a ``"City, State, Country"`` label.

    Labels are matched on the full state name rather than an abbreviation,
    because that is the form the records use; mapping "MD" to "Maryland" would
    add a table for no gain.
    """
    parts = [part.strip() for part in city_label.split(",") if part.strip()]
    if len(parts) < 3:
        return None
    return parts[-2]


def _coerce_coordinate(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def build_places(cities: Sequence[Any], geoloc: Sequence[Any]) -> list[Place]:
    """Pair city labels with their coordinates by position.

    The two lists are parallel in the source data. When they disagree in
    length, cities without a matching coordinate are still returned (with no
    coordinates) rather than dropped, so a malformed record degrades into an
    unknown distance instead of a silently missing location.
    """
    places: list[Place] = []
    for index, raw_city in enumerate(cities):
        if not isinstance(raw_city, str) or not raw_city.strip():
            continue
        label = raw_city.strip()
        latitude: float | None = None
        longitude: float | None = None
        if index < len(geoloc):
            point = geoloc[index]
            if isinstance(point, dict):
                latitude = _coerce_coordinate(point.get("lat"))
                longitude = _coerce_coordinate(point.get("lon") or point.get("lng"))
        places.append(
            Place(
                label=label,
                state=parse_state(label),
                latitude=latitude,
                longitude=longitude,
            )
        )
    return places


def select_nearby_places(
    places: Iterable[Place],
    *,
    home_latitude: float,
    home_longitude: float,
    radius_miles: float,
    excluded_states: frozenset[str],
) -> list[Place]:
    """Return in-radius places outside the excluded states, nearest first.

    Places without coordinates cannot be measured, so they are kept only if
    their state is not excluded, and sort last with an unknown distance.
    """
    kept: list[Place] = []
    for place in places:
        if place.state is not None and place.state.casefold() in excluded_states:
            continue
        if place.latitude is None or place.longitude is None:
            kept.append(place.with_distance(None))
            continue
        distance = haversine_miles(home_latitude, home_longitude, place.latitude, place.longitude)
        if distance <= radius_miles:
            kept.append(place.with_distance(distance))
    kept.sort(key=lambda p: (p.distance_miles is None, p.distance_miles or 0.0))
    return kept


def normalize_states(states: Iterable[str]) -> frozenset[str]:
    """Case-fold configured state names for comparison."""
    return frozenset(state.strip().casefold() for state in states if state.strip())
