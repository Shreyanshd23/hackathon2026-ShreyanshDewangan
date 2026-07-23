"""OpenAI-compatible tool schemas advertised to the LLM (Groq tool-calling)."""

TOOL_DEFINITIONS = [
    {"type": "function", "function": {
        "name": "get_order",
        "description": "Look up an order by its ID. Returns status, dates, amount, refund status, and notes.",
        "parameters": {"type": "object", "properties": {
            "order_id": {"type": "string", "description": "The order ID, e.g. ORD-1001"}},
            "required": ["order_id"]}}},
    {"type": "function", "function": {
        "name": "get_customer",
        "description": "Look up a customer by email. Returns profile, tier, order history summary, and notes.",
        "parameters": {"type": "object", "properties": {
            "email": {"type": "string", "description": "Customer email address"}},
            "required": ["email"]}}},
    {"type": "function", "function": {
        "name": "get_product",
        "description": "Look up a product by ID. Returns name, category, price, warranty months, return window, notes.",
        "parameters": {"type": "object", "properties": {
            "product_id": {"type": "string", "description": "Product ID, e.g. P001"}},
            "required": ["product_id"]}}},
    {"type": "function", "function": {
        "name": "search_knowledge_base",
        "description": "Search ShopWave's support knowledge base for policies, FAQs, and procedures.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Search query, e.g. 'return policy for electronics'"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "check_refund_eligibility",
        "description": "Check whether an order is eligible for a refund. Returns eligibility, reason, refundable amount.",
        "parameters": {"type": "object", "properties": {
            "order_id": {"type": "string", "description": "Order ID to check"}},
            "required": ["order_id"]}}},
    {"type": "function", "function": {
        "name": "issue_refund",
        "description": "Issue a refund. Only call AFTER confirming eligibility with check_refund_eligibility.",
        "parameters": {"type": "object", "properties": {
            "order_id": {"type": "string", "description": "Order ID to refund"},
            "amount": {"type": "number", "description": "Refund amount in USD"}},
            "required": ["order_id", "amount"]}}},
    {"type": "function", "function": {
        "name": "send_reply",
        "description": "Send a reply message to the customer. Standard way to resolve or respond to a ticket.",
        "parameters": {"type": "object", "properties": {
            "ticket_id": {"type": "string", "description": "Ticket ID, e.g. TKT-001"},
            "message": {"type": "string", "description": "Professional, empathetic reply addressing customer by first name"}},
            "required": ["ticket_id", "message"]}}},
    {"type": "function", "function": {
        "name": "escalate",
        "description": "Escalate ticket to a human. Use for warranty claims, replacement requests, fraud, refunds >$200, or low-confidence situations.",
        "parameters": {"type": "object", "properties": {
            "ticket_id": {"type": "string", "description": "Ticket ID to escalate"},
            "summary": {"type": "string", "description": "Concise summary: issue, what was verified, recommended resolution"},
            "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"], "description": "Escalation priority"}},
            "required": ["ticket_id", "summary", "priority"]}}},
]
