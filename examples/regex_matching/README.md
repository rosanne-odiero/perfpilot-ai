# Regex matching

`workload.extract_error_codes(lines)` returns the 4-digit code of every standalone `ERR-dddd`
token, in line order.

## Suspected bottleneck

`re.findall(pattern_string, line)` is called once per line with a pattern *string*. A static
rule flags this as "regex compiled in a loop". In CPython, though, `re` caches compiled patterns,
so each call costs a cache lookup (`re._compile`) plus the actual matching, not a recompile. The
profile shows how much time `re._compile` really takes compared with `Pattern.findall`.

## Plausible optimization and its semantic trade-offs

Hoisting `re.compile(...)` to module level and calling the bound `findall` removes the cache
lookup and one Python-level call per line.

* Semantics are identical (same pattern and flags).
* The gain is bounded by the share of time spent in `re._compile`/`re.findall` wrappers; matching
  itself is unchanged. Expect a modest effect, and let the benchmark decide whether it clears the
  practical-significance threshold.
* If a program uses more distinct patterns than the `re` cache holds (`re._MAXCACHE`), the
  original would recompile, and hoisting would matter much more than here.

`reference/optimized.py` is a hand-written **reference implementation for tests**, not output of
the platform.
