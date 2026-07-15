import Foundation

public enum IronAgentCameraCommand: String, Codable, Sendable {
    case list = "camera.list"
    case snap = "camera.snap"
    case clip = "camera.clip"
}

public enum IronAgentCameraFacing: String, Codable, Sendable {
    case back
    case front
}

public enum IronAgentCameraImageFormat: String, Codable, Sendable {
    case jpg
    case jpeg
}

public enum IronAgentCameraVideoFormat: String, Codable, Sendable {
    case mp4
}

public struct IronAgentCameraSnapParams: Codable, Sendable, Equatable {
    public var facing: IronAgentCameraFacing?
    public var maxWidth: Int?
    public var quality: Double?
    public var format: IronAgentCameraImageFormat?
    public var deviceId: String?
    public var delayMs: Int?

    public init(
        facing: IronAgentCameraFacing? = nil,
        maxWidth: Int? = nil,
        quality: Double? = nil,
        format: IronAgentCameraImageFormat? = nil,
        deviceId: String? = nil,
        delayMs: Int? = nil)
    {
        self.facing = facing
        self.maxWidth = maxWidth
        self.quality = quality
        self.format = format
        self.deviceId = deviceId
        self.delayMs = delayMs
    }
}

public struct IronAgentCameraClipParams: Codable, Sendable, Equatable {
    public var facing: IronAgentCameraFacing?
    public var durationMs: Int?
    public var includeAudio: Bool?
    public var format: IronAgentCameraVideoFormat?
    public var deviceId: String?

    public init(
        facing: IronAgentCameraFacing? = nil,
        durationMs: Int? = nil,
        includeAudio: Bool? = nil,
        format: IronAgentCameraVideoFormat? = nil,
        deviceId: String? = nil)
    {
        self.facing = facing
        self.durationMs = durationMs
        self.includeAudio = includeAudio
        self.format = format
        self.deviceId = deviceId
    }
}
