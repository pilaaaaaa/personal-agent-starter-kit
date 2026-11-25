# Decision methods

| Method | When to use |
| --- | --- |
| [Constraint-based Planning](constraint-based-planning.md) | Fixed commitments, scarce resources or conditional preferences |
| [Critical Path / Dependency Analysis](critical-path-dependency-analysis.md) | Success depends on predecessors or external replies |
| [Cost of Delay](cost-of-delay.md) | Actions compete for a feasible window |
| [Value of Information](value-of-information.md) | An unknown could change the decision |
| [Multi-objective Optimization](multi-objective-optimization.md) | Meaningful trade-offs across feasible options |
| [Receding Horizon Planning](receding-horizon-planning.md) | Progress, context or external replies can change |
| [Task Compatibility](task-compatibility.md) | Potential parallel work, batching or shared trips |
| [Decision Delegation](decision-delegation.md) | An action may require a user decision or execution permission |
| [Goal Substitution](goal-substitution.md) | An existing path is blocked, redundant or expensive |
| [Preference Learning](preference-learning.md) | User feedback could improve future plans |
| [Goal Decomposition](goal-decomposition.md) | A goal is abstract, stalled or lacks a next step |
| [Workflow Optimization](workflow-optimization.md) | Activities contain waiting, device time or shared resources |

Discover Markdown modules in this directory. Use this index on path-only connectors. Select only enabled modules that change the decision. Missing and unknown statuses are skipped. A module has a stable filename, title, Enabled or Disabled status written as `Status: enabled` or `Status: disabled`, trigger, inputs and reasoning, output and optional combinations. Add or rename links after editing modules. Modules are Agent reasoning guides, and their outputs can feed other modules. No mandatory scoring formula or sequence applies.

For contested plans, remove infeasible options first. Compare the surviving options against the current goal, look for synergy, and reconcile disagreements explicitly. Revisit material assumptions with new evidence. The operating protocol always governs authorization.

Optional workflow method: [Daily review](daily-review.md), when reviewing journal evidence.
