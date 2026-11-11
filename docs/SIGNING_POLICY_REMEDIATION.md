# Signing and credential policy remediation

H05: production Jupiter signing verifies `EXPECTED_WALLET_ADDRESS` at the shared
`sign_after_checks` boundary and both orchestration entry points. Existing drill
identity checks and the configured wallet identity remain unchanged. The wallet
loader alone is not a trade authorization boundary.

H09: mint decimals must be integers from 0 through 255, excluding booleans.
Zero-decimal mints remain valid. Buy execution obtains the chain decimal fact
before constructing the local trade intent; unavailable or disagreeing provider
and chain decimals refuse the operation. This preserves the existing RPC trust
policy for `getTokenSupply`; it does not establish new multi-provider consensus.

H14: production approval consumption supplies a canonical operation containing
side, wallet, input/output mints, exact raw input quantity, mint decimals, maximum
slippage, and optional minimum raw output. Identity and quantity must match;
execution may tighten the approved slippage/output bounds. Expiry and one-use
consumption still apply. Mismatches leave the approval unconsumed. Existing
unbound approvals must be proposed and approved again before production use.
`propose_trade` now verifies wallet identity and chain decimals when creating a
bound buy proposal. Generic low-level queue proposals can carry a canonical sell
operation; a buy approval never authorizes a sell. Manual-confirmation defaults
and trading caps have not changed.

H06: credential-bearing local curl/browser requests are restricted to the exact
configured FOMO API origin, including scheme and port. Curl does not follow
redirects. Browser credentials are applied only by a scoped route fetch with
redirects disabled, never through context-wide extra headers. Cross-origin
resources and redirects are refused before forwarding credentials. A failed
route setup leaves the browser without injected credentials. Approved provider
proxies that explicitly forward headers are a separate intermediary trust
policy, not a same-origin browser guarantee.

H15: the Jupiter HTTP adapters use the shared bounded HTTP reader. The stealth
adapter additionally caps body text before JSON parsing. Scrapling/Playwright
materialize their response before exposing `.body`; that parser cap alone does
not bound their internal transport/decompression allocation. These transports
need independently verified streaming interception or process resource limits
for a complete byte-allocation guarantee. No such guarantee is claimed here.

All regression fixtures use dummy identities and mocked irreversible boundaries.
No production wallet, native approved-DEX exploit fixture, signing/broadcast,
provider service, or real financial effect was exercised.
