from __future__ import annotations

import re
from typing import Optional

from sqlalchemy.orm import Session as DbSession

from .models import Customer, Order
from .models import Session as ChatSession

_EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
_ORDER_RE = re.compile(r'\bORD-[\w-]+\b', re.IGNORECASE)

# Phrases (not single words) that indicate a personal/account-specific question
_NEEDS_ID_PHRASES = {
    'my order', 'my orders', 'my package', 'my item', 'my account',
    'my refund', 'my delivery', 'my membership', 'my tier', 'my purchase',
    'my payment', 'my billing', 'my invoice', 'my shipment',
    'i ordered', 'i bought', 'i purchased',
    'order status', 'order number',
    'track my', 'cancel my', 'return my', 'refund my',
    'where is my', 'where is it', 'when will my',
    'how many orders', 'my order history', 'open orders', 'total orders',
}
_NO_ID_INTENTS = {
    'product_inquiry', 'shipping_options', 'check_stock',
    'discount_inquiry', 'general_inquiry', 'compliment',
}


def _extract_email(text: str) -> Optional[str]:
    m = _EMAIL_RE.search(text)
    return m.group(0).lower() if m else None


def _extract_order_id(text: str) -> Optional[str]:
    m = _ORDER_RE.search(text)
    return m.group(0).upper() if m else None


def _extract_order_from_history(history: list) -> Optional[str]:
    """Scan recent user messages for an order ID."""
    for msg in reversed(history[-8:]):
        if msg.get("role") == "user":
            oid = _extract_order_id(msg.get("content", ""))
            if oid:
                return oid
    return None


def _bot_is_waiting_for_email(history: list) -> bool:
    """True if the last bot message was requesting email for identity verification."""
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            c = msg.get("content", "").lower()
            return "email" in c and ("confirm" in c or "account" in c or "verify" in c or "identity" in c)
    return False


def _needs_identification(message: str, history: list) -> bool:
    """Return True if this message requires the customer to be identified first."""
    from .sentiment import detect_intent

    # Mid-conversation with an order already referenced → always need ID
    if _extract_order_from_history(history):
        return True

    msg_lower = message.lower()

    # Policy/general questions should NEVER require ID, even if bot previously asked for email.
    # A user pivoting from a personal query to "what's your return policy?" should get an answer.
    _is_policy_question = any(kw in msg_lower for kw in (
        'policy', 'policies', 'warranty', 'guarantee', 'how long', 'how do i',
        'what is your', 'what are your', 'do you offer', 'do you have',
        'what if', 'can i', 'is it possible',
        'what do i need', 'how to', 'steps to', 'how does', 'what happens',
        'start a return', 'initiate a return', 'begin a return', 'process a return',
        'opened', 'open box', 'condition', 'seal', 'packaging',
    ))

    # Bot explicitly asked for email — treat the current message as an identification response
    # UNLESS it's clearly a general/policy question (user changed topic).
    if _bot_is_waiting_for_email(history) and not _is_policy_question:
        return True

    # Proactive credential submission: user sends their email without being prompted.
    # This handles "My email is alice@demo.atlas" or just "alice@demo.atlas" as first message.
    # Safe because: (a) policy questions are already excluded above, (b) if the email isn't
    # in the DB the handler returns a "couldn't find account" message, not an error.
    if _extract_email(message) and not _is_policy_question:
        return True

    # Explicit order ID in message → definitely needs ID
    if _ORDER_RE.search(message):
        return True

    # Policy / general info questions never need ID even if they contain personal keywords
    if _is_policy_question:
        return False

    # Escalation intent never needs ID — route directly to human handoff
    intent = detect_intent(message)
    if intent == 'escalate_to_human':
        return False

    # Personal phrases → needs ID
    if any(phrase in msg_lower for phrase in _NEEDS_ID_PHRASES):
        return True

    # Fall back to intent for remaining cases
    if intent in _NO_ID_INTENTS:
        return False

    # Intents that clearly need personal context — but only when the message
    # is framed personally (has "my", "I want/need/have"). A question like
    # "what is the return window?" or "noise cancelling earbuds" should NOT
    # trigger the ID gate even if the intent classifier fires on a substring.
    # Use specific phrases — bare "i have" matches "do i have to" (false positive)
    _PERSONAL_INDICATORS = {'my ', 'i want', 'i need', "i'd like", 'i would like',
                             'i have my', 'i have an order', "i've", 'i ordered',
                             'i bought', 'i purchased', 'i placed'}
    msg_is_personal = any(ind in msg_lower for ind in _PERSONAL_INDICATORS)
    if intent in {'track_order', 'cancel_order', 'return_item', 'refund_request',
                  'delivery_problem', 'wrong_item', 'missing_item', 'account_access',
                  'payment_issue', 'product_defect'} and msg_is_personal:
        return True

    return False


def handle_identification(
    message: str,
    chat_session: ChatSession,
    history: list,
    db: DbSession,
) -> Optional[str]:
    """
    Drive the customer identification flow before handing off to the agent.

    Returns a response string while the flow is in progress.
    Returns None when identification is complete or not needed — let the agent run.
    """
    # Already identified — nothing to do
    if chat_session.customer_id:
        return None

    # General questions (policy, product search, FAQs) — no ID required
    if not _needs_identification(message, history):
        return None

    email = _extract_email(message)
    # Order ID can come from the current message or a recent user message in history
    order_id = _extract_order_id(message) or _extract_order_from_history(history)

    # ── Case 1: have both email and order ID ──────────────────────────────────
    if email and order_id:
        order = db.get(Order, order_id)
        if not order:
            return (
                f"I couldn't find order **{order_id}** in our system. "
                "Could you double-check the order number?"
            )
        customer = db.get(Customer, order.customer_id)
        if customer and customer.email.lower() == email.lower():
            chat_session.customer_id = customer.id
            db.flush()
            return (
                f"Identity confirmed — welcome, **{customer.name}**! "
                f"You're a **{customer.tier}** member. "
                f"What would you like to know about order **{order_id}**?"
            )
        else:
            return (
                "I wasn't able to verify that order with the email you provided. "
                "For security reasons I can't share order details that don't match your account. "
                "Please double-check, or contact our support team for further help."
            )

    # ── Case 2: email only ────────────────────────────────────────────────────
    if email:
        customer = db.query(Customer).filter(Customer.email == email).first()
        if customer:
            chat_session.customer_id = customer.id
            db.flush()
            # Identified — welcome them and ask what they need
            return (
                f"Got it, **{customer.name}**! I've verified your account "
                f"({customer.tier} member). "
                "Which order can I help you with, or what else can I do for you today?"
            )
        else:
            return (
                f"I couldn't find an account with **{email}**. "
                "Could you double-check the email address? "
                "Or if you have your order number handy, share that and I'll look it up."
            )

    # ── Case 3: order ID only — need email confirmation ───────────────────────
    if order_id:
        order = db.get(Order, order_id)
        if order:
            return (
                f"I can see that order in our system. "
                "To confirm it's you, could you share the email address on your account?"
            )
        else:
            return (
                f"I couldn't find order **{order_id}** in our system. "
                "Could you double-check the order number?"
            )

    # ── Case 4: nothing to work with ─────────────────────────────────────────
    return (
        "I'd be happy to help! To look up your order or account details, "
        "I'll need to verify your identity first. "
        "What's the email address on your account?"
    )
