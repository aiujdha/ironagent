import Foundation

public enum IronAgentDeviceCommand: String, Codable, Sendable {
    case status = "device.status"
    case info = "device.info"
}

public enum IronAgentBatteryState: String, Codable, Sendable {
    case unknown
    case unplugged
    case charging
    case full
}

public enum IronAgentThermalState: String, Codable, Sendable {
    case nominal
    case fair
    case serious
    case critical
}

public enum IronAgentNetworkPathStatus: String, Codable, Sendable {
    case satisfied
    case unsatisfied
    case requiresConnection
}

public enum IronAgentNetworkInterfaceType: String, Codable, Sendable {
    case wifi
    case cellular
    case wired
    case other
}

public struct IronAgentBatteryStatusPayload: Codable, Sendable, Equatable {
    public var level: Double?
    public var state: IronAgentBatteryState
    public var lowPowerModeEnabled: Bool

    public init(level: Double?, state: IronAgentBatteryState, lowPowerModeEnabled: Bool) {
        self.level = level
        self.state = state
        self.lowPowerModeEnabled = lowPowerModeEnabled
    }
}

public struct IronAgentThermalStatusPayload: Codable, Sendable, Equatable {
    public var state: IronAgentThermalState

    public init(state: IronAgentThermalState) {
        self.state = state
    }
}

public struct IronAgentStorageStatusPayload: Codable, Sendable, Equatable {
    public var totalBytes: Int64
    public var freeBytes: Int64
    public var usedBytes: Int64

    public init(totalBytes: Int64, freeBytes: Int64, usedBytes: Int64) {
        self.totalBytes = totalBytes
        self.freeBytes = freeBytes
        self.usedBytes = usedBytes
    }
}

public struct IronAgentNetworkStatusPayload: Codable, Sendable, Equatable {
    public var status: IronAgentNetworkPathStatus
    public var isExpensive: Bool
    public var isConstrained: Bool
    public var interfaces: [IronAgentNetworkInterfaceType]

    public init(
        status: IronAgentNetworkPathStatus,
        isExpensive: Bool,
        isConstrained: Bool,
        interfaces: [IronAgentNetworkInterfaceType])
    {
        self.status = status
        self.isExpensive = isExpensive
        self.isConstrained = isConstrained
        self.interfaces = interfaces
    }
}

public struct IronAgentDeviceStatusPayload: Codable, Sendable, Equatable {
    public var battery: IronAgentBatteryStatusPayload
    public var thermal: IronAgentThermalStatusPayload
    public var storage: IronAgentStorageStatusPayload
    public var network: IronAgentNetworkStatusPayload
    public var uptimeSeconds: Double

    public init(
        battery: IronAgentBatteryStatusPayload,
        thermal: IronAgentThermalStatusPayload,
        storage: IronAgentStorageStatusPayload,
        network: IronAgentNetworkStatusPayload,
        uptimeSeconds: Double)
    {
        self.battery = battery
        self.thermal = thermal
        self.storage = storage
        self.network = network
        self.uptimeSeconds = uptimeSeconds
    }
}

public struct IronAgentDeviceInfoPayload: Codable, Sendable, Equatable {
    public var deviceName: String
    public var modelIdentifier: String
    public var systemName: String
    public var systemVersion: String
    public var appVersion: String
    public var appBuild: String
    public var locale: String

    public init(
        deviceName: String,
        modelIdentifier: String,
        systemName: String,
        systemVersion: String,
        appVersion: String,
        appBuild: String,
        locale: String)
    {
        self.deviceName = deviceName
        self.modelIdentifier = modelIdentifier
        self.systemName = systemName
        self.systemVersion = systemVersion
        self.appVersion = appVersion
        self.appBuild = appBuild
        self.locale = locale
    }
}
