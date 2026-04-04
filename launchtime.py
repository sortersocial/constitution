from decimal import Decimal
from datetime import datetime, timezone

from skyfield import almanac
from skyfield.api import N, E, wgs84, load

gps_north = Decimal("41.902270")
gps_east = Decimal("12.453365")

ts = load.timescale()
eph = load("de440s.bsp")

location = wgs84.latlon(float(gps_north) * N, float(gps_east) * E)
observer = eph["Earth"] + location

t0 = ts.utc(2026, 4, 5)
t1 = ts.utc(2026, 4, 6)

times, events = almanac.find_risings(observer, eph["Sun"], t0, t1)

for t, event in zip(times, events):
    if not event:
        continue
    dt = t.utc_datetime()
    unix_ms = int(dt.timestamp() * 1000)
    iso = dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond // 1000:03d}Z"
    print(f"Sunrise on 2026-04-05 at ({gps_north}°N, {gps_east}°E)")
    print(f"  UTC:      {iso}")
    print(f"  Unix ms:  {unix_ms}")
    print(f"  Julian Date: {t.tt:.10f} TT")
    break