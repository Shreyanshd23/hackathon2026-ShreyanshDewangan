# Failure Modes & Resilience Analysis 🛡️

This document outlines how the ShopWave AI Support System handles common AI failures and edge cases.

## 1. API Rate Limiting (429 Errors)
**Scenario**: The LLM provider (Groq) limits the number of tokens or requests per minute.
**Handling**: The system uses a **Staggered Concurrency** model.
- Maximum of 2 concurrent threads.
- 10-second delay between ticket starts.
- Automatic exponential backoff (Retries 1, 2, 4, 8s) in the `llm_client.py`.

## 2. Tool Output Ambiguity
**Scenario**: A tool call (e.g., `get_order`) returns data that conflicts with the user's claim.
**Handling**: The **Resolver Agent** is programmed to use **Multi-Step Cross-Referencing**.
- If an Order ID is found but doesn't match the email, the agent will attempt to search by name or escalate to a human rather than hallucinating a resolution.

## 3. Reasoning Loops (Hallucination Prevention)
**Scenario**: An agent gets stuck repeating the same tool call.
**Handling**: **Hard Iteration Caps**.
- Every ticket has a `MAX_TOOL_ITERATIONS` limit of 12.
- If the limit is reached, the ticket is marked as "Incomplete" and flagged for human review, preventing infinite API costs and user frustration.

## 4. Random Tool Failures
**Scenario**: An internal database or API call fails (simulated via `FAILURE_INJECTION_RATE`).
**Handling**: **Graceful Degradation**.
- Agents are instructed to retry the tool once. If it persistently fails, they automatically switch to "Escalation Mode," providing the human agent with a summary of what failed.
