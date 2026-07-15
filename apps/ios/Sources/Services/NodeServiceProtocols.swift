import CoreLocation
import Foundation
import IronAgentKit
import UIKit

typealias IronAgentCameraSnapResult = (format: String, base64: String, width: Int, height: Int)
typealias IronAgentCameraClipResult = (format: String, base64: String, durationMs: Int, hasAudio: Bool)

protocol CameraServicing: Sendable {
    func listDevices() async -> [CameraController.CameraDeviceInfo]
    func snap(params: IronAgentCameraSnapParams) async throws -> IronAgentCameraSnapResult
    func clip(params: IronAgentCameraClipParams) async throws -> IronAgentCameraClipResult
}

protocol ScreenRecordingServicing: Sendable {
    func record(
        screenIndex: Int?,
        durationMs: Int?,
        fps: Double?,
        includeAudio: Bool?,
        outPath: String?) async throws -> String
}

@MainActor
protocol LocationServicing: Sendable {
    func authorizationStatus() -> CLAuthorizationStatus
    func accuracyAuthorization() -> CLAccuracyAuthorization
    func ensureAuthorization(mode: IronAgentLocationMode) async -> CLAuthorizationStatus
    func currentLocation(
        params: IronAgentLocationGetParams,
        desiredAccuracy: IronAgentLocationAccuracy,
        maxAgeMs: Int?,
        timeoutMs: Int?) async throws -> CLLocation
    func startLocationUpdates(
        desiredAccuracy: IronAgentLocationAccuracy,
        significantChangesOnly: Bool) -> AsyncStream<CLLocation>
    func stopLocationUpdates()
    func startMonitoringSignificantLocationChanges(onUpdate: @escaping @Sendable (CLLocation) -> Void)
    func stopMonitoringSignificantLocationChanges()
}

@MainActor
protocol DeviceStatusServicing: Sendable {
    func status() async throws -> IronAgentDeviceStatusPayload
    func info() -> IronAgentDeviceInfoPayload
}

protocol PhotosServicing: Sendable {
    func latest(params: IronAgentPhotosLatestParams) async throws -> IronAgentPhotosLatestPayload
}

protocol ContactsServicing: Sendable {
    func search(params: IronAgentContactsSearchParams) async throws -> IronAgentContactsSearchPayload
    func add(params: IronAgentContactsAddParams) async throws -> IronAgentContactsAddPayload
}

protocol CalendarServicing: Sendable {
    func events(params: IronAgentCalendarEventsParams) async throws -> IronAgentCalendarEventsPayload
    func add(params: IronAgentCalendarAddParams) async throws -> IronAgentCalendarAddPayload
}

protocol RemindersServicing: Sendable {
    func list(params: IronAgentRemindersListParams) async throws -> IronAgentRemindersListPayload
    func add(params: IronAgentRemindersAddParams) async throws -> IronAgentRemindersAddPayload
}

protocol MotionServicing: Sendable {
    func activities(params: IronAgentMotionActivityParams) async throws -> IronAgentMotionActivityPayload
    func pedometer(params: IronAgentPedometerParams) async throws -> IronAgentPedometerPayload
}

struct WatchMessagingStatus: Sendable, Equatable {
    var supported: Bool
    var paired: Bool
    var appInstalled: Bool
    var reachable: Bool
    var activationState: String
}

struct WatchQuickReplyEvent: Sendable, Equatable {
    var replyId: String
    var promptId: String
    var actionId: String
    var actionLabel: String?
    var sessionKey: String?
    var note: String?
    var sentAtMs: Int?
    var transport: String
}

struct WatchNotificationSendResult: Sendable, Equatable {
    var deliveredImmediately: Bool
    var queuedForDelivery: Bool
    var transport: String
}

protocol WatchMessagingServicing: AnyObject, Sendable {
    func status() async -> WatchMessagingStatus
    func setReplyHandler(_ handler: (@Sendable (WatchQuickReplyEvent) -> Void)?)
    func sendNotification(
        id: String,
        params: IronAgentWatchNotifyParams) async throws -> WatchNotificationSendResult
}

extension CameraController: CameraServicing {}
extension ScreenRecordService: ScreenRecordingServicing {}
extension LocationService: LocationServicing {}
