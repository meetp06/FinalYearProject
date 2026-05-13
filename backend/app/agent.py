from __future__ import annotations

import logging
import re
from typing import List, Tuple

import httpx

from . import tools
from .schemas import ToolInvocation
from .sentiment import detect_intent as _detect_intent, _strip_product_phrases
from .settings import get_settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are Atlas, a warm, direct, and helpful customer support representative for our e-commerce platform. "
    "You resolve shopping inquiries using the verified system data provided with each message. "
    "CRITICAL: All system data has already been fetched for you. NEVER say you need to check, look up, "
    "or gather information — state the facts directly from the data provided. "
    "NEVER ask the customer for their order number if it appears in the data below. "
    "When system data is provided, state those exact facts — order ID, carrier, dates, "
    "amounts, policy text — and do not add information from outside what is given. "
    "REPLY IN 2-3 SENTENCES MAXIMUM. Be direct and specific. "
    "Do NOT copy-paste policy text. Do NOT add caveats like 'it may vary' or 'contact support' "
    "unless you have no other answer. Never start with 'I appreciate your message' or 'I see what you mean'."
)

_ORDER_ID_RE = re.compile(r'\bORD-[\w-]+\b', re.IGNORECASE)

_POLICY_KEYWORDS = {
    'return', 'refund', 'shipping', 'warranty', 'guarantee', 'cancel', 'exchange',
    'policy', 'eligible', 'damaged', 'missing', 'lost', 'delivery', 'prime',
    'delivered', 'arrived', 'received', 'package', 'never', 'wrong',
    'described', 'late', 'delayed', 'waiting', 'door', 'porch',
    'neighbor', 'left', 'nowhere', 'find',
    # return condition / packaging
    'opened', 'open box', 'condition', 'seal', 'packaging', 'original box',
    'unwrap', 'used it', 'tried it', 'start a return', 'initiate a return',
    'how to return', 'begin a return', 'process a return', 'steps to return',
}
_PRODUCT_KEYWORDS = {
    'recommend', 'suggest', 'looking for', 'show me', 'find me', 'best',
    'under $', 'cheap', 'buy', 'purchase', 'product', 'headphone', 'laptop',
    'phone', 'camera', 'watch', 'bag', 'shoe', 'keyboard', 'monitor',
    'earbuds', 'speaker', 'tablet', 'charger', 'cable', 'stand', 'desk',
    'chair', 'light', 'lamp', 'game', 'toy', 'headset', 'mouse',
    'printer', 'router', 'backpack', 'suitcase', 'luggage', 'wallet',
    'jacket', 'clothing', 'glasses', 'sunglasses', 'book', 'vacuum',
    'blender', 'coffee', 'pillow', 'mattress', 'towel', 'bottle',
    # additional coverage
    'bundle', 'electronics', 'gadget', 'device', 'accessories', 'accessory',
    'earphone', 'earbud', 'airpod', 'wearable', 'smartwatch', 'fitness tracker',
    'features', 'specs', 'specifications', 'model', 'brand', 'series',
}
_ACCOUNT_KEYWORDS = {
    'account', 'membership', 'tier', 'profile', 'my info', 'member since',
    'how many orders', 'open orders', 'total orders', 'order history',
    'recent orders', 'my orders', 'order count', 'orders i', 'orders placed',
    'orders have i', 'my account info', 'account details', 'my profile',
    'my membership', 'member benefits', 'my tier', 'account status',
}
_REFUND_KEYWORDS = {'refund', 'money back', 'charge back', 'reimbburse', 'reimburse'}
_CANCEL_KEYWORDS = {
    'cancel my order', 'cancel the order', 'cancel order', 'i want to cancel',
    'cancellation', 'cancel it', 'stop my order', "don't want it anymore",
    'no longer want', 'undo my order', 'abort',
}
# Phrases that CONTAIN "cancel" but are NOT order-cancellation requests
_CANCEL_FALSE_POSITIVES = {'noise cancel', 'noise-cancel', 'active cancel'}

_ORDER_FOLLOWUP_RE = re.compile(
    r'\b(carrier|when|eta|arrival|transit|tracking|estimated|update|'
    r'late|early|delayed|status|same order|that order|expedite|delivery date|'
    r'where|how long|still|yet|received|expect|location|it is|it\'s|'
    r'wrong with|broken|defective|missing item|not arrived|'
    r'items|contents|include|what is in|what\'s in|in it|in the order|'
    r'what did i order|what was ordered|order details|order summary|'
    r'that\'s it|yes that|this is it|that is it)\b',
    re.IGNORECASE,
)
_PRODUCT_FOLLOWUP_RE = re.compile(
    r'\b(yes|waterproof|battery|wireless|bluetooth|bass|'
    r'volume|size|color|colour|brand|cheaper|better|similar|'
    r'instead|alternative|what about|how about|does it|will it|'
    r'is it|can it|any other|other option|the first|the second|that one)\b',
    re.IGNORECASE,
)
_REFUND_FOLLOWUP_RE = re.compile(
    r'\b(confirmed|confirm|processed|went through|refund|money back|'
    r'how long|when will|appear|statement|bank|credit|receive|'
    r'status|pending|done|complete|successful|approved|issued)\b',
    re.IGNORECASE,
)


def _fmt_order(result: dict) -> str:
    if not result.get("ok") or not result.get("found"):
        oid = result.get("order_id", "N/A")
        return f"Order Number: {oid}\nStatus: Not found. {result.get('message', '')}"
    oid = result.get("order_id", "N/A")
    items = result.get("items", [])
    item_list = ", ".join(f"{i.get('title', 'Item')} x{i.get('qty', 1)}" for i in items)
    lines = [
        f"Order Number: {oid}",
        f"Order Status: {result.get('status', 'N/A')}",
        f"Shipping Method: {result.get('carrier', 'N/A')}",
        f"Tracking Number: {result.get('tracking', 'N/A')}",
        f"Delivery Date: {result.get('eta', 'N/A')}",
        f"Items Ordered: {item_list}",
        f"Order Total: {result.get('total', 'N/A')}",
    ]
    return "\n".join(lines)


def _extract_order_id_from_history(history: List[dict]) -> str:
    """Scan the last few history messages for an order ID when none is in the current message."""
    for msg in reversed(history[-6:]):
        content = msg.get("content") or ""
        m = _ORDER_ID_RE.search(content)
        if m:
            return m.group(0).upper()
    return ""


def _extract_last_product_query(history: List[dict]) -> str:
    """Find the most recent user message that was a product search query."""
    for msg in reversed(history[-6:]):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if any(k in content.lower() for k in _PRODUCT_KEYWORDS):
                return content
    return ""


def _extract_prior_refund_order_id(history: List[dict]) -> str:
    """Return the order ID from a refund that was processed earlier in this session."""
    def _looks_like_refund_processed(content: str) -> bool:
        c = content.lower()
        # "Refund ID:" is always emitted by the template when process_refund succeeds
        if "refund id" in c:
            return True
        # Personalized confirmation with a specific amount — never appears in policy text
        if "your refund" in c and ("$" in content or "usd" in c):
            return True
        return False

    if not any(
        _looks_like_refund_processed(msg.get("content") or "")
        for msg in history[-10:]
        if msg.get("role") == "assistant"
    ):
        return ""
    # Refund was confirmed — scan ALL messages (user + assistant) for an order ID
    for msg in reversed(history[-10:]):
        m = _ORDER_ID_RE.search(msg.get("content") or "")
        if m:
            return m.group(0).upper()
    return ""


def _in_order_mode(history: List[dict]) -> bool:
    """Return True if a recent assistant message contains order status output."""
    for msg in reversed(history[-4:]):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if any(p in content for p in (
                "Order Number:", "Order Status:", "Status is **",
                "estimated delivery **", "Here's the latest on",
            )):
                return True
    return False


def _route_tools(
    message: str,
    ctx: tools.ToolContext,
    history: List[dict] = [],
) -> Tuple[List[ToolInvocation], str]:
    """Deterministically call tools based on message content.
    Returns (invocations, context_block_to_inject)."""
    # Strip product/feature phrases before keyword matching so that e.g.
    # "active noise cancellation" doesn't leak "cancellation" into cancel/policy routing.
    msg_lower = _strip_product_phrases(message).lower()
    invocations: List[ToolInvocation] = []
    blocks: List[str] = []

    order_match = _ORDER_ID_RE.search(message)

    # Bug fix: refund takes priority over cancel when both keywords appear
    has_refund = any(k in msg_lower for k in _REFUND_KEYWORDS)
    has_cancel = (
        any(k in msg_lower for k in _CANCEL_KEYWORDS)
        and not any(fp in msg_lower for fp in _CANCEL_FALSE_POSITIVES)
        and not has_refund
    )

    # Only pull order ID from history when the current message is actually order-related.
    # Use word boundaries to avoid "ship" matching "membership", etc.
    _ORDER_CONTEXT_RE = re.compile(
        r'\b(order|track|tracking|shipped|shipping|deliver|delivered|delivery|'
        r'cancel|cancell?ation|refund|return|package|parcel|status|carrier|'
        r'eta|transit|estimated|arrival|where is|where\'s|when will|when does|'
        r'arrive|arrived|item|same order|that order|my order)\b',
        re.IGNORECASE,
    )
    if not order_match and history and _ORDER_CONTEXT_RE.search(message):
        prior_id = _extract_order_id_from_history(history)
        if prior_id:
            order_match = type('M', (), {'group': lambda self, n: prior_id})()

    # Cancel order
    if order_match and has_cancel:
        order_id = order_match.group(0).upper()
        result = tools.execute_tool("cancel_order", {"order_id": order_id, "reason": message}, ctx)
        invocations.append(ToolInvocation(name="cancel_order", arguments={"order_id": order_id}, result=result))
        msg_txt = result.get("message", "N/A")
        ok = result.get("ok", False)
        blocks.append(
            f"Cancellation Status: {'Successful' if ok else 'Failed'}\n"
            f"Cancellation Message: {msg_txt}"
        )

    # Refund with known order ID
    elif order_match and has_refund:
        order_id = order_match.group(0).upper()
        result = tools.execute_tool("lookup_order", {"order_id": order_id}, ctx)
        invocations.append(ToolInvocation(name="lookup_order", arguments={"order_id": order_id}, result=result))
        blocks.append(_fmt_order(result))
        refund_result = tools.execute_tool("process_refund", {"order_id": order_id, "reason": message}, ctx)
        invocations.append(ToolInvocation(name="process_refund", arguments={"order_id": order_id, "reason": message}, result=refund_result))
        blocks.append(
            f"Refund ID: {refund_result.get('refund_id', 'N/A')}\n"
            f"Refund Amount: {refund_result.get('amount', 'N/A')}\n"
            f"Refund Status: {refund_result.get('status', 'N/A')}"
        )

    # Order lookup
    elif order_match:
        order_id = order_match.group(0).upper()
        result = tools.execute_tool("lookup_order", {"order_id": order_id}, ctx)
        invocations.append(ToolInvocation(name="lookup_order", arguments={"order_id": order_id}, result=result))
        blocks.append(_fmt_order(result))

    # Policy question — always search if keywords match (even with order ID)
    if any(k in msg_lower for k in _POLICY_KEYWORDS):
        # For short follow-ups, enrich the query with prior user message so RAG
        # finds the right context (e.g. "what if i opened the box" after return question)
        policy_query = message
        if len(message.split()) <= 12 and history:
            prior_user = next(
                (m.get("content", "") for m in reversed(history[-6:]) if m.get("role") == "user"),
                ""
            )
            if prior_user:
                policy_query = f"{prior_user} {message}"
        result = tools.execute_tool("search_policy_knowledge", {"query": policy_query, "top_k": 3}, ctx)
        invocations.append(ToolInvocation(name="search_policy_knowledge", arguments={"query": message}, result=result))
        for r in result.get("results", []):
            blocks.append(f"Policy ({r.get('topic','?')} - {r.get('section','?')}): {r.get('text','')}")

    # Product search
    if any(k in msg_lower for k in _PRODUCT_KEYWORDS):
        result = tools.execute_tool("search_product_knowledge", {"query": message, "top_k": 4}, ctx)
        invocations.append(ToolInvocation(name="search_product_knowledge", arguments={"query": message}, result=result))
        for r in result.get("results", []):
            blocks.append(f"- {r.get('title','?')} | {r.get('category','?')} | ${r.get('price',0)} | {r.get('stars',0)} stars")

    # Account info
    if any(k in msg_lower for k in _ACCOUNT_KEYWORDS):
        result = tools.execute_tool("get_account_info", {}, ctx)
        invocations.append(ToolInvocation(name="get_account_info", arguments={}, result=result))
        blocks.append(
            f"Customer Name: {result.get('name', 'N/A')}\n"
            f"Account Type: {result.get('tier', 'N/A')}\n"
            f"Member Since: {result.get('member_since', 'N/A')}\n"
            f"Open Orders: {result.get('open_orders', 0)}\n"
            f"Total Orders: {result.get('total_orders', 0)}"
        )

    # ── Order context carryforward ────────────────────────────────────────────
    if (not any(inv.name == "lookup_order" for inv in invocations)
            and ctx.customer_id
            and history
            and _ORDER_FOLLOWUP_RE.search(message)):
        prior_id = _extract_order_id_from_history(history)
        if prior_id:
            result = tools.execute_tool("lookup_order", {"order_id": prior_id}, ctx)
            invocations.append(ToolInvocation(
                name="lookup_order",
                arguments={"order_id": prior_id},
                result=result,
            ))
            blocks.append(_fmt_order(result))

    # ── Product context carryforward ──────────────────────────────────────────
    # If the user is refining a product search (no product keywords in this message)
    # but there was a prior product query in history, re-run with the combined query.
    # Guard: suppress when recent assistant messages look like order status replies.
    if (not any(inv.name == "search_product_knowledge" for inv in invocations)
            and history
            and _PRODUCT_FOLLOWUP_RE.search(message)
            and not _in_order_mode(history)):
        last_query = _extract_last_product_query(history)
        if last_query:
            combined_query = last_query + " " + message
            result = tools.execute_tool(
                "search_product_knowledge",
                {"query": combined_query, "top_k": 4},
                ctx,
            )
            invocations.append(ToolInvocation(
                name="search_product_knowledge",
                arguments={"query": combined_query},
                result=result,
            ))
            for r in result.get("results", []):
                blocks.append(
                    f"- {r.get('title','?')} | {r.get('category','?')} | "
                    f"${r.get('price',0)} | {r.get('stars',0)} stars"
                )

    # ── Refund context carryforward ───────────────────────────────────────────
    # If a refund was processed earlier in the session and this is a follow-up
    # question about it, re-run process_refund lookup so the LLM has the data.
    if (not any(inv.name == "process_refund" for inv in invocations)
            and history
            and _REFUND_FOLLOWUP_RE.search(message)
            and not _ORDER_ID_RE.search(message)):  # avoid double-firing on new requests
        prior_order_id = _extract_prior_refund_order_id(history)
        if prior_order_id:
            refund_result = tools.execute_tool(
                "process_refund",
                {"order_id": prior_order_id, "reason": "follow-up status check"},
                ctx,
            )
            invocations.append(ToolInvocation(
                name="process_refund",
                arguments={"order_id": prior_order_id},
                result=refund_result,
            ))
            blocks.append(
                f"Refund ID: {refund_result.get('refund_id', 'N/A')}\n"
                f"Refund Amount: {refund_result.get('amount', 'N/A')}\n"
                f"Refund Status: {refund_result.get('status', 'N/A')}"
            )

    context_block = "\n".join(blocks)
    return invocations, context_block



_STATIC_PLACEHOLDERS = {
    "website_url": "website",
    "online_company_portal_info": "website",
    "online_order_interaction": "Your Orders",
    "customer_support_hours": "business hours",
    "customer_support_phone_number": "our contact page",
    "company_name": "our platform",
}


def _fill_placeholders(text: str, invocations: "List[ToolInvocation]") -> str:
    """Substitute {{placeholder}} tokens with real values from tool results."""
    values: dict = dict(_STATIC_PLACEHOLDERS)
    for inv in invocations:
        r = inv.result or {}
        if inv.name in {"lookup_order", "cancel_order", "process_refund"} and r.get("order_id"):
            values["order_number"] = r["order_id"]
            values["order_id"] = r["order_id"]
        if inv.name == "lookup_order":
            carrier = r.get("carrier", "")
            values.update({
                "status": r.get("status", ""),
                "carrier": carrier,
                "shipping_method": carrier,
                "shipping_carrier": carrier,
                "tracking": r.get("tracking", ""),
                "tracking_number": r.get("tracking", ""),
                "eta": r.get("eta", ""),
                "delivery_date": r.get("eta", ""),
                "expected_delivery": r.get("eta", ""),
                "total": r.get("total", ""),
            })
        elif inv.name == "process_refund":
            values.update({
                "refund_id": r.get("refund_id", ""),
                "amount": r.get("amount", ""),
                "refund_amount": r.get("amount", ""),
            })
        elif inv.name == "cancel_order":
            values["cancel_status"] = "successful" if r.get("ok") else "failed"
        elif inv.name == "get_account_info":
            tier = r.get("tier", "")
            name = r.get("name", "")
            since = r.get("member_since", "")
            values.update({
                "name": name,
                "customer_name": name,
                "tier": tier,
                "account_type": tier,
                "account_tier": tier,
                "membership_level": tier,
                "membership_type": tier,
                "membership": tier,
                "member_since": since,
                "membership_since": since,
                "open_orders": str(r.get("open_orders", "")),
                "total_orders": str(r.get("total_orders", "")),
            })

    def _sub(m: re.Match) -> str:
        key = m.group(1).strip().lower().replace(" ", "_")
        val = values[key] if key in values else m.group(1).strip().lower()
        # Avoid "ORD-ORD-12345" when model outputs "ORD-{{Order Number}}"
        prefix = text[max(0, m.start() - 4): m.start()]
        if prefix.upper().endswith("ORD-") and isinstance(val, str) and val.upper().startswith("ORD-"):
            val = val[4:]
        return str(val)

    return re.sub(r'\{\{([^}]+)\}\}', _sub, text)


_STALL_RE = re.compile(
    r'\b(let me (check|look|find|gather|pull|verify|search)|'
    r'allow me (a moment|to check|to look|to verify)|'
    r'(I|I\'ll) (need to|would need to|will need to|am going to) (check|look|verify|gather|pull)|'
    r'please (hold|wait|give me a moment)|'
    r'I\'m (checking|looking|pulling|verifying)|'
    r'I (don\'t|do not) have that information)\b',
    re.IGNORECASE,
)

_PLEASANTRY_RE = re.compile(
    r'\b(thank(s| you)|appreciate|great|awesome|perfect|wonderful|'
    r'that\'s all|that is all|no more|all good|you\'ve been|you have been)\b',
    re.IGNORECASE,
)
_GREETING_RE = re.compile(
    r'^\s*(hey|hi|hello|good\s+(morning|afternoon|evening)|howdy|hiya|sup|greetings)\s*[!.?]?\s*$',
    re.IGNORECASE,
)
_ORDER_INTENT_RE = re.compile(
    r'\b(my order|order details?|order status|order info(rmation)?|check (?:my )?order|'
    r'see (?:my )?order|get (?:my )?order|order summary|recent order|last order|'
    r'order number|what.*order|order.*detail|about (?:my )?order|view (?:my )?order)\b',
    re.IGNORECASE,
)


def _template_from_invocations(invocations: "List[ToolInvocation]") -> str:
    """Deterministic fallback when LLM stalls: build response directly from tool results."""
    parts = []
    for inv in invocations:
        r = inv.result or {}
        if inv.name == "lookup_order":
            if r.get("found"):
                oid = r.get("order_id", "your order")
                items = r.get("items", [])
                item_str = ", ".join(f"{i.get('title','Item')} x{i.get('qty',1)}" for i in items)
                parts.append(
                    f"Here's the latest on **{oid}**: "
                    f"Status is **{r.get('status','N/A')}**, "
                    f"shipped via {r.get('carrier','N/A')} "
                    f"(tracking: `{r.get('tracking','N/A')}`), "
                    f"estimated delivery **{r.get('eta','N/A')}**."
                    + (f" Items: {item_str}." if item_str else "")
                )
            else:
                parts.append(f"I couldn't find order {r.get('order_id','requested')} in our system.")
        elif inv.name == "process_refund":
            if r.get("ok"):
                parts.append(
                    f"Your refund of **{r.get('amount','N/A')}** has been initiated "
                    f"(Refund ID: {r.get('refund_id','N/A')}). "
                    f"Status: **{r.get('status','processing')}**. "
                    "You'll receive a confirmation email shortly."
                )
            else:
                parts.append(
                    f"We weren't able to process the refund at this time. "
                    f"{r.get('message','Please contact our support team for assistance.')}"
                )
        elif inv.name == "cancel_order":
            if r.get("ok"):
                parts.append(f"Your order has been **successfully cancelled**. {r.get('message','')}")
            else:
                parts.append(
                    f"We're unable to cancel this order. "
                    f"{r.get('message','It may have already shipped — you can return it once delivered.')}"
                )
        elif inv.name == "get_account_info":
            name = r.get("name", "")
            tier = r.get("tier", "")
            since = r.get("member_since", "")
            open_o = r.get("open_orders", 0)
            total_o = r.get("total_orders", 0)
            if name or tier:
                parts.append(
                    f"Here's your account summary, **{name}**: "
                    f"you're a **{tier}** member since {since}, "
                    f"with {open_o} open order(s) and {total_o} total orders."
                )
    if parts:
        return " ".join(parts) + " Is there anything else I can help you with?"
    return ""


def _reply_uses_order_data(reply: str, invocations: "List[ToolInvocation]") -> bool:
    """Return True if the reply contains at least one concrete value from lookup_order."""
    for inv in invocations:
        if inv.name != "lookup_order":
            continue
        r = inv.result or {}
        if not r.get("found"):
            return True  # not-found message is fine as-is
        for key in ("tracking", "eta", "status", "carrier"):
            val = str(r.get(key, "")).strip()
            if val and val.lower() not in ("n/a", "none", "") and val in reply:
                return True
    return False


def _reply_uses_account_data(reply: str, invocations: "List[ToolInvocation]") -> bool:
    """Return True if the reply contains at least one concrete value from get_account_info."""
    for inv in invocations:
        if inv.name != "get_account_info":
            continue
        r = inv.result or {}
        for key in ("tier", "name", "member_since"):
            val = str(r.get(key, "")).strip()
            if val and val.lower() not in ("n/a", "none", "") and val.lower() in reply.lower():
                return True
    return False


_BOILERPLATE_RE = re.compile(
    r'\s*(If you have any (further |specific |additional |other )?'
    r'(questions?|concerns?|issues?|inquiries?|details?|information)'
    r'.*?$'
    r'|please (don\'t hesitate|feel free) to (ask|reach out|contact).*?$'
    r'|They will be able to provide.*?$'
    r'|please (reach out|contact) (our|the) (customer )?support team.*?$'
    r'|I\'m here to help.*?$)',
    re.IGNORECASE | re.DOTALL,
)

_FILLER_START_RE = re.compile(
    r'^(I (appreciate|see what you mean|understand|recognize) (your (message|concern|request|question)'
    r'|that you|what you)[^.]*\.\s*'
    r'|I see that you [^.]*\.\s*'
    r'|It\'s clear to me that [^.]*\.\s*'
    r'|It appears that you [^.]*\.\s*'
    r'|Your message indicates that [^.]*\.\s*'
    r'|Thank you for (reaching out|contacting|your message)[^.]*\.\s*'
    r'|I realized that you [^.]*\.\s*'
    r'|I can see that you [^.]*\.\s*'
    r'|I notice that you [^.]*\.\s*)',
    re.IGNORECASE,
)


def _trim_to_sentences(text: str, max_sentences: int = 4) -> str:
    """Cut response to max_sentences, preserving markdown lists as one unit."""
    # If response is a numbered/bulleted list, keep it as-is (it's already structured)
    if re.search(r'^\s*\d+\.|\s*[-•*]\s', text, re.MULTILINE):
        # Still strip boilerplate paragraph at the end
        text = _BOILERPLATE_RE.sub('', text).strip()
        return text
    # Split on sentence boundaries
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    trimmed = ' '.join(sentences[:max_sentences])
    return trimmed


def _clean_reply(text: str) -> str:
    text = re.sub(r'\[TOOL_CALL\].*?(\}|\n|$)', '', text, flags=re.DOTALL)
    text = re.sub(r'\[TOOL_RESULT\].*?(\}|\n|$)', '', text, flags=re.DOTALL)
    text = text.strip()
    # Strip filler opening sentence ("I appreciate your message! It's clear to me that...")
    text = _FILLER_START_RE.sub('', text).strip()
    # Strip boilerplate closing ("If you have any questions, contact support...")
    text = _BOILERPLATE_RE.sub('', text).strip()
    # Hard cap at 4 sentences
    text = _trim_to_sentences(text, max_sentences=4)
    return text


def _messages_to_phi4_prompt(messages: List[dict]) -> str:
    parts = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "system":
            parts.append(f"<|system|>\n{content}<|end|>")
        elif role == "user":
            parts.append(f"<|user|>\n{content}<|end|>")
        elif role == "assistant":
            parts.append(f"<|assistant|>\n{content}<|end|>")
    parts.append("<|assistant|>")
    return "\n".join(parts)


def _call_hf(messages: List[dict], max_new_tokens: int = 256) -> str:
    """Call Hugging Face Inference Endpoint (Phi-4-mini)."""
    settings = get_settings()
    prompt = _messages_to_phi4_prompt(messages)
    try:
        resp = httpx.post(
            settings.hf_endpoint_url,
            headers={"Authorization": f"Bearer {settings.hf_token}"},
            json={
                "inputs": prompt,
                "parameters": {
                    "max_new_tokens": max_new_tokens,
                    "temperature": 0.1,
                    "return_full_text": False,
                },
            },
            timeout=settings.llm_timeout_seconds,
        )
        if not resp.is_success:
            log.error("HF 4xx body: %s", resp.text[:500])
        resp.raise_for_status()
        result = resp.json()
        if isinstance(result, list):
            return result[0].get("generated_text", "")
        return result.get("generated_text", "")
    except Exception as exc:
        log.error("HF endpoint error: %s", exc)
        raise


def _call_groq(messages: List[dict], max_tokens: int = 300) -> str:
    """Call Groq's OpenAI-compatible chat completions API with automatic key fallback."""
    settings = get_settings()
    keys = [settings.groq_api_key]
    if settings.groq_api_key_fallback:
        keys.append(settings.groq_api_key_fallback)

    last_exc = None
    for i, api_key in enumerate(keys):
        try:
            resp = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.llm_model,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": max_tokens,
                },
                timeout=settings.llm_timeout_seconds,
            )
            if not resp.is_success:
                log.error("Groq key #%d error: %s", i + 1, resp.text[:500])
                resp.raise_for_status()
            result = resp.json()
            return result["choices"][0]["message"]["content"]
        except Exception as exc:
            last_exc = exc
            if i < len(keys) - 1:
                log.warning("Groq key #%d failed, trying fallback key: %s", i + 1, exc)
            else:
                log.error("All Groq keys failed: %s", exc)
    raise last_exc


def _call_llm(messages: List[dict], max_tokens: int = 300) -> str:
    """Route to the configured LLM provider."""
    settings = get_settings()
    if settings.llm_provider == "hf":
        return _call_hf(messages, max_new_tokens=max_tokens)
    return _call_groq(messages, max_tokens=max_tokens)


def _augment_system(base: str, sentiment: str, cumulative: str) -> str:
    extras = []
    if cumulative == "frustrated":
        extras.append(
            "CRITICAL: This customer has been frustrated across multiple turns. "
            "Lead with sincere acknowledgement and resolve as fast as possible."
        )
    elif cumulative == "negative" or sentiment in {"negative", "frustrated"}:
        extras.append("Note: The customer is unhappy. Be extra empathetic and solution-focused.")
    if not extras:
        return base
    return base + "\n\n" + "\n".join(extras)


def run_agent(
    *,
    user_message: str,
    history: List[dict],
    sentiment: str,
    cumulative: str,
    ctx: tools.ToolContext,
) -> Tuple[str, List[ToolInvocation]]:
    # Step 1: deterministically call tools based on message content
    invocations, tool_context = _route_tools(user_message, ctx, history)

    # Escalation intent — fire the tool to create a ticket + send email, then respond
    if _detect_intent(user_message) == "escalate_to_human":
        esc_result = tools.execute_tool(
            "escalate_to_human",
            {"reason": user_message, "priority": "high"},
            ctx,
        )
        invocations.append(ToolInvocation(
            name="escalate_to_human",
            arguments={"reason": user_message, "priority": "high"},
            result=esc_result,
        ))
        ticket_id = esc_result.get("ticket_id", "")
        wait = esc_result.get("estimated_wait", "a few minutes")
        return (
            f"I completely understand — I've opened ticket **{ticket_id}** and a human support agent "
            f"is being notified right now. Estimated wait: **{wait}**. "
            "They'll have full context of our conversation. A confirmation email has been sent to you."
        ), invocations

    # If nothing was retrieved, skip the LLM — it will hallucinate without grounding
    if not invocations:
        if _GREETING_RE.search(user_message):
            return (
                "Hi there! Welcome to Atlas support. How can I help you today?"
            ), invocations
        if _ORDER_INTENT_RE.search(user_message):
            return (
                "Sure! Could you share your order number? "
                "It starts with ORD- and you'll find it in your confirmation email."
            ), invocations
        if _PLEASANTRY_RE.search(user_message):
            return (
                "You're very welcome! I'm glad I could help. "
                "If you ever need anything else, don't hesitate to reach out. Have a great day!"
            ), invocations
        if sentiment == "frustrated" or cumulative == "frustrated":
            return (
                "I'm really sorry you're having this experience — that's completely understandable. "
                "I want to make sure you get the right help. Let me connect you with a member of our "
                "support team who can look into this personally. Please hold on."
            ), invocations
        return (
            "I don't have specific information about that in our system. "
            "For detailed help, please contact our support team directly or visit our help center."
        ), invocations

    # Step 2: build prompt — inject tool results so model only needs to write the response
    system = _augment_system(SYSTEM_PROMPT, sentiment, cumulative)

    readable_context = tool_context
    if readable_context:
        augmented_user = (
            f"{user_message}\n\n"
            f"Here is the verified data from our systems. Use ONLY these facts in your reply:\n{readable_context}"
        )
    else:
        augmented_user = user_message

    messages: List[dict] = [{"role": "system", "content": system}]
    for h in history[-8:]:
        role = h.get("role")
        content = h.get("content") or ""
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": augmented_user})

    reply = _call_llm(messages, max_tokens=180)
    reply = _fill_placeholders(reply, invocations)
    reply = _clean_reply(reply)

    # Safety net 1: LLM explicitly stalls ("let me check", "please allow me", etc.)
    if _STALL_RE.search(reply):
        template = _template_from_invocations(invocations)
        if template:
            log.info("LLM stall detected — using template response")
            return template, invocations

    # Safety net 2: LLM called lookup_order but reply contains none of the actual data values
    has_order_lookup = any(inv.name == "lookup_order" for inv in invocations)
    if has_order_lookup and not _reply_uses_order_data(reply, invocations):
        template = _template_from_invocations(invocations)
        if template:
            log.info("LLM ignored order data — using template response")
            # Append return guidance when this is a cancel follow-up on a shipped order
            lookup_inv_sn2 = next((inv for inv in invocations if inv.name == "lookup_order"), None)
            if (lookup_inv_sn2
                    and "cancel" in user_message.lower()
                    and lookup_inv_sn2.result.get("status", "").lower() == "shipped"
                    and "return" not in template.lower()):
                template += (
                    " Since your order has already shipped, cancellation isn't possible. "
                    "You can return it once it's delivered — just contact us to initiate a return within our standard return window."
                )
            return template, invocations

    # Safety net 3: LLM called get_account_info but reply contains none of the account values
    has_account = any(inv.name == "get_account_info" for inv in invocations)
    if has_account and not _reply_uses_account_data(reply, invocations):
        template = _template_from_invocations(invocations)
        if template:
            log.info("LLM ignored account data — using template response")
            return template, invocations

    # Safety net 4: cancel_order failed but LLM didn't mention the return path
    cancel_inv = next((inv for inv in invocations if inv.name == "cancel_order"), None)
    if cancel_inv and not cancel_inv.result.get("ok"):
        if not any(w in reply.lower() for w in ("shipped", "return", "deliver")):
            template = _template_from_invocations(invocations)
            if template:
                log.info("Cancel failed — LLM omitted return guidance, using template")
                return template, invocations

    # Safety net 5: process_refund was called but LLM reply doesn't include the
    # Refund ID. Require "refund id" (not just "refund") so that the stored
    # assistant message always contains a reliable marker that _looks_like_refund_processed
    # can detect in subsequent turns.
    has_refund_inv = any(inv.name == "process_refund" for inv in invocations)
    if has_refund_inv and "refund id" not in reply.lower():
        template = _template_from_invocations(invocations)
        if template:
            log.info("LLM omitted Refund ID — using template")
            return template, invocations

    # Post-process: follow-up about a failed cancel earlier in this session —
    # check history so this works even when no lookup_order fired this turn.
    if "cancel" in user_message.lower() and "return" not in reply.lower():
        for msg in reversed((history or [])[-5:]):
            if msg.get("role") == "assistant":
                prev = (msg.get("content") or "").lower()
                if ("unable to cancel" in prev
                        or "already shipped" in prev
                        or ("cancel" in prev and "shipped" in prev)):
                    reply += (
                        " Since your order has already shipped, cancellation isn't possible. "
                        "You can return it once it's delivered — just contact us to initiate a return within our standard return window."
                    )
                    break

    # Post-process: cancel in the current turn on a freshly-looked-up shipped order
    lookup_inv = next((inv for inv in invocations if inv.name == "lookup_order"), None)
    if (lookup_inv
            and "cancel" in user_message.lower()
            and lookup_inv.result.get("status", "").lower() == "shipped"
            and "return" not in reply.lower()):
        reply += (
            " Since your order has already shipped, cancellation isn't possible at this stage. "
            "However, you can return it once it's delivered — just initiate a return within our standard return window."
        )

    # ── Empathy prefix for unhappy customers ─────────────────────────────────
    if reply and sentiment in {"frustrated", "negative"} or cumulative == "frustrated":
        _EMPATHY_PREFIXES = {
            "frustrated": "I'm truly sorry for the frustration — I understand how important this is to you. ",
            "negative": "I'm sorry you're having this experience — let me help make it right. ",
        }
        prefix = _EMPATHY_PREFIXES.get(sentiment, _EMPATHY_PREFIXES.get(cumulative, ""))
        # Only prepend if reply doesn't already start with an apology
        if prefix and not any(reply.lower().startswith(w) for w in ("i'm sorry", "i'm truly", "i apologize", "my apologies")):
            reply = prefix + reply

    return reply or "I'm here to help — could you clarify what you need?", invocations
