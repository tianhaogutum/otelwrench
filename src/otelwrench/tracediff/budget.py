# SPDX-License-Identifier: Apache-2.0
"""Exact threshold comparison against the decimal form of a policy number.

The decimal form is the JavaScript ``String(number)`` rendering, so ``0.1`` is
compared as exactly 1/10 rather than as its binary floating-point value.
"""
from __future__ import annotations

from .jsontext import js_number


def rational(number) -> tuple[int, int]:
    mantissa, _, exponent = js_number(number).partition("e")
    whole, _, fraction = mantissa.partition(".")
    power = int(exponent or "0") - len(fraction)
    if power >= 0:
        return int(whole + fraction) * 10 ** power, 1
    return int(whole + fraction), 10 ** -power


def exceeds(numerator: int, denominator: int, budget: tuple[int, int]) -> bool:
    num, den = budget
    return numerator * den > num * denominator
