"""What a caller may SEE, decided before the model plans (gt-lotr 0.5.0, gt 0.21.4).

ACS calls it attention exposure: a model that has been shown a capability already reasons with it,
and a refusal after the fact does not take the idea back. So the operations a caller could not use
are left out of `find` altogether, `find(connection, op)` (describe) answers for them exactly as it
does for an operation that does not exist, and the same rules run again when a call is made. The
rule here is the one rule for all three moments; the engine asks it with the facts it holds.

An operation is hidden when, in this order:

  denied        the owner's connection policy denies it (`policy.deny`)
  <code>        policy.check_client says no: client_not_allowed, tier_ceiling, client_revoked. For the
                local caller that is gateway.json `local` (allow, max_tier); for a hub client its
                enrolment
  unattended    a hub client and an OAuth sign-in: an OAuth sign-in is a person's, so an unattended
                client may only read through it (the call would be oauth_unattended_refused)
  role          the caller said which agent it is (`role`) and its tools do not carry the tier:
                reader = find + call_read, writer = find + call_write + call_consent (the
                agent_split design), combined = all four -- but only where `agent_split` combined is
                IN EFFECT, which needs lockdown insecure (lotrlib.agent_split); anywhere else a
                combined agent sees nothing and is told why. A role narrows, it never widens, and
                nothing here can tell which agent is really asking: it is a courtesy that keeps a
                reader's context clear of tools it cannot call, enforced again by the tool tiers
                at call time. That is why it is not called a boundary
  <authority>   gt unlock's authority hard-refuses the scope `lotr:<connection>:<tier>` for this
                process (mcp_only, denied). Asked with request=False, so visibility never prompts.
                `locked` and `step_up` are NOT hard: the person can unlock, and the shim does it
                on the call, so the operation stays visible

A persona is not a source. A persona changes what gt OFFERS and never what it ENFORCES (gt ADR-18,
and lotrlib.persona says the same of LOTR); nothing here reads one, and a test pins that.
"""
from . import agent_split, policy
from .errors import GatewayError

ROLES = ("reader", "writer", "combined")
ROLE_TIERS = {"reader": ("read",), "writer": ("write", "consent"),
              "combined": ("read", "write", "consent")}
AUTHORITY_HARD = ("mcp_only", "denied")


def role_tiers(role, cfg=None):
    """-> (tiers the role's tools carry or None for no narrowing, a note for the model or None).
    Raises GatewayError(bad_request) for a role that is not one of ROLES."""
    if role is None:
        return None, None
    if role not in ROLES:
        raise GatewayError("bad_request", "role must be one of %s" % ", ".join(ROLES))
    if role == "combined":
        mode, why = agent_split.effective(cfg)
        if mode != "combined":
            return (), ("agent_split combined is not in effect%s; use the reader and writer agents"
                        % (" (" + why + ")" if why else ""))
    return ROLE_TIERS[role], None


def hidden_reason(*, tier, denied, client, conn_id, remote, oauth, role_tiers, authority):
    """-> None when the caller may see the operation, else why not (a short code, for the audit
    log only: the model is never told which rule hid it). `authority(conn_id, tier)` returns None
    or a hard refusal code; pass None when gt unlock is off or the caller is a hub client."""
    if denied:
        return "denied"
    try:
        policy.check_client(client, conn_id, tier)
    except GatewayError as e:
        return e.code
    if remote and oauth and tier != "read":
        return "unattended"
    if role_tiers is not None and tier not in role_tiers:
        return "role"
    if authority is not None:
        code = authority(conn_id, tier)
        if code:
            return "authority:" + code
    return None
