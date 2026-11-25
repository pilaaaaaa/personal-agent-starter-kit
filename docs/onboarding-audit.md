# Onboarding audit and design

Baseline: public main at `edc0beebf66f2d6b715be5ef5b49d968aea6989d` (0.2.0). The entry already recognized a bare launch link and excluded review requests. Failure could occur earlier if a host never fetched the file. After a successful fetch, “start onboarding” and “help immediately” had no shared continuation rule. The separate onboarding guide delegated timing of storage and integrations to the Agent. AGENTS.md defined daily operations without an onboarding gate. Protocol and update files described revision consistency and private setup, with no persisted onboarding progress. The initializer and manifest copied documentation and blank task/planning files. Existing regression tests checked copied references, task data and integration adapters. No fixture specified a conversational interrupt, transition guard, recovery, refusal or exit.

| Mechanism | Adopted design | Runtime boundary |
| --- | --- | --- |
| [Agent Skills specification](https://agentskills.io/specification) | Valid entry metadata, essential instructions at launch and deeper references on demand. | Native skill activation belongs to the host. Fetching a Raw URL in chat has no native activation guarantee. |
| [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api) and [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) | Explicit guarded states and a saved user-input boundary, with resume from a stable question key. | A LangGraph runtime enforces node and interrupt execution. This kit uses an Agent-followed protocol and Git conditional writes, with no runtime, server or database requirement. |
| [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | Separate resumable state from long-term knowledge and reuse the authorized canonical workspace. | Git stores explicit checkpoints. Unavailable tool access or a failed write leaves session-only progress. |
| [Conversational preference elicitation study](https://arxiv.org/abs/2504.13095) | Elicit only context that affects actual work, adapt question wording, and confirm the scope of durable preferences. | This is a design choice informed by conversational recommender research. Its effect has yet to be measured for this kit. |

JSON provides a human-readable declaration with standard-library parsing. The schema constrains checkpoint shape and selected invariants. The interaction protocol specifies evidence and effects whose truth depends on the conversation or tools. Offline tests can validate the declared contract and curated traces. Platform tests must establish whether real products fetch the file, obey interrupts and expose the appropriate tools.

## State graph

```mermaid
flowchart TD
    Start[Incoming request] -->|Review or ordinary task without launch| Daily[Respond to request]
    Start -->|New launch| S0[ S0 Welcome ]
    Start -->|Authorized saved checkpoint| Restore[Restore saved state and question]
    Start -->|Known workspace inaccessible| Limited[Chat help and unresolved access]
    Start -->|Verified legacy without checkpoint| S5
    S0 -->|Welcomed| S1[S1 Need]
    S1 -->|Goal known| S2[S2 First value]
    S2 -->|Real result delivered and turn ends| S3[S3 Storage]
    S3 -->|Chat chosen, storage verified, declined or deferred| S4[S4 Integrations]
    S4 -->|Scope resolved, declined or deferred| S5[S5 Normal use and learning]
    S3 -->|Reopened storage resolved and S4 already resolved| S5
    S5 -->|User reopens storage| S3
    S5 -->|User reopens integration scope| S4
    S1 -->|Missing goal| S1
    S2 -->|Missing input| S2
    S3 -->|Unanswered decision, retry or failure| S3
    S4 -->|Open module, grant, retry or failure| S4
    Active[S0–S4 active] -->|Unrelated task or explicit pause| Paused[Paused at same checkpoint]
    Active -->|Explicit exit| Exited[Exited and normal work allowed]
    Paused -->|Explicit exit| Exited
    Paused -->|Resume, relaunch or answer pending question| Restore
    Exited -->|Explicit resume or relaunch| Restore
    Restore -->|Retain saved status and location| Active
    Restore -->|Completed| S5
```
