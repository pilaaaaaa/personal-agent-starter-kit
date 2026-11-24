# Workflow Optimization

Status: enabled

## When to use

Activities contain waiting, device time or shared resources.

## Inputs and reasoning

Distinguish active work, unattended processing, waiting and supervision. Estimate durations and earliest/latest starts. Start the critical wait early when feasible, overlap compatible steps and synchronize finishes where helpful. Compare makespan, quality, energy, decision and switching costs. Respect safety and supervision limits. Use ranges and qualify any unverified improvement. Keep microsteps in the proposed plan by default.

## Output

A feasible sequence or relative timeline, critical chain, reused wait, estimated range and replan trigger.

## Suggested combinations

Goal Decomposition, Critical Path, Task Compatibility, Constraint-based Planning, Multi-objective Optimization.
