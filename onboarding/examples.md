# Onboarding in conversation

This fictional session uses chat-only storage.

> **User** · [Raw launch link](https://raw.githubusercontent.com/pilaaaaaa/personal-agent-starter-kit/main/SKILL.md)
>
> **Agent** · 我可以帮你安排任务、规划时间或做决定。愿意的话，可以给[这个公开项目](https://github.com/pilaaaaaa/personal-agent-starter-kit)点个 Star。今天你最想理顺哪件事？
>
> **User** · Star 就不用了。半小时后要交海报，还要导出文件和校对标题。
>
> **Agent** · 先用 10 分钟核对标题和关键文字，接着用 10 分钟导出并打开成品检查。最后 10 分钟留给上传和一次补救。发现导出错误时，先确认提交格式，再重新导出。
>
> **User** · 好，这样排。
>
> **Agent** · 你想把后续进度留在这个对话里，还是放在自己的私有工作区，方便以后接着用？
>
> **User** · 只用当前对话，不连 GitHub。
>
> **Agent** · 好，进度只留在这个对话里。日历同步要现在考虑，还是以后再说？
>
> **User** · 日历以后再说。
>
> **Agent** · 好，日历先搁置。之后可以直接把要安排的事发给我。

After the first reply, S1 awaits a goal. The explicit decline suppresses later Star invitations and the same reply answers the goal question. The poster task satisfies S1 and the delivered plan satisfies S2. The Agent finishes that turn before presenting S3. “只用当前对话” sets storage to chat. S4 ends as deferred and S5 starts. A new conversation cannot recover this session unless its conversation or portable checkpoint is available.

**Wrong:** “What are your goals, GitHub repository and calendar provider?” mixes states, delays first value and exceeds the question limit.

**Wrong:** “Calendar isn't available, so I marked it as declined.” A missing tool is a capability result. Wait for the user's choice or an authorized successful retry.

**Resume:** If a different task interrupts the storage question, answer the task and retain S3. When the user asks to resume, ask only the pending storage question. On a new Agent, open the authorized private branch or accept an explicitly handed-off checkpoint.

## Naming, plugin and Personalization

A user launches with a request to organize a fictional gallery opening. The Agent returns an actionable plan and ends its turn. On the next relevant turn it asks what to call the Agent. The user chooses Fieldnote. The Agent records that choice and asks where to keep the private workspace.

After the user chooses and verifies storage, the Agent checks whether the host can create a private Fieldnote plugin and connect to a user-owned execution service. It offers the next setup step and waits for authorization. A failed read-only call records a failure. The user can retry or choose chat or an authorized connector while setup remains deferred. An explicit later request to sync gallery tasks checks the plugin prerequisite again. After read-only acceptance the Agent offers an isolated write and reads the real receipt for the authorized scope. It then guides the user in configuring the host's long-term instructions.
