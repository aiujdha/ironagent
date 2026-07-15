package ai.ironagent.app.node

import ai.ironagent.app.protocol.IronAgentCalendarCommand
import ai.ironagent.app.protocol.IronAgentCameraCommand
import ai.ironagent.app.protocol.IronAgentCallLogCommand
import ai.ironagent.app.protocol.IronAgentCapability
import ai.ironagent.app.protocol.IronAgentContactsCommand
import ai.ironagent.app.protocol.IronAgentDeviceCommand
import ai.ironagent.app.protocol.IronAgentLocationCommand
import ai.ironagent.app.protocol.IronAgentMotionCommand
import ai.ironagent.app.protocol.IronAgentNotificationsCommand
import ai.ironagent.app.protocol.IronAgentPhotosCommand
import ai.ironagent.app.protocol.IronAgentSmsCommand
import ai.ironagent.app.protocol.IronAgentSystemCommand
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class InvokeCommandRegistryTest {
  private val coreCapabilities =
    setOf(
      IronAgentCapability.Canvas.rawValue,
      IronAgentCapability.Device.rawValue,
      IronAgentCapability.Notifications.rawValue,
      IronAgentCapability.System.rawValue,
      IronAgentCapability.Photos.rawValue,
      IronAgentCapability.Contacts.rawValue,
      IronAgentCapability.Calendar.rawValue,
    )

  private val optionalCapabilities =
    setOf(
      IronAgentCapability.Camera.rawValue,
      IronAgentCapability.Location.rawValue,
      IronAgentCapability.Sms.rawValue,
      IronAgentCapability.CallLog.rawValue,
      IronAgentCapability.VoiceWake.rawValue,
      IronAgentCapability.Motion.rawValue,
    )

  private val coreCommands =
    setOf(
      IronAgentDeviceCommand.Status.rawValue,
      IronAgentDeviceCommand.Info.rawValue,
      IronAgentDeviceCommand.Permissions.rawValue,
      IronAgentDeviceCommand.Health.rawValue,
      IronAgentNotificationsCommand.List.rawValue,
      IronAgentNotificationsCommand.Actions.rawValue,
      IronAgentSystemCommand.Notify.rawValue,
      IronAgentPhotosCommand.Latest.rawValue,
      IronAgentContactsCommand.Search.rawValue,
      IronAgentContactsCommand.Add.rawValue,
      IronAgentCalendarCommand.Events.rawValue,
      IronAgentCalendarCommand.Add.rawValue,
    )

  private val optionalCommands =
    setOf(
      IronAgentCameraCommand.Snap.rawValue,
      IronAgentCameraCommand.Clip.rawValue,
      IronAgentCameraCommand.List.rawValue,
      IronAgentLocationCommand.Get.rawValue,
      IronAgentMotionCommand.Activity.rawValue,
      IronAgentMotionCommand.Pedometer.rawValue,
      IronAgentSmsCommand.Send.rawValue,
      IronAgentSmsCommand.Search.rawValue,
      IronAgentCallLogCommand.Search.rawValue,
    )

  private val debugCommands = setOf("debug.logs", "debug.ed25519")

  @Test
  fun advertisedCapabilities_respectsFeatureAvailability() {
    val capabilities = InvokeCommandRegistry.advertisedCapabilities(defaultFlags())

    assertContainsAll(capabilities, coreCapabilities)
    assertMissingAll(capabilities, optionalCapabilities)
  }

  @Test
  fun advertisedCapabilities_includesFeatureCapabilitiesWhenEnabled() {
    val capabilities =
      InvokeCommandRegistry.advertisedCapabilities(
        defaultFlags(
          cameraEnabled = true,
          locationEnabled = true,
          sendSmsAvailable = true,
          readSmsAvailable = true,
          smsSearchPossible = true,
          callLogAvailable = true,
          voiceWakeEnabled = true,
          motionActivityAvailable = true,
          motionPedometerAvailable = true,
        ),
      )

    assertContainsAll(capabilities, coreCapabilities + optionalCapabilities)
  }

  @Test
  fun advertisedCommands_respectsFeatureAvailability() {
    val commands = InvokeCommandRegistry.advertisedCommands(defaultFlags())

    assertContainsAll(commands, coreCommands)
    assertMissingAll(commands, optionalCommands + debugCommands)
  }

  @Test
  fun advertisedCommands_includesFeatureCommandsWhenEnabled() {
    val commands =
      InvokeCommandRegistry.advertisedCommands(
        defaultFlags(
          cameraEnabled = true,
          locationEnabled = true,
          sendSmsAvailable = true,
          readSmsAvailable = true,
          smsSearchPossible = true,
          callLogAvailable = true,
          motionActivityAvailable = true,
          motionPedometerAvailable = true,
          debugBuild = true,
        ),
      )

    assertContainsAll(commands, coreCommands + optionalCommands + debugCommands)
  }

  @Test
  fun advertisedCommands_onlyIncludesSupportedMotionCommands() {
    val commands =
      InvokeCommandRegistry.advertisedCommands(
        NodeRuntimeFlags(
          cameraEnabled = false,
          locationEnabled = false,
          sendSmsAvailable = false,
          readSmsAvailable = false,
          smsSearchPossible = false,
          callLogAvailable = false,
          voiceWakeEnabled = false,
          motionActivityAvailable = true,
          motionPedometerAvailable = false,
          debugBuild = false,
        ),
      )

    assertTrue(commands.contains(IronAgentMotionCommand.Activity.rawValue))
    assertFalse(commands.contains(IronAgentMotionCommand.Pedometer.rawValue))
  }

  @Test
  fun advertisedCommands_splitsSmsSendAndSearchAvailability() {
    val readOnlyCommands =
      InvokeCommandRegistry.advertisedCommands(
        defaultFlags(readSmsAvailable = true, smsSearchPossible = true),
      )
    val sendOnlyCommands =
      InvokeCommandRegistry.advertisedCommands(
        defaultFlags(sendSmsAvailable = true),
      )
    val requestableSearchCommands =
      InvokeCommandRegistry.advertisedCommands(
        defaultFlags(smsSearchPossible = true),
      )

    assertTrue(readOnlyCommands.contains(IronAgentSmsCommand.Search.rawValue))
    assertFalse(readOnlyCommands.contains(IronAgentSmsCommand.Send.rawValue))
    assertTrue(sendOnlyCommands.contains(IronAgentSmsCommand.Send.rawValue))
    assertFalse(sendOnlyCommands.contains(IronAgentSmsCommand.Search.rawValue))
    assertTrue(requestableSearchCommands.contains(IronAgentSmsCommand.Search.rawValue))
  }

  @Test
  fun advertisedCapabilities_includeSmsWhenEitherSmsPathIsAvailable() {
    val readOnlyCapabilities =
      InvokeCommandRegistry.advertisedCapabilities(
        defaultFlags(readSmsAvailable = true),
      )
    val sendOnlyCapabilities =
      InvokeCommandRegistry.advertisedCapabilities(
        defaultFlags(sendSmsAvailable = true),
      )
    val requestableSearchCapabilities =
      InvokeCommandRegistry.advertisedCapabilities(
        defaultFlags(smsSearchPossible = true),
      )

    assertTrue(readOnlyCapabilities.contains(IronAgentCapability.Sms.rawValue))
    assertTrue(sendOnlyCapabilities.contains(IronAgentCapability.Sms.rawValue))
    assertFalse(requestableSearchCapabilities.contains(IronAgentCapability.Sms.rawValue))
  }

  @Test
  fun advertisedCommands_excludesCallLogWhenUnavailable() {
    val commands = InvokeCommandRegistry.advertisedCommands(defaultFlags(callLogAvailable = false))

    assertFalse(commands.contains(IronAgentCallLogCommand.Search.rawValue))
  }

  @Test
  fun advertisedCapabilities_excludesCallLogWhenUnavailable() {
    val capabilities = InvokeCommandRegistry.advertisedCapabilities(defaultFlags(callLogAvailable = false))

    assertFalse(capabilities.contains(IronAgentCapability.CallLog.rawValue))
  }

  @Test
  fun advertisedCapabilities_includesVoiceWakeWithoutAdvertisingCommands() {
    val capabilities = InvokeCommandRegistry.advertisedCapabilities(defaultFlags(voiceWakeEnabled = true))
    val commands = InvokeCommandRegistry.advertisedCommands(defaultFlags(voiceWakeEnabled = true))

    assertTrue(capabilities.contains(IronAgentCapability.VoiceWake.rawValue))
    assertFalse(commands.any { it.contains("voice", ignoreCase = true) })
  }

  @Test
  fun find_returnsForegroundMetadataForCameraCommands() {
    val list = InvokeCommandRegistry.find(IronAgentCameraCommand.List.rawValue)
    val location = InvokeCommandRegistry.find(IronAgentLocationCommand.Get.rawValue)

    assertNotNull(list)
    assertEquals(true, list?.requiresForeground)
    assertNotNull(location)
    assertEquals(false, location?.requiresForeground)
  }

  @Test
  fun find_returnsNullForUnknownCommand() {
    assertNull(InvokeCommandRegistry.find("not.real"))
  }

  private fun defaultFlags(
    cameraEnabled: Boolean = false,
    locationEnabled: Boolean = false,
    sendSmsAvailable: Boolean = false,
    readSmsAvailable: Boolean = false,
    smsSearchPossible: Boolean = false,
    callLogAvailable: Boolean = false,
    voiceWakeEnabled: Boolean = false,
    motionActivityAvailable: Boolean = false,
    motionPedometerAvailable: Boolean = false,
    debugBuild: Boolean = false,
  ): NodeRuntimeFlags =
    NodeRuntimeFlags(
      cameraEnabled = cameraEnabled,
      locationEnabled = locationEnabled,
      sendSmsAvailable = sendSmsAvailable,
      readSmsAvailable = readSmsAvailable,
      smsSearchPossible = smsSearchPossible,
      callLogAvailable = callLogAvailable,
      voiceWakeEnabled = voiceWakeEnabled,
      motionActivityAvailable = motionActivityAvailable,
      motionPedometerAvailable = motionPedometerAvailable,
      debugBuild = debugBuild,
    )

  private fun assertContainsAll(actual: List<String>, expected: Set<String>) {
    expected.forEach { value -> assertTrue(actual.contains(value)) }
  }

  private fun assertMissingAll(actual: List<String>, forbidden: Set<String>) {
    forbidden.forEach { value -> assertFalse(actual.contains(value)) }
  }
}
