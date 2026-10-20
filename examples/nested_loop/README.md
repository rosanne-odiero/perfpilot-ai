# Nested loop join

`workload.customer_totals(customers, orders)` returns `(name, total amount)` for each customer.

## Suspected bottleneck

The inner `for order in orders` runs once per customer, so the call does
`len(customers) × len(orders)` dictionary lookups and comparisons. cProfile attributes nearly all
exclusive time to `customer_totals` itself.

## Plausible optimization and its semantic trade-offs

Index the orders once (`customer_id -> running total`), then read the index per customer:
O(customers + orders).

* Customer ids must be hashable and their equality must be consistent with hashing.
* Floating-point addition is not associative. The reference adds each customer's amounts in the
  same order as the original, so results match exactly here, but the output contract compares
  totals with `math.isclose(rel_tol=1e-9, abs_tol=1e-9)` so a reordering change would still be
  judged fairly.
* Orders for unknown customers are ignored in both versions; customers with no orders get `0.0`.
* The index costs memory proportional to the number of distinct customers.

`reference/optimized.py` is a hand-written **reference implementation for tests**, not output of
the platform.
