import Foundation

public enum IronAgentLocationMode: String, Codable, Sendable, CaseIterable {
    case off
    case whileUsing
    case always
}
