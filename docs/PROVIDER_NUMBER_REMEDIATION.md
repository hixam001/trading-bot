# Provider number and listing policy

H08/H11: research enrichment validates each nested provider object before field
access. Numeric facts are finite and exclude booleans; transaction counts are
nonnegative integers. Malformed individual records do not abort valid sibling
candidates. Meaningful zero liquidity, volume, counts and price changes remain
zero. Invalid/missing facts remain unavailable.

Cross-pool total liquidity and volume are reported only when every included
Solana pool supplies the corresponding valid fact. Partial observations cannot
establish an all-pool total or concentration. All-zero liquidity has an undefined
`top_pool_share`, represented by `None`. The existing transaction-count policy
is retained: buys/sells sum reporting pools and remain `None` if none report.
Thus reported transaction counts can be partial; they are not a completeness
claim. No new thresholds or trading strategy were introduced.

Required numeric gate inputs receive a second finite/nonboolean check at the
rule boundary, including momentum/age, crowd heat and cash. Invalid inputs give
an explicit unavailable result, preventing `NaN` comparisons from authorizing
entry. Security unknowns retain the existing operator policy: `None` is not
converted into a known-bad security fact. Regression tests explicitly verify
that policy and legitimate zero momentum.

Sandbox checks: 165 tests passed across provider-number regressions, deterministic
rules, staged decision pipeline and fake-chart neighbors. Against the reviewed
baseline research/rules modules in a disposable sandbox source copy, the new
finite-number regression failed because `_f(True)` returned `1.0`. Current
source returns unavailable. This evidence is local policy behavior, without
provider requests or production state.
