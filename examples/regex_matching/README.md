# Regex matching

`workload.extract_error_codes(lines)` returns the 4-digit code of every standalone `ERR-dddd`
token, in line order.

## Suspected bottleneck

`re.findall(pattern_string, line)` is called once per line with a pattern *string*. A static
rule flags this as "regex compiled in a loop". In CPython, though, `re` caches compiled patterns
keyed by the pattern string, so each call costs a cache lookup in `re._compile` plus the actual
matching, not a recompilation. The profile shows how much time goes to `re._compile` and the
`re.findall` wrapper compared with `Pattern.findall` itself.

## Plausible optimization and its semantic trade-offs

The reference applies the literal fix: compile the pattern once at module level
(`ERROR_CODE = re.compile(...)`). It keeps the common `re.findall(ERROR_CODE, line)` call shape.

* Semantics are identical (same pattern and flags).
* No compilation work is removed, because the original never recompiled. The call still goes
  through the `re.findall` wrapper and `re._compile`. In CPython the module cache is keyed by
  pattern strings, so an already-compiled `Pattern` argument takes the cache-miss path on every
  call. The change can therefore be neutral or even slower. The paired benchmark measures this
  instead of assuming the static rule's advice helps.
* Removing the wrapper as well (calling the bound `ERROR_CODE.findall`) is a different change. It
  can be benchmarked as a separate candidate.
* If a program used more distinct patterns than the `re` cache holds, the original would
  recompile, and hoisting would matter much more than it does here.

`reference/optimized.py` is a hand-written **reference implementation for tests**, not output of
the platform.
