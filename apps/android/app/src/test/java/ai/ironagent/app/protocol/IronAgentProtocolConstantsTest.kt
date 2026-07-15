package ai.ironagent.app.protocol

import org.junit.Assert.assertEquals
import org.junit.Test

class IronAgentProtocolConstantsTest {
  @Test
  fun canvasCommandsUseStableStrings() {
    assertEquals("canvas.present", IronAgentCanvasCommand.Present.rawValue)
    assertEquals("canvas.hide", IronAgentCanvasCommand.Hide.rawValue)
    assertEquals("canvas.navigate", IronAgentCanvasCommand.Navigate.rawValue)
    assertEquals("canvas.eval", IronAgentCanvasCommand.Eval.rawValue)
    assertEquals("canvas.snapshot", IronAgentCanvasCommand.Snapshot.rawValue)
  }

  @Test
  fun a2uiCommandsUseStableStrings() {
    assertEquals("canvas.a2ui.push", IronAgentCanvasA2UICommand.Push.rawValue)
    assertEquals("canvas.a2ui.pushJSONL", IronAgentCanvasA2UICommand.PushJSONL.rawValue)
    assertEquals("canvas.a2ui.reset", IronAgentCanvasA2UICommand.Reset.rawValue)
  }

  @Test
  fun capabilitiesUseStableStrings() {
    assertEquals("canvas", IronAgentCapability.Canvas.rawValue)
    assertEquals("camera", IronAgentCapability.Camera.rawValue)
    assertEquals("voiceWake", IronAgentCapability.VoiceWake.rawValue)
    assertEquals("location", IronAgentCapability.Location.rawValue)
    assertEquals("sms", IronAgentCapability.Sms.rawValue)
    assertEquals("device", IronAgentCapability.Device.rawValue)
    assertEquals("notifications", IronAgentCapability.Notifications.rawValue)
    assertEquals("system", IronAgentCapability.System.rawValue)
    assertEquals("photos", IronAgentCapability.Photos.rawValue)
    assertEquals("contacts", IronAgentCapability.Contacts.rawValue)
    assertEquals("calendar", IronAgentCapability.Calendar.rawValue)
    assertEquals("motion", IronAgentCapability.Motion.rawValue)
    assertEquals("callLog", IronAgentCapability.CallLog.rawValue)
  }

  @Test
  fun cameraCommandsUseStableStrings() {
    assertEquals("camera.list", IronAgentCameraCommand.List.rawValue)
    assertEquals("camera.snap", IronAgentCameraCommand.Snap.rawValue)
    assertEquals("camera.clip", IronAgentCameraCommand.Clip.rawValue)
  }

  @Test
  fun notificationsCommandsUseStableStrings() {
    assertEquals("notifications.list", IronAgentNotificationsCommand.List.rawValue)
    assertEquals("notifications.actions", IronAgentNotificationsCommand.Actions.rawValue)
  }

  @Test
  fun deviceCommandsUseStableStrings() {
    assertEquals("device.status", IronAgentDeviceCommand.Status.rawValue)
    assertEquals("device.info", IronAgentDeviceCommand.Info.rawValue)
    assertEquals("device.permissions", IronAgentDeviceCommand.Permissions.rawValue)
    assertEquals("device.health", IronAgentDeviceCommand.Health.rawValue)
  }

  @Test
  fun systemCommandsUseStableStrings() {
    assertEquals("system.notify", IronAgentSystemCommand.Notify.rawValue)
  }

  @Test
  fun photosCommandsUseStableStrings() {
    assertEquals("photos.latest", IronAgentPhotosCommand.Latest.rawValue)
  }

  @Test
  fun contactsCommandsUseStableStrings() {
    assertEquals("contacts.search", IronAgentContactsCommand.Search.rawValue)
    assertEquals("contacts.add", IronAgentContactsCommand.Add.rawValue)
  }

  @Test
  fun calendarCommandsUseStableStrings() {
    assertEquals("calendar.events", IronAgentCalendarCommand.Events.rawValue)
    assertEquals("calendar.add", IronAgentCalendarCommand.Add.rawValue)
  }

  @Test
  fun motionCommandsUseStableStrings() {
    assertEquals("motion.activity", IronAgentMotionCommand.Activity.rawValue)
    assertEquals("motion.pedometer", IronAgentMotionCommand.Pedometer.rawValue)
  }

  @Test
  fun smsCommandsUseStableStrings() {
    assertEquals("sms.send", IronAgentSmsCommand.Send.rawValue)
    assertEquals("sms.search", IronAgentSmsCommand.Search.rawValue)
  }

  @Test
  fun callLogCommandsUseStableStrings() {
    assertEquals("callLog.search", IronAgentCallLogCommand.Search.rawValue)
  }

}
