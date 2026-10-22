"""Explicit request terms and conservative verification of provider observations.

No locally asserted verification boolean is authoritative. GET metadata must
affirm the requested terms; optional POST fields can reveal conflicts but their
absence never confirms anything. This does not prove account eligibility.
"""

from agentpal.vault.security import Keyring


USAGE_PATTERN = "IMMEDIATE"
DESCRIPTION = (
    "AgentPal Sandbox: future variable on-demand charges to AgentPal Sandbox Business; "
    "no retailer payment or fulfillment."
)
TERMS = {"usage_pattern": USAGE_PATTERN, "usage_type": "MERCHANT",
         "customer_type": "CONSUMER", "description": DESCRIPTION}


def metadata(result: dict) -> dict:
    """Retain only bounded agreement fields, never arbitrary payer/response JSON."""
    source = result.get("payment_source")
    wallet = source.get("paypal") if isinstance(source, dict) else None
    if source is not None and (not isinstance(source, dict) or (source and set(source) != {"paypal"})
            or ("paypal" in source and not isinstance(wallet, dict))):
        return {key: "invalid" for key in TERMS}
    if not isinstance(wallet, dict):
        return {key: None for key in TERMS}
    return {key: (None if wallet.get(key) is None else
                  wallet[key] if isinstance(wallet[key], str) and len(wallet[key]) <= 128 else "invalid")
            for key in TERMS}


def conflicts(observation: dict) -> bool:
    fields = observation.get("metadata")
    return isinstance(fields, dict) and any(fields.get(k) is not None and fields[k] != v for k, v in TERMS.items())


def evaluate(request: dict, setup: dict, approved: dict, token: dict, retrieved: dict) -> str:
    """Return verified, unverified or conflict from actual normalized evidence."""
    source = request.get("payment_source")
    wallet = source.get("paypal") if isinstance(source, dict) else None
    if not isinstance(wallet, dict) or any(wallet.get(k) != v for k, v in TERMS.items()):
        return "unverified"  # Never promote a legacy or different requested agreement.
    observations = (setup, approved, token, retrieved)
    if not all(isinstance(o, dict) for o in observations):
        return "unverified"
    if (not setup.get("id") or not setup.get("customer_id") or not token.get("id")
            or setup.get("id") != approved.get("id")
            or any(o.get("customer_id") != setup["customer_id"] for o in observations)
            or token.get("id") != retrieved.get("id")
            or setup.get("status") != "PAYER_ACTION_REQUIRED" or approved.get("status") != "APPROVED"
            or any(conflicts(o) for o in observations)):
        return "conflict"
    if any(o.get("metadata") != TERMS for o in (approved, retrieved)):
        return "unverified"
    return "verified"


def stored_state(keys: Keyring, row, reference: dict) -> str:
    """Re-derive authority from immutable record/purpose-bound envelopes."""
    names = (("approved_reference_ciphertext", "approved_key_id"),
             ("agreement_evidence_ciphertext", "agreement_evidence_key_id"))
    if row["flow_phase"] != "complete" or any(row.get(cipher) is None for cipher, _ in names):
        return "unverified"
    identifier = row["id"]
    flow = keys.open(identifier, "flow", row["flow_ciphertext"], row["flow_key_id"])
    setup = keys.open(identifier, "setup", row["setup_reference_ciphertext"], row["encryption_key_id"])
    approved = keys.open(identifier, "approved-setup", row["approved_reference_ciphertext"], row["approved_key_id"])
    token = keys.open(identifier, "token", row["token_ciphertext"], row["token_key_id"])
    evidence = keys.open(identifier, "usage-agreement", row["agreement_evidence_ciphertext"], row["agreement_evidence_key_id"])
    if reference != token or evidence.get("version") != 1:
        return "unverified"
    return evaluate(flow, setup, approved, token, evidence.get("payment_token"))
