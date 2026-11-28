import CryptoKit
import Darwin
import EventKit
import Foundation

struct TaskFile: Decodable {
    struct Task: Decodable {
        struct Due: Codable {
            let date: String
            let time: String?
            let timezone: String

            func components() throws -> DateComponents {
                guard let zone = TimeZone(identifier: timezone) else { throw SyncError.invalidInput }
                let format = time == nil ? "yyyy-MM-dd" : "yyyy-MM-dd HH:mm"
                let value = time.map { "\(date) \($0)" } ?? date
                let formatter = DateFormatter()
                formatter.locale = Locale(identifier: "en_US_POSIX")
                formatter.calendar = Calendar(identifier: .gregorian)
                formatter.timeZone = zone
                formatter.dateFormat = format
                guard let parsed = formatter.date(from: value), formatter.string(from: parsed) == value else {
                    throw SyncError.invalidInput
                }
                var calendar = Calendar(identifier: .gregorian)
                calendar.timeZone = zone
                let units: Set<Calendar.Component> = time == nil ? [.year, .month, .day] : [.year, .month, .day, .hour, .minute]
                var result = calendar.dateComponents(units, from: parsed)
                result.calendar = calendar
                result.timeZone = zone
                return result
            }
        }

        let id: String
        let title: String
        let priority: String
        let status: String
        let due: Due?
        let completed_at: String?
        let notes: String?
        let reminder_list: String?
        let start_time: String?
        let end_time: String?

        var schedule: Schedule {
            guard start_time != nil || end_time != nil else { return .clear }
            guard let start_time, let end_time,
                  let start = ZonedInstant(start_time), let end = ZonedInstant(end_time),
                  end.date > start.date else { return .invalid }
            return .block(start, end)
        }
    }

    struct Policy: Decodable {
        let direction: String
        let delete_behavior: String
    }

    let sync_policy: Policy?
    let schema_version: Int
    let tasks: [Task]
}

enum SyncError: Error {
    case invalidInput, notApproved, locked, noAccess, missingList, duplicateMapping, missingReminder, fetchFailed
    case invalidSchedule(String), staleObservation(String)
}

struct State: Codable {
    let listID: String
    var mapping: [String: String]
    var lastGitHubCompletion: [String: Bool]?
    var protectedIDs: Set<String>?
    var baselines: [String: ReminderBaseline]?
    var pending: [String: PendingReminderWrite]?
}

struct ReminderValue: Codable {
    let due: TaskFile.Task.Due?
    let completed: Bool
    let completedAt: String?

    func matches(_ other: ReminderValue) throws -> Bool {
        let sameDue = try Sync.sameDueDate(due?.components(), other.due?.components())
        return sameDue && completed == other.completed && completedAt == other.completedAt
    }
}

struct ReminderBaseline: Codable {
    let githubDue: TaskFile.Task.Due?
    let githubCompleted: Bool
    let apple: ReminderValue
}

struct PendingReminderWrite: Codable {
    let before: ReminderValue?
    let githubDue: TaskFile.Task.Due?
    let githubCompleted: Bool
    let desiredCompleted: Bool

    func matches(_ observed: ReminderValue) throws -> Bool {
        let sameDue = try Sync.sameDueDate(githubDue?.components(), observed.due?.components())
        return sameDue && desiredCompleted == observed.completed
    }
}

struct ReminderObservation: Codable {
    struct Item: Codable {
        let id: String
        let identifier: String
        let current: ReminderValue
        let baseline: ReminderBaseline?
        let legacyCompleted: Bool?
        let uncertain: Bool?
    }
    let version: Int
    let items: [Item]
    var excluded: Set<String>?
}

#if !SYNC_TEST
@main
#endif
struct Sync {
    static let priorities = ["P0": 1, "P1": 5, "P2": 9, "P3": 0]
    static let statuses = ["todo": false, "done": true, "completed": true]

    static func main() async {
        do {
            try await run()
        } catch {
            let category = (error as? SyncError).map { String(describing: $0) }
                ?? "\((error as NSError).domain) \((error as NSError).code)"
            FileHandle.standardError.write(Data("Sync failed: \(category)\n".utf8))
            exit(1)
        }
    }

    static func run() async throws {
        if Array(CommandLine.arguments.dropFirst()) == ["--version"] {
            #if VERSIONED_BUILD
            print(sourceBlob)
            #else
            print("unversioned")
            #endif
            return
        }
        let args = Array(CommandLine.arguments.dropFirst())
        if args.count == 2, args[0] == "--validate" {
            let file = try loadSnapshot(args[1], expectedBlob: nil)
            print("Validated \(file.tasks.count) reminders for schema \(file.schema_version)")
            return
        }
        guard (args.count == 3 && ["--dry-run", "--observe"].contains(args[0]))
            || (args.count == 4 && args[0] == "--apply") else {
            throw SyncError.invalidInput
        }
        let input = try Data(contentsOf: URL(fileURLWithPath: args[2]))
        let file = try decodeSnapshot(input, expectedBlob: args[1])
        umask(0o077)
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Personal Agent", isDirectory: true)
        let lock = open(directory.appendingPathComponent("sync.lock").path, O_CREAT | O_RDWR, 0o600)
        guard lock >= 0 else { throw SyncError.locked }
        defer { close(lock) }
        guard flock(lock, LOCK_EX | LOCK_NB) == 0 else { throw SyncError.locked }

        let observation = args[0] == "--apply"
            ? try JSONDecoder().decode(ReminderObservation.self, from: Data(contentsOf: URL(fileURLWithPath: args[3])))
            : nil
        try await syncReminders(file, input: input, store: EKEventStore(), directory: directory,
                                dryRun: args[0] == "--dry-run", observe: args[0] == "--observe",
                                observation: observation)
    }

    static func loadSnapshot(_ path: String, expectedBlob: String?) throws -> TaskFile {
        try decodeSnapshot(Data(contentsOf: URL(fileURLWithPath: path)), expectedBlob: expectedBlob)
    }

    static func decodeSnapshot(_ input: Data, expectedBlob: String?) throws -> TaskFile {
        if let expectedBlob {
            var hasher = Insecure.SHA1()
            hasher.update(data: Data("blob \(input.count)\0".utf8))
            hasher.update(data: input)
            guard hasher.finalize().map({ String(format: "%02x", $0) }).joined() == expectedBlob else {
                throw SyncError.invalidInput
            }
        }
        let file = try JSONDecoder().decode(TaskFile.self, from: input)
        if let policy = file.sync_policy {
            guard ["github_to_apple_reminders", "github_apple_reminders"].contains(policy.direction),
                  ["never_delete_automatically", "delete_verified_managed_reminders"].contains(policy.delete_behavior)
            else { throw SyncError.invalidInput }
        } else if expectedBlob != nil {
            throw SyncError.invalidInput
        }
        var ids = Set<String>()
        guard [1, 2].contains(file.schema_version) else { throw SyncError.invalidInput }
        for task in file.tasks {
            guard task.id.range(of: #"^[a-zA-Z0-9_-]{1,128}$"#, options: .regularExpression) != nil,
                  ids.insert(task.id).inserted,
                  !task.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  priorities[task.priority] != nil, (statuses[task.status] != nil || task.status == "cancelled"),
                  !(task.notes ?? "").contains("[Personal Agent ID:") else {
                throw SyncError.invalidInput
            }
            _ = try task.due?.components()
            if let completion = task.completed_at, ZonedInstant(completion) == nil {
                throw SyncError.invalidInput
            }
            if file.schema_version == 2, case .invalid = task.schedule {
                throw SyncError.invalidSchedule(task.id)
            }
        }

        return file
    }

    static func syncReminders(_ file: TaskFile, input: Data, store: EKEventStore, directory: URL,
                              dryRun: Bool, observe: Bool, observation: ReminderObservation?) async throws {
        let stateURL = directory.appendingPathComponent("state.json")
        guard try await store.requestFullAccessToReminders() else { throw SyncError.noAccess }
        let calendars = store.calendars(for: .reminder)
        var state: State
        var list: EKCalendar
        let firstImport = !FileManager.default.fileExists(atPath: stateURL.path)
        if !firstImport {
            state = try JSONDecoder().decode(State.self, from: Data(contentsOf: stateURL))
            guard let savedList = calendars.first(where: { $0.calendarIdentifier == state.listID }),
                  savedList.allowsContentModifications else { throw SyncError.missingList }
            list = savedList
        } else {
            if observe {
                try printObservation([])
                return
            }
            if dryRun {
                print("Dry run: no saved mapping; no deletions")
                return
            }
            let digest = SHA256.hash(data: input).map { String(format: "%02x", $0) }.joined()
            let approval = try? String(contentsOf: directory.appendingPathComponent("approved-snapshot"), encoding: .utf8)
            guard approval?.trimmingCharacters(in: .whitespacesAndNewlines) == digest else {
                throw SyncError.notApproved
            }
            let matchingLists = calendars.filter { $0.title == "Personal Agent" }
            if matchingLists.count == 1, let existing = matchingLists.first, existing.allowsContentModifications {
                list = existing
            } else {
                guard matchingLists.isEmpty,
                      let source = store.defaultCalendarForNewReminders()?.source,
                      source.sourceType == .calDAV else { throw SyncError.missingList }
                list = EKCalendar(for: .reminder, eventStore: store)
                list.title = "Personal Agent"
                list.source = source
                try store.saveCalendar(list, commit: true)
            }
            state = State(listID: list.calendarIdentifier, mapping: [:], protectedIDs: [])
        }

        let predicate = store.predicateForReminders(in: store.calendars(for: .reminder))
        let fetched: [EKReminder]? = await withCheckedContinuation { result in
            store.fetchReminders(matching: predicate) { result.resume(returning: $0) }
        }
        guard let reminders = fetched else { throw SyncError.fetchFailed }
        let byID = Dictionary(grouping: reminders, by: \.calendarItemIdentifier)
        guard byID.values.allSatisfy({ $0.count == 1 }),
              Set(state.mapping.values).count == state.mapping.count else { throw SyncError.duplicateMapping }
        let mappedOwners = Dictionary(uniqueKeysWithValues: state.mapping.map { ($0.value, $0.key) })
        let records = reminders.map {
            ReminderIdentity(identifier: $0.calendarItemIdentifier, title: $0.title ?? "",
                             notes: $0.notes, writable: $0.calendar.allowsContentModifications
                             && $0.calendar.source.sourceIdentifier == list.source.sourceIdentifier)
        }
        let projected = file.tasks.filter { $0.status != "cancelled" }
        let excluded = observation?.excluded ?? []
        let active = Set(projected.map(\.id))
        let protected = try state.protectedIDs ?? migrationProtection(directory: directory, state: state)
        let deletionEnabled = file.sync_policy?.delete_behavior == "delete_verified_managed_reminders"
            && ProcessInfo.processInfo.environment["PERSONAL_AGENT_ALLOW_REMINDER_DELETIONS"] == "true"
        let deletions = deletionPlan(active: active, mapping: state.mapping, protected: protected, reminders: records)
        state.protectedIDs = protected
        let encoder = JSONEncoder()
        var changed = 0
        var selected = Set<String>()
        var plan: [(TaskFile.Task, EKReminder, String, EKCalendar?, Bool)] = []
        for task in projected where !excluded.contains(task.id) {
            let marker = marker(for: task.id)
            let tagged = reminders.filter { $0.notes?.components(separatedBy: .newlines).contains(marker) == true }
            let mapped = state.mapping[task.id].flatMap { byID[$0]?.first }
            guard tagged.count <= 1,
                  mapped == nil || tagged.isEmpty || mapped?.calendarItemIdentifier == tagged.first?.calendarItemIdentifier else {
                throw SyncError.duplicateMapping
            }
            var target: EKCalendar?
            if let name = task.reminder_list {
                let matches = calendars.filter {
                    $0.title == name && $0.source.sourceIdentifier == list.source.sourceIdentifier
                }
                guard matches.count == 1, let destination = matches.first,
                      destination.allowsContentModifications else { throw SyncError.missingList }
                target = destination
            }
            let destination = target ?? list
            let matches = firstImport ? reminders.filter {
                canAdopt(title: $0.title ?? "", notes: $0.notes, listID: $0.calendar.calendarIdentifier,
                         taskTitle: task.title, targetID: destination.calendarIdentifier)
            } : []
            let reminder: EKReminder
            var isNew = false
            if let existing = mapped ?? tagged.first {
                let ownership = ownershipLines(existing.notes)
                let mappedOwner = mappedOwners[existing.calendarItemIdentifier]
                guard (mappedOwner == nil || mappedOwner == task.id),
                      ownership.isEmpty || ownership == [marker] else { throw SyncError.duplicateMapping }
                if ownership.isEmpty || mapped == nil {
                    state.protectedIDs?.insert(task.id)
                }
                reminder = existing
            } else if state.mapping[task.id] != nil {
                throw SyncError.missingReminder
            } else if matches.count == 1, let existing = matches.first {
                reminder = existing
                state.protectedIDs?.insert(task.id)
            } else if matches.isEmpty {
                reminder = EKReminder(eventStore: store)
                reminder.calendar = destination
                isNew = true
            } else {
                throw SyncError.duplicateMapping
            }
            guard reminder.calendar.allowsContentModifications,
                  reminder.calendar.source.sourceIdentifier == list.source.sourceIdentifier,
                  selected.insert(reminder.calendarItemIdentifier).inserted else { throw SyncError.duplicateMapping }
            if !isNew && state.protectedIDs?.contains(task.id) == true {
                state.mapping[task.id] = reminder.calendarItemIdentifier
            }
            plan.append((task, reminder, marker, target, isNew))
        }
        if observe {
            let items = try plan.compactMap { task, reminder, _, _, isNew -> ReminderObservation.Item? in
                guard !isNew else { return nil }
                let current = try value(reminder)
                var baseline = state.baselines?[task.id]
                var uncertain = false
                if let pending = state.pending?[task.id] {
                    if try pending.matches(current) {
                        baseline = ReminderBaseline(githubDue: pending.githubDue,
                            githubCompleted: pending.githubCompleted, apple: current)
                    } else {
                        uncertain = try !(pending.before?.matches(current) ?? false)
                    }
                }
                return ReminderObservation.Item(id: task.id, identifier: reminder.calendarItemIdentifier,
                    current: current, baseline: baseline, legacyCompleted: state.lastGitHubCompletion?[task.id],
                    uncertain: uncertain)

            }
            try printObservation(items)
            return
        }
        if deletionEnabled {
            for entry in deletions {
                let action = entry.reminderID == nil ? "Skip deletion" : (dryRun ? "Would delete" : "Planned deletion")
                print("\(action): \(entry.taskID) \(String(reflecting: entry.title))")
            }
        }
        if dryRun {
            print("Dry run: \(deletionEnabled ? deletions.filter { $0.reminderID != nil }.count : 0) planned deletions")
            return
        }
        guard let observation, observation.version == 1 else { throw SyncError.invalidInput }
        var observed: [String: ReminderObservation.Item] = [:]
        for item in observation.items {
            guard observed.updateValue(item, forKey: item.id) == nil else { throw SyncError.invalidInput }
        }
        // An observed item that disappears must not turn into a new creation.
        for (task, _, _, _, isNew) in plan where isNew && observed[task.id] != nil {
            throw SyncError.staleObservation(task.id)
        }
        // Check all observations before changing any reminders.
        for (task, reminder, _, _, isNew) in plan where !isNew {
            if let prior = observed[task.id] {
                guard prior.identifier == reminder.calendarItemIdentifier,
                      try prior.current.matches(value(reminder)) else { throw SyncError.staleObservation(task.id) }
            } else if !firstImport {
                throw SyncError.staleObservation(task.id)
            }
        }
        // Record adoption provenance before adding ownership markers to existing reminders.
        try encoder.encode(state).write(to: stateURL, options: .atomic)
        var baseline = state.lastGitHubCompletion ?? [:]
        var confirmed = state.baselines ?? [:]
        var pending = state.pending ?? [:]
        var verified: [String] = []
        var failed = false
        for (task, selectedReminder, marker, target, isNew) in plan {
            var reminder = selectedReminder
            if let prior = observed[task.id] {
                store.refreshSourcesIfNecessary()
                guard let fresh = store.calendarItem(withIdentifier: prior.identifier) as? EKReminder,
                      try prior.current.matches(value(fresh)),
                      ownershipLines(fresh.notes).allSatisfy({ $0 == marker }),
                      fresh.calendar.allowsContentModifications,
                      fresh.calendar.source.sourceIdentifier == list.source.sourceIdentifier else {
                    throw SyncError.staleObservation(task.id)
                }
                reminder = fresh
            }
            let notes = [task.notes, "GitHub 优先级 \(task.priority)", marker].compactMap { $0 }.joined(separator: "\n")
            let due = try task.due?.components()
            let move = target.map { $0.calendarIdentifier != reminder.calendar.calendarIdentifier } ?? false
            let github = statuses[task.status]!
            let desired = github || (firstImport && !isNew && reminder.isCompleted)
            let completedAt = task.completed_at.flatMap { ZonedInstant($0)?.date }
            let completionDateChanged = github && (completedAt.map {
                abs((reminder.completionDate ?? .distantPast).timeIntervalSince($0)) > 1
            } ?? false)
            if move || reminder.title != task.title || reminder.notes != notes || !sameDueDate(reminder.dueDateComponents, due)
                || reminder.priority != priorities[task.priority] || reminder.isCompleted != desired || completionDateChanged {
                pending[task.id] = PendingReminderWrite(before: isNew ? nil : try value(reminder),
                    githubDue: task.due, githubCompleted: github, desiredCompleted: desired)
                state.pending = pending
                try encoder.encode(state).write(to: stateURL, options: .atomic)
                if let target, move { reminder.calendar = target }
                reminder.title = task.title
                reminder.notes = notes
                reminder.dueDateComponents = due
                reminder.priority = priorities[task.priority]!
                setCompletion(reminder, completed: desired, completedAt: github ? completedAt : nil)
                try store.save(reminder, commit: true)
                changed += 1
            }
            guard let saved = store.calendarItem(withIdentifier: reminder.calendarItemIdentifier) as? EKReminder,
                  sameDueDate(saved.dueDateComponents, due), saved.isCompleted == desired,
                  ownershipLines(saved.notes) == [marker],
                  !github || (completedAt.map {
                      abs((saved.completionDate ?? .distantPast).timeIntervalSince($0)) <= 1
                  } ?? true) else {
                print("EventKit readback mismatch: \(task.id)")
                failed = true
                continue
            }
            state.mapping[task.id] = saved.calendarItemIdentifier
            baseline[task.id] = github
            confirmed[task.id] = ReminderBaseline(githubDue: task.due, githubCompleted: github,
                                                  apple: try value(saved))
            state.lastGitHubCompletion = baseline
            state.baselines = confirmed
            pending.removeValue(forKey: task.id)
            state.pending = pending
            try encoder.encode(state).write(to: stateURL, options: .atomic)
            verified.append(task.id)
        }
        var deleted = 0
        if deletionEnabled {
            for entry in deletions {
                guard let identifier = entry.reminderID else { continue }
                do {
                    guard let reminder = store.calendarItem(withIdentifier: identifier) as? EKReminder,
                          ownershipLines(reminder.notes) == [marker(for: entry.taskID)],
                          reminder.calendar.allowsContentModifications,
                          reminder.calendar.source.sourceIdentifier == list.source.sourceIdentifier else {
                        throw SyncError.missingReminder
                    }
                    try store.remove(reminder, commit: true)
                } catch {
                    print("Deletion failed: \(entry.taskID) \(String(reflecting: entry.title))")
                    failed = true
                    continue
                }
                print("Deleted: \(entry.taskID) \(String(reflecting: entry.title))")
                deleted += 1
                state.mapping.removeValue(forKey: entry.taskID)
                state.lastGitHubCompletion?.removeValue(forKey: entry.taskID)
                state.baselines?.removeValue(forKey: entry.taskID)
                state.pending?.removeValue(forKey: entry.taskID)
                try encoder.encode(state).write(to: stateURL, options: .atomic)
            }
        }
        print("Reminders: deleted=\(deleted)")
        print("Reminders: eventkit_verified=\(verified.count) changed=\(changed)")
        print("Reminders: verified_ids=\(verified.joined(separator: ","))")
        if failed { throw SyncError.fetchFailed }
    }

    static func setCompletion(_ reminder: EKReminder, completed: Bool, completedAt: Date?) {
        if reminder.isCompleted != completed { reminder.isCompleted = completed }
        if completed, let completedAt { reminder.completionDate = completedAt }
    }

    static func printObservation(_ items: [ReminderObservation.Item]) throws {
        let data = try JSONEncoder().encode(ReminderObservation(version: 1, items: items))
        print(String(decoding: data, as: UTF8.self))
    }

    static func value(_ reminder: EKReminder) throws -> ReminderValue {
        var due: TaskFile.Task.Due?
        if let source = reminder.dueDateComponents {
            guard let year = source.year, let month = source.month, let day = source.day else {
                throw SyncError.invalidInput
            }
            let date = String(format: "%04d-%02d-%02d", year, month, day)
            let time = source.hour.map { String(format: "%02d:%02d", $0, source.minute ?? 0) }
            let zone = source.timeZone ?? source.calendar?.timeZone ?? TimeZone.current
            due = TaskFile.Task.Due(date: date, time: time, timezone: zone.identifier)
        }
        let completedAt = reminder.completionDate.map { ISO8601DateFormatter().string(from: $0) }
        return ReminderValue(due: due, completed: reminder.isCompleted, completedAt: completedAt)
    }

    struct ReminderIdentity {
        let identifier: String
        let title: String
        let notes: String?
        let writable: Bool
    }

    struct Deletion {
        let taskID: String
        let title: String
        let reminderID: String?
    }

    static func marker(for id: String) -> String { "[Personal Agent ID: \(id)]" }

    static func ownershipLines(_ notes: String?) -> [String] {
        (notes ?? "").components(separatedBy: .newlines).filter { $0.contains("[Personal Agent ID:") }
    }

    static func deletionPlan(active: Set<String>, mapping: [String: String], protected: Set<String>,
                             reminders: [ReminderIdentity]) -> [Deletion] {
        let prefix = "[Personal Agent ID: "
        var byMarker: [String: [ReminderIdentity]] = [:]
        for reminder in reminders {
            for line in Set(ownershipLines(reminder.notes)) where line.hasPrefix(prefix) && line.hasSuffix("]") {
                let id = String(line.dropFirst(prefix.count).dropLast())
                byMarker[id, default: []].append(reminder)
            }
        }
        let byIdentifier = Dictionary(grouping: reminders, by: \.identifier)
        return Set(mapping.keys).union(byMarker.keys).subtracting(active).sorted().flatMap { id -> [Deletion] in
            let tagged = byMarker[id] ?? []
            let mapped = mapping[id].flatMap { byIdentifier[$0] } ?? []
            if !protected.contains(id), tagged.count == 1, mapped.count == 1,
               let reminder = mapped.first, reminder.identifier == tagged.first?.identifier,
               reminder.writable, ownershipLines(reminder.notes) == [marker(for: id)] {
                return [Deletion(taskID: id, title: reminder.title, reminderID: reminder.identifier)]
            }
            let candidates = mapped + tagged.filter { candidate in
                !mapped.contains { $0.identifier == candidate.identifier }
            }
            if candidates.isEmpty { return [Deletion(taskID: id, title: "(missing reminder)", reminderID: nil)] }
            return candidates.map { Deletion(taskID: id, title: $0.title, reminderID: nil) }
        }
    }

    static func migrationProtection(directory: URL, state: State) throws -> Set<String> {
        let initial = directory.appendingPathComponent("initial-tasks.json")
        guard FileManager.default.fileExists(atPath: initial.path) else { return Set(state.mapping.keys) }
        let input = try Data(contentsOf: initial)
        let digest = SHA256.hash(data: input).map { String(format: "%02x", $0) }.joined()
        let approval = try String(contentsOf: directory.appendingPathComponent("approved-snapshot"), encoding: .utf8)
        guard approval.trimmingCharacters(in: .whitespacesAndNewlines) == digest else { throw SyncError.notApproved }
        return Set(try decodeSnapshot(input, expectedBlob: nil).tasks.map(\.id))
    }

    static func canAdopt(title: String, notes: String?, listID: String, taskTitle: String, targetID: String) -> Bool {
        listID == targetID && !(notes ?? "").contains("[Personal Agent ID:")
            && normalized(title) == normalized(taskTitle)
    }

    static func sameDueDate(_ existing: DateComponents?, _ desired: DateComponents?) -> Bool {
        guard let existing, let desired else { return existing == nil && desired == nil }
        if existing.hour == nil || desired.hour == nil {
            return existing.hour == nil && desired.hour == nil && existing.year == desired.year
                && existing.month == desired.month && existing.day == desired.day
        }
        // A legacy floating reminder returns components in the Mac's effective calendar zone.
        // Compare the represented instant, including explicitly zoned reminders.
        guard let existingDate = existing.date, let desiredDate = desired.date else { return false }
        return floor(existingDate.timeIntervalSince1970 / 60) == floor(desiredDate.timeIntervalSince1970 / 60)
    }

    static func normalized(_ title: String) -> String {
        title.precomposedStringWithCompatibilityMapping
            .replacingOccurrences(of: #"^\s*\[P[0-3]\]\s*"#, with: "", options: .regularExpression)
            .lowercased()
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")
    }
}

struct ZonedInstant {
    let date: Date

    init?(_ source: String) {
        let pattern = #"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$"#
        guard let offsetRange = source.range(of: #"Z$|[+-]\d{2}:?\d{2}$"#, options: .regularExpression),
              source.range(of: pattern, options: .regularExpression) != nil else { return nil }
        let offset = source[offsetRange].filter { $0 != ":" }
        let seconds: Int
        if offset == "Z" {
            seconds = 0
        } else {
            guard let hours = Int(offset.dropFirst().prefix(2)), let minutes = Int(offset.suffix(2)),
                  hours <= 18, minutes < 60 else { return nil }
            seconds = (hours * 3600 + minutes * 60) * (offset.first == "-" ? -1 : 1)
        }
        guard let zone = TimeZone(secondsFromGMT: seconds) else { return nil }
        var local = String(source[..<offsetRange.lowerBound])
        if local.count == 16 { local += ":00" }
        let parts = local.split(separator: ".", omittingEmptySubsequences: false)
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = zone
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        let whole = String(parts[0])
        guard let base = formatter.date(from: whole), formatter.string(from: base) == whole else { return nil }
        let fraction = parts.count == 2 ? Double("0." + parts[1]) ?? 0 : 0
        self.date = base.addingTimeInterval(fraction)
    }
}

enum Schedule {
    case clear, invalid, block(ZonedInstant, ZonedInstant)

    var label: String {
        switch self {
        case .clear: "clear"
        case .invalid: "invalid"
        case .block: "block"
        }
    }
}
