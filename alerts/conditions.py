"""
Everything the alerts need to know about ONE user's place, read from the airquality and weather
apps. Weather data is optional: if the weather app is not installed, forecast and smoke alerts
simply never fire. Only fresh data is used, so stale readings never trigger an alert.
"""
import math
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.db.models import Count, Max, Sum
from django.utils import timezone


def _cfg(name, default):
    return getattr(settings, name, default)


def _model(app_label, name):
    try:
        return apps.get_model(app_label, name)
    except LookupError:
        return None


class Conditions:
    def __init__(self, profile, now=None, cache=None):
        self.profile = profile
        self.now = now or timezone.now()
        self.pt = Point(profile.lon, profile.lat, srid=4326)
        self._cache = cache if cache is not None else {}

    def _memo(self, key, loader):
        full = (key, round(self.profile.lat, 2), round(self.profile.lon, 2))
        if full not in self._cache:
            self._cache[full] = loader()
        return self._cache[full]

    # -- current AQI: CPCB station within 10 km, else satellite-model grid within 25 km ----------
    def aqi(self):
        return self._memo("aqi", self._load_aqi)

    def _load_aqi(self):
        Location = _model("airquality", "Location")
        GroundReading = _model("airquality", "GroundReading")
        Grid = _model("airquality", "AQIGridPoint")
        if Location is None or GroundReading is None:
            return None
        oldest = self.now - timedelta(hours=_cfg("ALERTS_MAX_DATA_AGE_HOURS", 6))
        stations = (Location.objects.filter(is_cpcb_station=True, point__dwithin=(self.pt, D(km=10)))
                    .annotate(distance=Distance("point", self.pt)).order_by("distance")[:3])
        for st in stations:
            r = (GroundReading.objects.filter(location=st, aqi__isnull=False, timestamp__gte=oldest)
                 .order_by("-timestamp").first())
            if r:
                return {"value": float(r.aqi), "source": "cpcb_station", "place": st.name,
                        "distance_km": round(st.distance.m / 1000, 1), "timestamp": r.timestamp}
        if Grid is not None:
            near = Grid.objects.filter(point__dwithin=(self.pt, D(km=25)),
                                       timestamp__gte=self.now - timedelta(hours=_cfg("ALERTS_GRID_MAX_AGE_HOURS", 12)))
            latest = near.order_by("-timestamp").values_list("timestamp", flat=True).first()
            if latest:
                g = (near.filter(timestamp=latest).annotate(distance=Distance("point", self.pt))
                     .order_by("distance").first())
                return {"value": float(g.aqi), "source": "satellite_model", "place": "",
                        "distance_km": round(g.distance.m / 1000, 1), "timestamp": g.timestamp}
        return None

    # -- AQI forecast: newest model run of the nearest location (persistence demo rows ignored) ----
    def forecast(self):
        return self._memo("forecast", self._load_forecast)

    def _load_forecast(self):
        Forecast = _model("weather", "AirQualityForecast")
        Location = _model("airquality", "Location")
        if Forecast is None or Location is None:
            return []
        qs = Forecast.objects.filter(
            forecast_for__gt=self.now, forecast_for__lte=self.now + timedelta(hours=72),
            run_at__gte=self.now - timedelta(hours=_cfg("ALERTS_FORECAST_MAX_AGE_HOURS", 24)))
        if not _cfg("ALERTS_ALLOW_BASELINE_FORECAST", False):
            qs = qs.exclude(model_version="persistence")
        ids = qs.values_list("location_id", flat=True).distinct()
        loc = (Location.objects.filter(id__in=ids, point__dwithin=(self.pt, D(km=50)))
               .annotate(distance=Distance("point", self.pt)).order_by("distance").first())
        if loc is None:
            return []
        rows = qs.filter(location=loc)
        newest = rows.aggregate(m=Max("run_at"))["m"]
        out = []
        for r in rows.filter(run_at=newest).order_by("forecast_for"):
            hours = max(1, math.ceil((r.forecast_for - self.now).total_seconds() / 3600))
            out.append({"forecast_for": r.forecast_for, "hours": hours, "prediction": float(r.prediction),
                        "place": loc.name, "model_version": r.model_version})
        return out

    # -- stubble smoke predicted within radius_km ---------------------------------------------------
    def smoke(self, radius_km):
        return self._memo(("smoke", radius_km), lambda: self._load_smoke(radius_km))

    def _load_smoke(self, radius_km):
        Drift = _model("weather", "SmokeDriftPrediction")
        if Drift is None:
            return None
        qs = Drift.objects.filter(
            point__dwithin=(self.pt, D(km=radius_km)), predicted_for__gte=self.now,
            generated_at__gte=self.now - timedelta(hours=_cfg("ALERTS_SMOKE_MAX_AGE_HOURS", 12)))
        first = qs.order_by("predicted_for").values_list("predicted_for", flat=True).first()
        if first is None:
            return None
        stats = qs.filter(predicted_for=first).aggregate(plumes=Count("id"), frp=Sum("hotspot__frp"))
        hours = max(1, round((first - self.now).total_seconds() / 3600))
        return {"hours": hours, "plumes": stats["plumes"], "total_frp": stats["frp"] or 0.0}

    # -- dispersion: ventilation index (wind speed x boundary-layer height) -------------------------
    def dispersion(self):
        return self._memo("dispersion", self._load_dispersion)

    def _load_dispersion(self):
        Reading = _model("weather", "WeatherReading")
        Location = _model("airquality", "Location")
        if Reading is None or Location is None:
            return None
        ids = (Reading.objects.filter(timestamp__gte=self.now - timedelta(hours=3), timestamp__lte=self.now + timedelta(hours=1))
               .values_list("location_id", flat=True).distinct())
        loc = (Location.objects.filter(id__in=ids, point__dwithin=(self.pt, D(km=100)))
               .annotate(distance=Distance("point", self.pt)).order_by("distance").first())
        if loc is None:
            return None
        r = Reading.objects.filter(location=loc, timestamp__lte=self.now).order_by("-timestamp").first()
        if r is None or r.wind_speed_ms is None or r.boundary_layer_m is None:
            return None
        vi = r.wind_speed_ms * r.boundary_layer_m
        return {"ventilation_index": round(vi, 1), "poor": vi < _cfg("ALERTS_VENTILATION_POOR_M2S", 6000)}
