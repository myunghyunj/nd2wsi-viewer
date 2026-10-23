"""Names used by current correction records and read-only archive compatibility.

Older names remain explicit here so previously saved models and corrected
caches keep their exact provenance checks without being rewritten.
"""

SCHEMA = "BF_free_consensus/1"
NATIVE_CONTRACT = "BF-free-consensus-native-sampled-T-P-spaced-A-then-box-mean"

_CONTRACTS = {
    SCHEMA: {
        "method": "consensus",
        "display_name": "Consensus",
        "native_contract": NATIVE_CONTRACT,
    },
    "BF_free_consensus_rank1/1": {
        "method": "consensus_rank1",
        "display_name": "Consensus rank-1",
        "native_contract": "BF-free-consensus-rank1-native-sampled-T-P-spaced-A-then-box-mean",
    },
}
SUPPORTED_SCHEMAS = frozenset(_CONTRACTS)


def contract_for_schema(schema):
    return _CONTRACTS[schema]
