import Foundation

public enum IronAgentRemindersCommand: String, Codable, Sendable {
    case list = "reminders.list"
    case add = "reminders.add"
}

public enum IronAgentReminderStatusFilter: String, Codable, Sendable {
    case incomplete
    case completed
    case all
}

public struct IronAgentRemindersListParams: Codable, Sendable, Equatable {
    public var status: IronAgentReminderStatusFilter?
    public var limit: Int?

    public init(status: IronAgentReminderStatusFilter? = nil, limit: Int? = nil) {
        self.status = status
        self.limit = limit
    }
}

public struct IronAgentRemindersAddParams: Codable, Sendable, Equatable {
    public var title: String
    public var dueISO: String?
    public var notes: String?
    public var listId: String?
    public var listName: String?

    public init(
        title: String,
        dueISO: String? = nil,
        notes: String? = nil,
        listId: String? = nil,
        listName: String? = nil)
    {
        self.title = title
        self.dueISO = dueISO
        self.notes = notes
        self.listId = listId
        self.listName = listName
    }
}

public struct IronAgentReminderPayload: Codable, Sendable, Equatable {
    public var identifier: String
    public var title: String
    public var dueISO: String?
    public var completed: Bool
    public var listName: String?

    public init(
        identifier: String,
        title: String,
        dueISO: String? = nil,
        completed: Bool,
        listName: String? = nil)
    {
        self.identifier = identifier
        self.title = title
        self.dueISO = dueISO
        self.completed = completed
        self.listName = listName
    }
}

public struct IronAgentRemindersListPayload: Codable, Sendable, Equatable {
    public var reminders: [IronAgentReminderPayload]

    public init(reminders: [IronAgentReminderPayload]) {
        self.reminders = reminders
    }
}

public struct IronAgentRemindersAddPayload: Codable, Sendable, Equatable {
    public var reminder: IronAgentReminderPayload

    public init(reminder: IronAgentReminderPayload) {
        self.reminder = reminder
    }
}
