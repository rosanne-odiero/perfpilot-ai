# Duplicate counter

`workload.count_duplicates(items)` returns `{item: count}` for every item that occurs more than
once.

## Suspected bottleneck

`items.count(item)` runs inside the loop over `items`, so every element triggers a full rescan:
O(n²). In a cProfile the exclusive time is split between `count_duplicates` and the built-in
`list.count` method, which is called once per element (the call count equals `len(items)`).

## Plausible optimization and its semantic trade-offs

A single pass with `collections.Counter` is O(n).

* Items must be hashable; `list.count` only needs `==`.
* `Counter` groups by `hash` + `==`, so objects whose equality is inconsistent with their hash
  (or `NaN`-like values that are not equal to themselves) can be counted differently.
* The original assigns `duplicates[item]` repeatedly for each occurrence; the result is the same
  dict (same keys, values and first-occurrence key order).
* Peak memory grows by one counter entry per distinct item.

`reference/optimized.py` is a hand-written **reference implementation for tests**, not output of
the platform.
