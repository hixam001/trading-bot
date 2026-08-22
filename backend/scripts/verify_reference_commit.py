"""Scratch: independently verify omotrades' commit-reveal mechanism.

Takes a revealed preimage + published sha256 from omotrades.com/proof and
recomputes the hash locally. If it matches, our appendix's documented
mechanism (sha256('omo-commit-v1|' + nonce + '|' + canonical(payload)))
is confirmed against the real system, byte for byte.
"""
import hashlib

# From https://omotrades.com/proof — revealed entry, 2026-08-21 04:12:35z:
preimage = (
    'omo-commit-v1|37e5b6f340027d0ebbf2a2d241eec30d|'
    '{"decision_at":"2026-08-21T04:12:35.251+00:00","inputs":{"ageHours":1.36,'
    '"buys1h":1880,"cashUsd":55817.5,"chg1h":18.55,"chg6h":91.84,"fomo":20,'
    '"hasSite":false,"heldUsd":0,"liquidityUsd":19937,"researched":true,'
    '"sells1h":6469,"socials":["twitter"],"vol1h":177402},"mint":'
    '"GqnkeRkiSNMdRNCg94hpsBJnQq9ZFsicWhnyDBWNpump","note":"refused market buy '
    'into Guineas while 1h volume sits at $177k with sellers leading","rules":'
    '[{"detail":"pool $19,937","id":"liquidity_floor","pass":true},'
    '{"detail":"1h volume $177,402","id":"volume_alive","pass":true},'
    '{"detail":"1880 buys vs 6469 sells","id":"buy_pressure","pass":false},'
    '{"detail":"age 1.4h, 1h 18.6%","id":"not_newborn_fade","pass":true},'
    '{"detail":"twitter","id":"public_presence","pass":true},'
    '{"detail":"fomo 20","id":"crowd_heat","pass":false},'
    '{"detail":"cash $55817.50","id":"cash_available","pass":true},'
    '{"detail":"no size on","id":"already_held","pass":true},'
    '{"detail":"awake","id":"not_on_break","pass":true}],"side":null,'
    '"symbol":"GUINEAS","v":1,"verdict":"pass"}'
)
published_hash = "0cd102c7962951650140e42690226e8ea773a2597583ba56b45d054a7b0a67ea"

computed = hashlib.sha256(preimage.encode()).hexdigest()
print("published:", published_hash)
print("computed :", computed)
print("MATCH    :", computed == published_hash)

# Also verify the second revealed entry to rule out coincidence.
preimage2 = (
    'omo-commit-v1|2bd7aac9e705c03dea88aaeaddb5bee8|'
    '{"decision_at":"2026-08-21T04:08:27.023+00:00","inputs":{"ageHours":1.32,'
    '"buys1h":1995,"cashUsd":58412.26,"chg1h":13.47,"chg6h":70.05,"fomo":22,'
    '"hasSite":false,"heldUsd":0,"liquidityUsd":18670,"researched":true,'
    '"sells1h":6471,"socials":["twitter"],"vol1h":182584},"mint":'
    '"GqnkeRkiSNMdRNCg94hpsBJnQq9ZFsicWhnyDBWNpump","note":"refused market buy '
    'into Guineas while 1h volume sits at $183k with sellers leading","rules":'
    '[{"detail":"pool $18,670","id":"liquidity_floor","pass":true},'
    '{"detail":"1h volume $182,584","id":"volume_alive","pass":true},'
    '{"detail":"1995 buys vs 6471 sells","id":"buy_pressure","pass":false},'
    '{"detail":"age 1.3h, 1h 13.5%","id":"not_newborn_fade","pass":true},'
    '{"detail":"twitter","id":"public_presence","pass":true},'
    '{"detail":"fomo 22","id":"crowd_heat","pass":false},'
    '{"detail":"cash $58412.26","id":"cash_available","pass":true},'
    '{"detail":"no size on","id":"already_held","pass":true},'
    '{"detail":"awake","id":"not_on_break","pass":true}],"side":null,'
    '"symbol":"GUINEAS","v":1,"verdict":"pass"}'
)
published2 = "21fcce8677beff02d7688c214d57bd957eaad9fb960190d1bdb358ae765d8fbb"
computed2 = hashlib.sha256(preimage2.encode()).hexdigest()
print("published2:", published2)
print("computed2 :", computed2)
print("MATCH2    :", computed2 == published2)
