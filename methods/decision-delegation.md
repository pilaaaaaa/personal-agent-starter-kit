# Decision Delegation

Status: enabled

## When to use

An action may require a user decision or execution permission.

## Inputs and reasoning

Classify by impact and cost of reversal. High-impact irreversible choices belong to the user. Low-impact irreversible choices require confirmation after a concise recommendation. High-impact reversible choices can be recommended with a rollback path. Low-impact reversible choices can use a reasonable default. Check execution authorization separately for every side effect.

## Output

Recommendation, execution boundary and any approval or rollback condition. Offer more alternatives when asking for a decision.

## Suggested combinations

Value of Information, Receding Horizon Planning.
