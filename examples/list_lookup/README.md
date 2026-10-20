# List lookup

`workload.find_known(known_ids, queries)` returns the queries that also appear in `known_ids`.

## Suspected bottleneck

`query in known_ids` is a linear scan of a list, executed once per query, so the call is
O(len(queries) × len(known_ids)). The profile should show nearly all exclusive time inside
`find_known` itself (the `in` operator on a list does not appear as a separate Python frame).

## Plausible optimization and its semantic trade-offs

Building `set(known_ids)` once turns each membership test into an average O(1) hash lookup.

* Elements must be hashable (true for the generated ints, not for arbitrary objects).
* Set membership uses `hash` + `==`; list membership uses identity then `==`. For ints these agree,
  but for objects with inconsistent `__hash__`/`__eq__` the results can differ.
* The set costs extra memory proportional to `len(known_ids)`.
* Output order and repeats are unchanged because the queries are still iterated in order.

`reference/optimized.py` is a hand-written **reference implementation for tests**, not output of
the platform.
