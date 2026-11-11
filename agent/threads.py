"""Link reply emails to their parent claim and merge their facts (reply wins)."""
from __future__ import annotations

import copy
from dataclasses import fields
from typing import Dict, List

from agent.models import Email, Facts, Judgment

# Fields that are never inherited from the parent or overwritten by the reply's empty values.
_SKIP = {"claim_id", "documents", "photo_count", "thread_parent", "extractor", "intent"}


def link_threads(emails: List[Email]) -> Dict[str, str]:
    """Map reply claim_id -> parent claim_id where reply.in_reply_to == parent.message_id."""
    by_mid = {e.message_id: e.claim_id for e in emails if e.message_id}
    links: Dict[str, str] = {}
    for e in emails:
        if e.in_reply_to and e.in_reply_to in by_mid and by_mid[e.in_reply_to] != e.claim_id:
            links[e.claim_id] = by_mid[e.in_reply_to]
    return links


def _empty(v: object) -> bool:
    return v is None or v == "" or v == [] or v == {}


def merge_thread(parent: Facts, reply: Facts) -> Facts:
    """Reply's non-null/non-empty values win; documents concatenated; photos summed; judgments OR-ed.

    The merged claim keeps the reply's claim_id and gets thread_parent = parent.claim_id. A reply whose
    intent is reply_with_info becomes the parent's intent (normally new_claim) so it is decided as a claim.
    """
    merged = copy.deepcopy(parent)
    for f in fields(Facts):
        name = f.name
        if name in _SKIP:
            continue
        pv, rv = getattr(parent, name), getattr(reply, name)
        if isinstance(pv, Judgment):
            if rv.value:
                setattr(merged, name, copy.deepcopy(rv))
            elif pv.value:
                setattr(merged, name, copy.deepcopy(pv))
            else:
                setattr(merged, name, Judgment(False, rv.evidence or pv.evidence))
        elif not _empty(rv):
            setattr(merged, name, copy.deepcopy(rv))
    merged.claim_id = reply.claim_id
    merged.documents = copy.deepcopy(parent.documents) + copy.deepcopy(reply.documents)
    merged.photo_count = parent.photo_count + reply.photo_count
    merged.thread_parent = parent.claim_id
    merged.extractor = reply.extractor
    merged.intent = parent.intent if reply.intent in ("reply_with_info", "new_claim") else reply.intent
    return merged
