import EventKit
import Foundation

@main
struct SyncRules {
    static func main() throws {
        let oldState = Data(#"{"listID":"home","mapping":{"a":"apple-id"}}"#.utf8)
        var state = try JSONDecoder().decode(State.self, from: oldState)
        precondition(state.mapping["a"] == "apple-id" && state.lastGitHubCompletion == nil)
        state.lastGitHubCompletion = ["a": false]
        let restored = try JSONDecoder().decode(State.self, from: JSONEncoder().encode(state))
        precondition(restored.lastGitHubCompletion == ["a": false])

        // A completed device snapshot from an interrupted projection is its own write.
        let due = TaskFile.Task.Due(date: "2026-11-27", time: nil, timezone: "Europe/Amsterdam")
        let older = TaskFile.Task.Due(date: "2026-11-25", time: nil, timezone: "Europe/Amsterdam")
        let before = ReminderValue(due: older, completed: false, completedAt: nil)
        let after = ReminderValue(due: due, completed: true, completedAt: "2026-11-27T11:00:00Z")
        let pending = PendingReminderWrite(before: before, githubDue: due,
                                           githubCompleted: true, desiredCompleted: true)
        let found = try pending.matches(after)
        let unchanged = try pending.matches(before)
        precondition(found && !unchanged)
        state.pending = ["a": pending]
        let recovered = try JSONDecoder().decode(State.self, from: JSONEncoder().encode(state))
        precondition(recovered.pending?["a"]?.githubCompleted == true)

        let named = try TaskFile.Task.Due(date: "2026-11-27", time: "08:30",
                                         timezone: "Europe/Amsterdam").components()
        var floating = try TaskFile.Task.Due(date: "2026-11-27", time: "07:30",
                                             timezone: "UTC").components()
        floating.timeZone = nil
        precondition(Sync.sameDueDate(floating, named))
        floating.minute = 45
        precondition(!Sync.sameDueDate(floating, named))

        let reminder = EKReminder(eventStore: EKEventStore())
        let historical = Date(timeIntervalSince1970: 1_700_000_000)
        Sync.setCompletion(reminder, completed: true, completedAt: historical)
        precondition(reminder.isCompleted && reminder.completionDate == historical)
        Sync.setCompletion(reminder, completed: false, completedAt: nil)
        precondition(!reminder.isCompleted && reminder.completionDate == nil)

        precondition(Sync.canAdopt(title: "[P1] Mail", notes: nil, listID: "target", taskTitle: "mail", targetID: "target"))
        precondition(!Sync.canAdopt(title: "Mail", notes: nil, listID: "work", taskTitle: "mail", targetID: "target"))
        precondition(!Sync.canAdopt(title: "Mail", notes: "[Personal Agent ID: other]", listID: "target", taskTitle: "mail", targetID: "target"))
        let scheduled = Data(#"{"id":"dst","title":"DST","priority":"P1","status":"todo","start_time":"2026-10-25T02:30:00+02:00","end_time":"2026-10-25T02:15:00+01:00"}"#.utf8)
        let task = try JSONDecoder().decode(TaskFile.Task.self, from: scheduled)
        guard case let .block(start, end) = task.schedule else { preconditionFailure("Expected a DST block") }
        precondition(end.date.timeIntervalSince(start.date) == 45 * 60)
        let statusSnapshot = Data(#"{"schema_version":2,"tasks":[{"id":"a","title":"A","priority":"P1","status":"cancelled"},{"id":"b","title":"B","priority":"P1","status":"done"},{"id":"c","title":"C","priority":"P1","status":"completed"}]}"#.utf8)
        let statusFile = try Sync.decodeSnapshot(statusSnapshot, expectedBlob: nil)
        precondition(statusFile.tasks.filter { $0.status != "cancelled" }.map(\.id) == ["b", "c"])
        precondition(Sync.statuses["done"] == Sync.statuses["completed"])
        try deletionRules()
        print("Passed state migration, completion ownership, first-import matching, schedule validation, deletion and snapshot checks.")
    }
    static func deletionRules() throws {
        func reminder(_ identifier: String, _ taskID: String?, title: String = "Same title") -> Sync.ReminderIdentity {
            .init(identifier: identifier, title: title, notes: taskID.map { Sync.marker(for: $0) }, writable: true)
        }
        let reminders = [
            reminder("apple", "active"), reminder("old", "graduation"), reminder("walk", "walk"),
            reminder("personal", nil), reminder("adopted", "initial"),
            reminder("dup1", "duplicate"), reminder("dup2", "duplicate"),
            reminder("mismatch", "other"), reminder("orphan", "daylight")
        ]
        let active: Set<String> = ["active"]
        var mapping = ["active": "apple", "graduation": "old", "walk": "walk", "initial": "adopted",
                       "duplicate": "dup1", "wrong": "mismatch", "unmarked": "personal"]
        let protected: Set<String> = ["initial"]
        let plan = Sync.deletionPlan(active: active, mapping: mapping, protected: protected, reminders: reminders)
        let deleted = plan.filter { $0.reminderID != nil }
        precondition(deleted.map(\.taskID) == ["graduation", "walk"])
        precondition(Set(plan.filter { $0.reminderID == nil }.map(\.taskID)) ==
                     ["daylight", "duplicate", "initial", "other", "unmarked", "wrong"])
        for entry in deleted { mapping.removeValue(forKey: entry.taskID) }
        let remaining = reminders.filter { item in !deleted.contains { $0.reminderID == item.identifier } }
        let second = Sync.deletionPlan(active: active, mapping: mapping, protected: protected, reminders: remaining)
        precondition(second.allSatisfy { $0.reminderID == nil })

        let good = Data(#"{"schema_version":2,"sync_policy":{"direction":"github_to_apple_reminders","delete_behavior":"delete_verified_managed_reminders"},"tasks":[]}"#.utf8)
        let decoded = try Sync.decodeSnapshot(good, expectedBlob: nil)
        precondition(decoded.tasks.isEmpty)
        expectFailure { _ = try Sync.decodeSnapshot(good, expectedBlob: String(repeating: "0", count: 40)) }
        expectFailure { _ = try Sync.decodeSnapshot(Data(good.dropLast()), expectedBlob: nil) }
        expectFailure { _ = try Sync.decodeSnapshot(Data(#"{"schema_version":2}"#.utf8), expectedBlob: nil) }
        expectFailure { _ = try Sync.decodeSnapshot(Data(#"{"schema_version":2,"tasks":[{"id":"x","title":"x","priority":"P8","status":"todo"}]}"#.utf8), expectedBlob: nil) }
        expectFailure { _ = try Sync.loadSnapshot("/dev/null/nonexistent-snapshot.json", expectedBlob: nil) }
    }

    static func expectFailure(_ action: () throws -> Void) {
        do {
            try action()
            preconditionFailure("Expected the snapshot to be rejected")
        } catch {}
    }

}
