import Foundation

// Stable identifier used for both the macOS LaunchAgent label and Nix-managed defaults suite.
// nix-ironagent writes app defaults into this suite to survive app bundle identifier churn.
let launchdLabel = "ai.ironagent.mac"
let gatewayLaunchdLabel = "ai.ironagent.gateway"
let onboardingVersionKey = "ironagent.onboardingVersion"
let onboardingSeenKey = "ironagent.onboardingSeen"
let currentOnboardingVersion = 7
let pauseDefaultsKey = "ironagent.pauseEnabled"
let iconAnimationsEnabledKey = "ironagent.iconAnimationsEnabled"
let swabbleEnabledKey = "ironagent.swabbleEnabled"
let swabbleTriggersKey = "ironagent.swabbleTriggers"
let voiceWakeTriggerChimeKey = "ironagent.voiceWakeTriggerChime"
let voiceWakeSendChimeKey = "ironagent.voiceWakeSendChime"
let showDockIconKey = "ironagent.showDockIcon"
let defaultVoiceWakeTriggers = ["ironagent"]
let voiceWakeMaxWords = 32
let voiceWakeMaxWordLength = 64
let voiceWakeMicKey = "ironagent.voiceWakeMicID"
let voiceWakeMicNameKey = "ironagent.voiceWakeMicName"
let voiceWakeLocaleKey = "ironagent.voiceWakeLocaleID"
let voiceWakeAdditionalLocalesKey = "ironagent.voiceWakeAdditionalLocaleIDs"
let voicePushToTalkEnabledKey = "ironagent.voicePushToTalkEnabled"
let voiceWakeTriggersTalkModeKey = "ironagent.voiceWakeTriggersTalkMode"
let talkEnabledKey = "ironagent.talkEnabled"
let iconOverrideKey = "ironagent.iconOverride"
let connectionModeKey = "ironagent.connectionMode"
let remoteTargetKey = "ironagent.remoteTarget"
let remoteIdentityKey = "ironagent.remoteIdentity"
let remoteProjectRootKey = "ironagent.remoteProjectRoot"
let remoteCliPathKey = "ironagent.remoteCliPath"
let canvasEnabledKey = "ironagent.canvasEnabled"
let cameraEnabledKey = "ironagent.cameraEnabled"
let systemRunPolicyKey = "ironagent.systemRunPolicy"
let systemRunAllowlistKey = "ironagent.systemRunAllowlist"
let systemRunEnabledKey = "ironagent.systemRunEnabled"
let locationModeKey = "ironagent.locationMode"
let locationPreciseKey = "ironagent.locationPreciseEnabled"
let peekabooBridgeEnabledKey = "ironagent.peekabooBridgeEnabled"
let deepLinkKeyKey = "ironagent.deepLinkKey"
let modelCatalogPathKey = "ironagent.modelCatalogPath"
let modelCatalogReloadKey = "ironagent.modelCatalogReload"
let cliInstallPromptedVersionKey = "ironagent.cliInstallPromptedVersion"
let heartbeatsEnabledKey = "ironagent.heartbeatsEnabled"
let debugPaneEnabledKey = "ironagent.debugPaneEnabled"
let debugFileLogEnabledKey = "ironagent.debug.fileLogEnabled"
let appLogLevelKey = "ironagent.debug.appLogLevel"
let voiceWakeSupported: Bool = ProcessInfo.processInfo.operatingSystemVersion.majorVersion >= 26
