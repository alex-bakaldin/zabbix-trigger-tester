"""Port of Zabbix ``src/libs/zbxalgo/prediction.c``: forecast() and timeleft().

The matrix code, polynomial root finder (Aberth iteration) and the special
cases are transcribed from the server so that the pure-Python engine gives the
same numbers as the native one, including its quirks.
"""

from __future__ import annotations

import math

MATH_ERROR = -1.0
DBL_MAX = 1.7976931348623157e308
EPSILON = 1e-6
MAX_ITERATIONS = 200
INF = float("inf")
NAN = float("nan")

FIT_LINEAR, FIT_POLYNOMIAL, FIT_EXPONENTIAL, FIT_LOGARITHMIC, FIT_POWER = (
    "linear",
    "polynomial",
    "exponential",
    "logarithmic",
    "power",
)
MODE_VALUE, MODE_MAX, MODE_MIN, MODE_DELTA, MODE_AVG = "value", "max", "min", "delta", "avg"


class PredictionError(ValueError):
    pass


def fit_code(text: str | None) -> tuple[str, int]:
    text = text or ""
    if text in ("", "linear"):
        return FIT_LINEAR, 0
    if text.startswith("polynomial"):
        degree = text[len("polynomial") :]
        if not degree.isdigit() or not 1 <= int(degree) <= 6:
            raise PredictionError("polynomial degree is invalid")
        return FIT_POLYNOMIAL, int(degree)
    if text in (FIT_EXPONENTIAL, FIT_LOGARITHMIC, FIT_POWER):
        return text, 0
    raise PredictionError("invalid 'fit' parameter")


def mode_code(text: str | None) -> str:
    text = text or ""
    if text == "":
        return MODE_VALUE
    if text in (MODE_VALUE, MODE_MAX, MODE_MIN, MODE_DELTA, MODE_AVG):
        return text
    raise PredictionError("invalid 'mode' parameter")


# --- IEEE helpers (C arithmetic never raises) -------------------------------


def _div(a: float, b: float) -> float:
    try:
        return a / b
    except ZeroDivisionError:
        if a == 0.0 or math.isnan(a):
            return NAN
        return math.copysign(INF, a) * math.copysign(1.0, b)


def _log(v: float) -> float:
    if v > 0.0:
        return math.log(v)
    return -INF if v == 0.0 else NAN


def _exp(v: float) -> float:
    try:
        return math.exp(v)
    except OverflowError:
        return INF


# --- matrices (lists of rows) ----------------------------------------------


def _identity(n: int) -> list[list[float]]:
    return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


def _transpose(m: list[list[float]]) -> list[list[float]]:
    return [list(col) for col in zip(*m)]


def _mult(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(len(b))) for j in range(len(b[0]))] for i in range(len(a))]


def _inverse(m: list[list[float]]) -> list[list[float]] | None:
    n = len(m)
    if n == 1:
        if m[0][0] == 0.0:
            return None
        return [[1.0 / m[0][0]]]
    if n == 2:
        det = m[0][0] * m[1][1] - m[0][1] * m[1][0]
        if det == 0.0:
            return None
        return [[m[1][1] / det, -m[0][1] / det], [-m[1][0] / det, m[0][0] / det]]
    r = _identity(n)
    left = [row[:] for row in m]
    for i in range(n):
        k = i
        pivot = left[i][i]
        for j in range(i, n):
            if abs(left[j][i]) > abs(pivot):
                k = j
                pivot = left[j][i]
        if pivot == 0.0:
            return None
        if k != i:
            left[i], left[k] = left[k], left[i]
            r[i], r[k] = r[k], r[i]
        for j in range(i + 1, n):
            factor = -left[j][i] / left[i][i]
            if factor != 0.0:
                left[j] = [lj + li * factor for lj, li in zip(left[j], left[i])]
                r[j] = [rj + ri * factor for rj, ri in zip(r[j], r[i])]
    for i in range(n - 1, 0, -1):
        for j in range(i):
            factor = -left[j][i] / left[i][i]
            if factor != 0.0:
                left[j] = [lj + li * factor for lj, li in zip(left[j], left[i])]
                r[j] = [rj + ri * factor for rj, ri in zip(r[j], r[i])]
    for i in range(n):
        r[i] = [v / left[i][i] for v in r[i]]
    return r


def _least_squares(independent: list[list[float]], dependent: list[list[float]]) -> list[float] | None:
    transposed = _transpose(independent)
    inverted = _inverse(_mult(transposed, independent))
    if inverted is None:
        return None
    coefficients = _mult(inverted, _mult(transposed, dependent))
    return [row[0] for row in coefficients]


def _fill_dependent(x: list[float], fit: str) -> list[list[float]] | None:
    if fit in (FIT_EXPONENTIAL, FIT_POWER):
        if any(v <= 0.0 for v in x):
            return None
        return [[math.log(v)] for v in x]
    return [[v] for v in x]


def _fill_independent(t: list[float], fit: str, k: int) -> list[list[float]]:
    if fit in (FIT_LINEAR, FIT_EXPONENTIAL):
        return [[1.0, v] for v in t]
    if fit in (FIT_LOGARITHMIC, FIT_POWER):
        return [[1.0, _log(v)] for v in t]
    n = len(t)
    if k > n - 1:
        k = n - 1
    rows = []
    for v in t:
        row = []
        element = 1.0
        for _ in range(k):
            row.append(element)
            element *= v
        row.append(element)
        rows.append(row)
    return rows


def regression(t: list[float], x: list[float], fit: str, k: int) -> list[float] | None:
    dependent = _fill_dependent(x, fit)
    if dependent is None:
        return None
    try:
        return _least_squares(_fill_independent(t, fit, k), dependent)
    except (OverflowError, ValueError):
        return None


# --- polynomials -----------------------------------------------------------------


def polynomial_value(t: float, c: list[float]) -> float:
    power = 1.0
    result = 0.0
    for coefficient in c:
        result += coefficient * power
        power *= t
    return result


def polynomial_antiderivative(t: float, c: list[float]) -> float:
    power = t
    result = 0.0
    for i, coefficient in enumerate(c):
        result += coefficient * power / (i + 1)
        power *= t
    return result


def derive_polynomial(c: list[float]) -> list[float]:
    if len(c) <= 1:
        return [0.0]
    return [c[i] * i for i in range(1, len(c))]


def polynomial_roots(c: list[float]) -> list[complex] | None:
    degree = len(c) - 1
    highest = c[degree]
    while highest == 0.0 and degree > 0:
        degree -= 1
        highest = c[degree]
    if degree == 0:
        return None if highest == 0.0 else []
    if degree == 1:
        return [complex(-c[0] / c[1], 0.0)]
    if degree == 2:
        temp = c[1] * c[1] - 4 * c[2] * c[0]
        if temp > 0.0:
            temp = -c[1] - math.sqrt(temp) if c[1] > 0 else -c[1] + math.sqrt(temp)
            return [complex(0.5 * temp / c[2], 0.0), complex(2.0 * c[0] / temp, 0.0)]
        real = -0.5 * c[1] / c[2]
        im1 = 0.5 * math.sqrt(-temp)
        return [complex(real, -im1 / c[2]), complex(real, im1)]

    roots = [0j] * degree
    first_nonzero = 0
    while c[first_nonzero] == 0.0:
        first_nonzero += 1
    upper = lower = 1.0
    for i in range(first_nonzero, degree):
        upper = max(upper, abs(c[i] / highest))
        lower = max(lower, abs(c[i + 1] / c[first_nonzero]))
    radius = 1.0 / lower
    iteration = 0
    roots_ok = False
    root_init = False
    updates = [0j] * degree
    try:
        while iteration < MAX_ITERATIONS and not roots_ok:
            iteration += 1
            if not root_init:
                if radius <= upper:
                    count = degree - first_nonzero
                    for i in range(count):
                        angle = (2.0 * math.pi * (i + 0.25)) / count
                        roots[i] = complex(radius * math.cos(angle), radius * math.sin(angle))
                    radius *= 2.0
                else:
                    root_init = True
            roots_ok = True
            max_update = 0.0
            for i in range(first_nonzero, degree):
                z = roots[i]
                denominator = complex(highest, 0.0)
                for j in range(first_nonzero, degree):
                    if j != i:
                        denominator *= z - roots[j]
                zpower = 1 + 0j
                polynomial = complex(c[first_nonzero], 0.0)
                for j in range(first_nonzero + 1, degree + 1):
                    zpower *= z
                    polynomial += zpower * c[j]
                residual = abs(polynomial.real) + abs(polynomial.imag)
                roots_ok = roots_ok and EPSILON > residual
                temp = denominator.real * denominator.real + denominator.imag * denominator.imag
                if temp != 0.0:
                    updates[i] = complex(
                        (polynomial.real * denominator.real + polynomial.imag * denominator.imag) / temp,
                        (polynomial.imag * denominator.real - polynomial.real * denominator.imag) / temp,
                    )
                else:
                    updates[i] = 0j
                temp = updates[i].real * updates[i].real + updates[i].imag * updates[i].imag
                if temp > max_update:
                    max_update = temp
            if max_update > radius * radius and not root_init:
                continue
            root_init = True
            for i in range(first_nonzero, degree):
                roots[i] -= updates[i]
    except (OverflowError, ValueError):
        return None
    return roots if roots_ok else None


def _polynomial_minmax(now: float, time: float, mode: str, c: list[float]) -> float | None:
    roots = polynomial_roots(derive_polynomial(c))
    if roots is None:
        return None
    minimum = polynomial_value(now, c)
    tmp = polynomial_value(now + time, c)
    if tmp < minimum:
        maximum, minimum = minimum, tmp
    else:
        maximum = tmp
    for root in roots:
        tmp = root.real
        if tmp < now or tmp > now + time:
            continue
        tmp = polynomial_value(tmp, c)
        if tmp < minimum:
            minimum = tmp
        elif tmp > maximum:
            maximum = tmp
    if mode == MODE_MAX:
        return maximum
    if mode == MODE_MIN:
        return minimum
    return maximum - minimum


def _polynomial_timeleft(now: float, threshold: float, c: list[float]) -> float | None:
    shifted = c[:]
    shifted[0] -= threshold
    roots = polynomial_roots(shifted)
    if roots is None:
        return None
    result = None
    for root in roots:
        tmp = root.real
        if result is None:
            if tmp > now and EPSILON > abs(polynomial_value(tmp, shifted)):
                result = tmp
        elif now < tmp < result and EPSILON > abs(polynomial_value(tmp, shifted)):
            result = tmp
    return DBL_MAX if result is None else result - now


def calculate_value(t: float, c: list[float], fit: str) -> float:
    if fit == FIT_LINEAR:
        return c[0] + c[1] * t
    if fit == FIT_POLYNOMIAL:
        return polynomial_value(t, c)
    if fit == FIT_EXPONENTIAL:
        return _exp(c[0] + c[1] * t)
    if fit == FIT_LOGARITHMIC:
        return c[0] + c[1] * _log(t)
    return _exp(c[0] + c[1] * _log(t))


def _finish(result: float | None, clamp_negative: bool) -> float:
    if result is None or math.isnan(result):
        return MATH_ERROR
    if clamp_negative:
        return DBL_MAX if result < 0.0 or result > DBL_MAX else result
    if result > DBL_MAX:
        return DBL_MAX
    if result < -DBL_MAX:
        return -DBL_MAX
    return result


def forecast(t: list[float], x: list[float], now: float, time: float, fit: str, k: int, mode: str) -> float:
    n = len(t)
    if n == 1:
        return 0.0 if mode == MODE_DELTA else x[0]
    if fit == FIT_POLYNOMIAL and n <= k:
        return MATH_ERROR
    c = regression(t, x, fit, k)
    if c is None:
        return MATH_ERROR
    try:
        if mode == MODE_VALUE:
            return _finish(calculate_value(now + time, c, fit), False)
        if time == 0.0:
            return _finish(0.0 if mode == MODE_DELTA else calculate_value(now, c, fit), False)
        if fit != FIT_POLYNOMIAL:
            left = calculate_value(now, c, fit)
            right = calculate_value(now + time, c, fit)
            if mode == MODE_MAX:
                result = max(left, right)
            elif mode == MODE_MIN:
                result = min(left, right)
            elif mode == MODE_DELTA:
                result = left - right if left > right else right - left
            elif fit == FIT_LINEAR:
                result = 0.5 * (left + right)
            elif fit == FIT_EXPONENTIAL:
                result = _div(_div(right - left, time), c[1])
            elif fit == FIT_LOGARITHMIC:
                result = right + c[1] * (_log(1.0 + time / now) * now / time - 1.0)
            elif c[1] != -1.0:
                result = _div(_div(right * (now + time) - left * now, time), c[1] + 1.0)
            else:
                result = _exp(c[0]) * _log(1.0 + time / now) / time
            return _finish(result, False)
        if mode == MODE_AVG:
            return _finish((polynomial_antiderivative(now + time, c) - polynomial_antiderivative(now, c)) / time, False)
        return _finish(_polynomial_minmax(now, time, mode, c), False)
    except (OverflowError, ValueError, ZeroDivisionError):
        return MATH_ERROR


def timeleft(t: list[float], x: list[float], now: float, threshold: float, fit: str, k: int) -> float:
    n = len(t)
    if n == 1:
        return 0.0 if x[0] == threshold else DBL_MAX
    c = regression(t, x, fit, k)
    if c is None:
        return MATH_ERROR
    try:
        current = calculate_value(now, c, fit)
        if current == threshold:
            return 0.0
        if fit == FIT_LINEAR:
            result: float | None = _div(threshold - c[0], c[1]) - now
        elif fit == FIT_POLYNOMIAL:
            result = _polynomial_timeleft(now, threshold, c)
        elif fit == FIT_EXPONENTIAL:
            result = _div(_log(threshold) - c[0], c[1]) - now
        elif fit == FIT_LOGARITHMIC:
            result = _exp(_div(threshold - c[0], c[1])) - now
        else:
            result = _exp(_div(_log(threshold) - c[0], c[1])) - now
    except (OverflowError, ValueError, ZeroDivisionError):
        return MATH_ERROR
    return _finish(result, True)
