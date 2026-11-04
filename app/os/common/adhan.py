"""Prayer times: a port of the JS `adhan` library, version 4.4.6 (the one vournalOS pins).

Only what vournalOS uses: `new PrayerTimes(coordinates, date, CalculationMethod[method]())`
with a madhab, the default high-latitude rule (middle of the night), no polar-circle
resolution, and each method's own rounding and minute adjustments.

Times are JavaScript instants: float milliseconds since the epoch, NaN for "no such
time" (the sun never sets or never rises). Arithmetic keeps JavaScript's order of
operations, so the answers match adhan's to the millisecond (tests/vournal_os/golden).
`adhan` reads the local calendar fields of the date it is given; here that day is a
`datetime.date`.
"""

import math
from datetime import date, timedelta

NAN = math.nan
_EPOCH = date(1970, 1, 1)


# ----- JavaScript number behaviour ----------------------------------------------------


def _floor(x: float) -> float:
    return x if not math.isfinite(x) else float(math.floor(x))


def _trunc(x: float) -> float:
    return x if not math.isfinite(x) else float(math.trunc(x))


def _js_round(x: float) -> float:
    """Math.round: halves go up (towards +∞)."""
    if not math.isfinite(x):
        return x
    r = math.floor(x)
    return float(r + 1 if x - r >= 0.5 else r)


def _asin(x: float) -> float:
    return math.asin(x) if -1 <= x <= 1 else NAN


def _acos(x: float) -> float:
    return math.acos(x) if -1 <= x <= 1 else NAN


def _div(a: float, b: float) -> float:
    if b == 0:
        return NAN if a == 0 or a != a else math.copysign(math.inf, a) * math.copysign(1, b)
    return a / b


def _sin(x: float) -> float:
    return math.sin(x) if math.isfinite(x) else NAN


def _cos(x: float) -> float:
    return math.cos(x) if math.isfinite(x) else NAN


def _tan(x: float) -> float:
    return math.tan(x) if math.isfinite(x) else NAN


# ----- MathUtils ---------------------------------------------------------------------


def degrees_to_radians(degrees: float) -> float:
    return degrees * math.pi / 180.0


def radians_to_degrees(radians: float) -> float:
    return radians * 180.0 / math.pi


def normalize_to_scale(num: float, maximum: float) -> float:
    return num - maximum * _floor(num / maximum)


def unwind_angle(angle: float) -> float:
    return normalize_to_scale(angle, 360.0)


def quadrant_shift_angle(angle: float) -> float:
    if -180 <= angle <= 180:
        return angle
    return angle - 360 * _js_round(angle / 360)


# ----- DateUtils (instants in ms) ------------------------------------------------------


def _utc_date(num: float, day: date) -> float:
    """new TimeComponents(num).utcDate(y, m, d): hours of the day → an instant."""
    hours = _floor(num)
    minutes = _floor((num - hours) * 60)
    seconds = _floor((num - (hours + minutes / 60)) * 60 * 60)
    if not math.isfinite(seconds):
        return NAN
    return (day - _EPOCH).days * 86_400_000 + (hours * 3_600_000 + minutes * 60_000 + seconds * 1000)


def _adding_seconds(ms: float, seconds: float) -> float:
    return _trunc(ms + seconds * 1000)  # new Date(number) drops the fraction


def _adding_minutes(ms: float, minutes: float) -> float:
    return _adding_seconds(ms, minutes * 60)


def _rounded_minute(ms: float, rounding: str) -> float:
    if ms != ms:
        return ms
    seconds = int(ms // 1000) % 60
    offset = 60 - seconds if seconds >= 30 else -1 * seconds
    if rounding == "up":
        offset = 60 - seconds
    elif rounding == "none":
        offset = 0
    return _adding_seconds(ms, offset)


def is_leap_year(year: int) -> bool:
    if year % 4 != 0:
        return False
    if year % 100 == 0 and year % 400 != 0:
        return False
    return True


def day_of_year(day: date) -> int:
    return day.timetuple().tm_yday


# ----- Astronomical ------------------------------------------------------------------


def mean_solar_longitude(T: float) -> float:
    term1 = 280.4664567
    term2 = 36000.76983 * T
    term3 = 0.0003032 * math.pow(T, 2)
    return unwind_angle(term1 + term2 + term3)


def mean_lunar_longitude(T: float) -> float:
    term1 = 218.3165
    term2 = 481267.8813 * T
    return unwind_angle(term1 + term2)


def ascending_lunar_node_longitude(T: float) -> float:
    term1 = 125.04452
    term2 = 1934.136261 * T
    term3 = 0.0020708 * math.pow(T, 2)
    term4 = math.pow(T, 3) / 450000
    return unwind_angle(term1 - term2 + term3 + term4)


def mean_solar_anomaly(T: float) -> float:
    term1 = 357.52911
    term2 = 35999.05029 * T
    term3 = 0.0001537 * math.pow(T, 2)
    return unwind_angle(term1 + term2 - term3)


def solar_equation_of_the_center(T: float, mean_anomaly: float) -> float:
    m = degrees_to_radians(mean_anomaly)
    term1 = (1.914602 - 0.004817 * T - 0.000014 * math.pow(T, 2)) * _sin(m)
    term2 = (0.019993 - 0.000101 * T) * _sin(2 * m)
    term3 = 0.000289 * _sin(3 * m)
    return term1 + term2 + term3


def apparent_solar_longitude(T: float, mean_longitude: float) -> float:
    longitude = mean_longitude + solar_equation_of_the_center(T, mean_solar_anomaly(T))
    omega = 125.04 - 1934.136 * T
    lam = longitude - 0.00569 - 0.00478 * _sin(degrees_to_radians(omega))
    return unwind_angle(lam)


def mean_obliquity_of_the_ecliptic(T: float) -> float:
    term1 = 23.439291
    term2 = 0.013004167 * T
    term3 = 0.0000001639 * math.pow(T, 2)
    term4 = 0.0000005036 * math.pow(T, 3)
    return term1 - term2 - term3 + term4


def apparent_obliquity_of_the_ecliptic(T: float, epsilon0: float) -> float:
    o = 125.04 - 1934.136 * T
    return epsilon0 + 0.00256 * _cos(degrees_to_radians(o))


def mean_sidereal_time(T: float) -> float:
    jd = T * 36525 + 2451545.0
    term1 = 280.46061837
    term2 = 360.98564736629 * (jd - 2451545)
    term3 = 0.000387933 * math.pow(T, 2)
    term4 = math.pow(T, 3) / 38710000
    return unwind_angle(term1 + term2 + term3 - term4)


def nutation_in_longitude(L0: float, Lp: float, omega: float) -> float:
    term1 = -17.2 / 3600 * _sin(degrees_to_radians(omega))
    term2 = 1.32 / 3600 * _sin(2 * degrees_to_radians(L0))
    term3 = 0.23 / 3600 * _sin(2 * degrees_to_radians(Lp))
    term4 = 0.21 / 3600 * _sin(2 * degrees_to_radians(omega))
    return term1 - term2 - term3 + term4


def nutation_in_obliquity(L0: float, Lp: float, omega: float) -> float:
    term1 = 9.2 / 3600 * _cos(degrees_to_radians(omega))
    term2 = 0.57 / 3600 * _cos(2 * degrees_to_radians(L0))
    term3 = 0.1 / 3600 * _cos(2 * degrees_to_radians(Lp))
    term4 = 0.09 / 3600 * _cos(2 * degrees_to_radians(omega))
    return term1 + term2 + term3 - term4


def altitude_of_celestial_body(phi: float, delta: float, H: float) -> float:
    term1 = _sin(degrees_to_radians(phi)) * _sin(degrees_to_radians(delta))
    term2 = _cos(degrees_to_radians(phi)) * _cos(degrees_to_radians(delta)) * _cos(degrees_to_radians(H))
    return radians_to_degrees(_asin(term1 + term2))


def approximate_transit(L: float, theta0: float, a2: float) -> float:
    lw = L * -1
    m0 = normalize_to_scale((a2 + lw - theta0) / 360, 1)
    # near the date line m0 can land on the wrong day: compare it with the transit the
    # longitude alone predicts, and move it by a day when they're more than half apart
    expected = normalize_to_scale((12.0 - L / 15.0) / 24.0, 1)
    if m0 - expected > 0.5:
        return m0 - 1.0
    if expected - m0 > 0.5:
        return m0 + 1.0
    return m0


def interpolate(y2: float, y1: float, y3: float, n: float) -> float:
    a = y2 - y1
    b = y3 - y2
    c = b - a
    return y2 + n / 2 * (a + b + n * c)


def interpolate_angles(y2: float, y1: float, y3: float, n: float) -> float:
    a = unwind_angle(y2 - y1)
    b = unwind_angle(y3 - y2)
    c = b - a
    return y2 + n / 2 * (a + b + n * c)


def corrected_transit(m0: float, L: float, theta0: float, a2: float, a1: float, a3: float) -> float:
    lw = L * -1
    theta = unwind_angle(theta0 + 360.985647 * m0)
    a = unwind_angle(interpolate_angles(a2, a1, a3, m0))
    H = quadrant_shift_angle(theta - lw - a)
    dm = H / -360
    return (m0 + dm) * 24


def corrected_hour_angle(m0, h0, lat, lng, after_transit, theta0, a2, a1, a3, d2, d1, d3) -> float:
    lw = lng * -1
    term1 = _sin(degrees_to_radians(h0)) - _sin(degrees_to_radians(lat)) * _sin(degrees_to_radians(d2))
    term2 = _cos(degrees_to_radians(lat)) * _cos(degrees_to_radians(d2))
    H0 = radians_to_degrees(_acos(_div(term1, term2)))
    m = m0 + H0 / 360 if after_transit else m0 - H0 / 360
    theta = unwind_angle(theta0 + 360.985647 * m)
    a = unwind_angle(interpolate_angles(a2, a1, a3, m))
    delta = interpolate(d2, d1, d3, m)
    H = theta - lw - a
    h = altitude_of_celestial_body(lat, delta, H)
    term3 = h - h0
    term4 = 360 * _cos(degrees_to_radians(delta)) * _cos(degrees_to_radians(lat)) * _sin(degrees_to_radians(H))
    dm = _div(term3, term4)
    return (m + dm) * 24


def julian_day(year: int, month: int, day: int, hours: float = 0) -> float:
    Y = math.trunc(year if month > 2 else year - 1)
    M = math.trunc(month if month > 2 else month + 12)
    D = day + hours / 24
    A = math.trunc(Y / 100)
    B = math.trunc(2 - A + math.trunc(A / 4))
    i0 = math.trunc(365.25 * (Y + 4716))
    i1 = math.trunc(30.6001 * (M + 1))
    return i0 + i1 + D + B - 1524.5


def julian_century(jd: float) -> float:
    return (jd - 2451545.0) / 36525


def _days_since_solstice(doy: int, year: int, latitude: float) -> int:
    northern_offset = 10
    southern_offset = 173 if is_leap_year(year) else 172
    days_in_year = 366 if is_leap_year(year) else 365
    if latitude >= 0:
        days = doy + northern_offset
        if days >= days_in_year:
            days = days - days_in_year
    else:
        days = doy - southern_offset
        if days < 0:
            days = days + days_in_year
    return days


def _seasonal(dyy: int, a: float, b: float, c: float, d: float) -> float:
    if dyy < 91:
        return a + (b - a) / 91.0 * dyy
    if dyy < 137:
        return b + (c - b) / 46.0 * (dyy - 91)
    if dyy < 183:
        return c + (d - c) / 46.0 * (dyy - 137)
    if dyy < 229:
        return d + (c - d) / 46.0 * (dyy - 183)
    if dyy < 275:
        return c + (b - c) / 46.0 * (dyy - 229)
    return b + (a - b) / 91.0 * (dyy - 275)


def season_adjusted_morning_twilight(latitude: float, doy: int, year: int, sunrise: float) -> float:
    lat = abs(latitude)
    a = 75 + 28.65 / 55.0 * lat
    b = 75 + 19.44 / 55.0 * lat
    c = 75 + 32.74 / 55.0 * lat
    d = 75 + 48.1 / 55.0 * lat
    adjustment = _seasonal(_days_since_solstice(doy, year, latitude), a, b, c, d)
    return _adding_seconds(sunrise, _js_round(adjustment * -60.0))


def season_adjusted_evening_twilight(latitude: float, doy: int, year: int, sunset: float, shafaq: str) -> float:
    lat = abs(latitude)
    if shafaq == "ahmer":
        a, b, c, d = 62 + 17.4 / 55.0 * lat, 62 - 7.16 / 55.0 * lat, 62 + 5.12 / 55.0 * lat, 62 + 19.44 / 55.0 * lat
    elif shafaq == "abyad":
        a, b, c, d = 75 + 25.6 / 55.0 * lat, 75 + 7.16 / 55.0 * lat, 75 + 36.84 / 55.0 * lat, 75 + 81.84 / 55.0 * lat
    else:
        a, b, c, d = 75 + 25.6 / 55.0 * lat, 75 + 2.05 / 55.0 * lat, 75 - 9.21 / 55.0 * lat, 75 + 6.14 / 55.0 * lat
    adjustment = _seasonal(_days_since_solstice(doy, year, latitude), a, b, c, d)
    return _adding_seconds(sunset, _js_round(adjustment * 60.0))


# ----- SolarCoordinates / SolarTime ----------------------------------------------------


class _SolarCoordinates:
    def __init__(self, jd: float):
        T = julian_century(jd)
        L0 = mean_solar_longitude(T)
        Lp = mean_lunar_longitude(T)
        omega = ascending_lunar_node_longitude(T)
        lam = degrees_to_radians(apparent_solar_longitude(T, L0))
        theta0 = mean_sidereal_time(T)
        d_psi = nutation_in_longitude(L0, Lp, omega)
        d_epsilon = nutation_in_obliquity(L0, Lp, omega)
        epsilon0 = mean_obliquity_of_the_ecliptic(T)
        epsilon_apparent = degrees_to_radians(apparent_obliquity_of_the_ecliptic(T, epsilon0))
        self.declination = radians_to_degrees(_asin(_sin(epsilon_apparent) * _sin(lam)))
        self.right_ascension = unwind_angle(
            radians_to_degrees(math.atan2(_cos(epsilon_apparent) * _sin(lam), _cos(lam)))
        )
        self.apparent_sidereal_time = theta0 + d_psi * 3600 * _cos(degrees_to_radians(epsilon0 + d_epsilon)) / 3600


class _SolarTime:
    def __init__(self, day: date, lat: float, lng: float):
        jd = julian_day(day.year, day.month, day.day, 0)
        self.lat, self.lng = lat, lng
        self.solar = _SolarCoordinates(jd)
        self.prev = _SolarCoordinates(jd - 1)
        self.next = _SolarCoordinates(jd + 1)
        s = self.solar
        m0 = approximate_transit(lng, s.apparent_sidereal_time, s.right_ascension)
        self.approx_transit = m0
        self.transit = corrected_transit(
            m0, lng, s.apparent_sidereal_time, s.right_ascension, self.prev.right_ascension, self.next.right_ascension
        )
        solar_altitude = -50.0 / 60.0
        self.sunrise = self.hour_angle(solar_altitude, False)
        self.sunset = self.hour_angle(solar_altitude, True)

    def hour_angle(self, angle: float, after_transit: bool) -> float:
        s, p, n = self.solar, self.prev, self.next
        return corrected_hour_angle(
            self.approx_transit, angle, self.lat, self.lng, after_transit, s.apparent_sidereal_time,
            s.right_ascension, p.right_ascension, n.right_ascension, s.declination, p.declination, n.declination,
        )

    def afternoon(self, shadow_length: int) -> float:
        tangent = abs(self.lat - self.solar.declination)
        inverse = shadow_length + _tan(degrees_to_radians(tangent))
        angle = radians_to_degrees(math.atan(_div(1.0, inverse)))
        return self.hour_angle(angle, True)


# ----- CalculationMethod --------------------------------------------------------------

_NO_ADJ = {"fajr": 0, "sunrise": 0, "dhuhr": 0, "asr": 0, "maghrib": 0, "isha": 0}

# name: (fajrAngle, ishaAngle, ishaInterval, maghribAngle, methodAdjustments, rounding)
METHODS: dict[str, tuple] = {
    "MuslimWorldLeague": (18, 17, 0, 0, {"dhuhr": 1}, "nearest"),
    "Egyptian": (19.5, 17.5, 0, 0, {"dhuhr": 1}, "nearest"),
    "Karachi": (18, 18, 0, 0, {"dhuhr": 1}, "nearest"),
    "UmmAlQura": (18.5, 0, 90, 0, {}, "nearest"),
    "Dubai": (18.2, 18.2, 0, 0, {"sunrise": -3, "dhuhr": 3, "asr": 3, "maghrib": 3}, "nearest"),
    "MoonsightingCommittee": (18, 18, 0, 0, {"dhuhr": 5, "maghrib": 3}, "nearest"),
    "NorthAmerica": (15, 15, 0, 0, {"dhuhr": 1}, "nearest"),
    "Kuwait": (18, 17.5, 0, 0, {}, "nearest"),
    "Qatar": (18, 0, 90, 0, {}, "nearest"),
    "Singapore": (20, 18, 0, 0, {"dhuhr": 1}, "up"),
    "Tehran": (17.7, 14, 0, 4.5, {}, "nearest"),
    "Turkey": (18, 17, 0, 0, {"sunrise": -7, "dhuhr": 5, "asr": 4, "maghrib": 7}, "nearest"),
}
_HIGH_LATITUDE_THRESHOLD = 55  # Moonsighting Committee's 1/7-of-the-night rule from here


def prayer_times(lat: float, lng: float, method: str, madhab: str, day: date) -> dict[str, float]:
    """new PrayerTimes(new Coordinates(lat, lng), day, params): the six times (and
    sunset), as instants in ms; NaN where adhan gives an invalid Date."""
    fajr_angle, isha_angle, isha_interval, maghrib_angle, method_adj, rounding = METHODS[method]
    adj = {**_NO_ADJ, **method_adj}
    solar = _SolarTime(day, lat, lng)
    dhuhr = _utc_date(solar.transit, day)
    sunrise = _utc_date(solar.sunrise, day)
    sunset = _utc_date(solar.sunset, day)
    tomorrow = day + timedelta(days=1)
    tomorrow_solar = _SolarTime(tomorrow, lat, lng)

    asr = _utc_date(solar.afternoon(2 if madhab == "hanafi" else 1), day)
    tomorrow_sunrise = _utc_date(tomorrow_solar.sunrise, tomorrow)
    night = (tomorrow_sunrise - sunset) / 1000
    fajr = _utc_date(solar.hour_angle(-1 * fajr_angle, False), day)
    moonsighting = method == "MoonsightingCommittee"
    if moonsighting and lat >= _HIGH_LATITUDE_THRESHOLD:
        fajr = _adding_seconds(sunrise, -(night / 7))
    if moonsighting:
        safe_fajr = season_adjusted_morning_twilight(lat, day_of_year(day), day.year, sunrise)
    else:
        safe_fajr = _adding_seconds(sunrise, -(1 / 2 * night))  # middle of the night
    if fajr != fajr or safe_fajr > fajr:
        fajr = safe_fajr

    if isha_interval > 0:
        isha = _adding_minutes(sunset, isha_interval)
    else:
        isha = _utc_date(solar.hour_angle(-1 * isha_angle, True), day)
        if moonsighting and lat >= _HIGH_LATITUDE_THRESHOLD:
            isha = _adding_seconds(sunset, night / 7)
        if moonsighting:
            safe_isha = season_adjusted_evening_twilight(lat, day_of_year(day), day.year, sunset, "general")
        else:
            safe_isha = _adding_seconds(sunset, 1 / 2 * night)
        if isha != isha or safe_isha < isha:
            isha = safe_isha

    maghrib = sunset
    if maghrib_angle:
        angle_based = _utc_date(solar.hour_angle(-1 * maghrib_angle, True), day)
        if sunset < angle_based and isha > angle_based:
            maghrib = angle_based

    def done(ms: float, prayer: str) -> float:
        return _rounded_minute(_adding_minutes(ms, adj[prayer]), rounding)

    return {
        "fajr": done(fajr, "fajr"),
        "sunrise": done(sunrise, "sunrise"),
        "dhuhr": done(dhuhr, "dhuhr"),
        "asr": done(asr, "asr"),
        "sunset": _rounded_minute(sunset, rounding),
        "maghrib": done(maghrib, "maghrib"),
        "isha": done(isha, "isha"),
    }
