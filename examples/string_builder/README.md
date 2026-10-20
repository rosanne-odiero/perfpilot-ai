# String builder

`workload.render_report(rows)` produces a fixed-width text report, one line per row.

## Suspected bottleneck

`report += line` inside the loop. In general, immutable-string concatenation copies the
accumulated string each time, which is O(n²). **However**, CPython has an optimisation for
`s += t` when `s` has no other references: it resizes the string in place, so the loop is often
close to linear. Most of the time is usually spent in f-string formatting, not concatenation.

## Plausible optimization and its semantic trade-offs

Collecting lines in a list and calling `"".join(...)` once is the textbook fix.

* The output is byte-for-byte identical.
* The list of lines is an extra temporary allocation (higher peak traced memory).
* The speed-up is implementation-dependent: on CPython it may be negligible or even negative,
  while on PyPy or when another reference to `report` exists, `+=` can be truly quadratic.

This workload tests whether the textbook advice still pays off on the interpreter at hand. The
paired benchmark reports the measured speed difference, which may be modest rather than the
asymptotic win the textbook promises. The memory comparison shows the cost of the temporary list
(higher traced peak). PerfPilot does not assume a benefit.

`reference/optimized.py` is a hand-written **reference implementation for tests**, not output of
the platform.
