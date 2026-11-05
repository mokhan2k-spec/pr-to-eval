# clamp ignores the upper bound

`tiny.mathx.clamp(value, low, high)` should return `value` limited to the range
`low..high`. It currently only applies the lower bound, so `clamp(42, 0, 10)`
returns `42` instead of `10`.

Make `clamp` respect both bounds. Do not change the existing tests.
