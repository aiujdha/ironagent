package ai.ironagent.app.ui

import org.junit.Assert.assertEquals
import org.junit.Test

class SettingsSheetNotificationAppsTest {
  @Test
  fun resolveNotificationCandidatePackages_keepsConfiguredPackagesVisible() {
    val packages =
      resolveNotificationCandidatePackages(
        launcherPackages = setOf("com.example.launcher"),
        recentPackages = listOf("com.example.recent", "com.example.launcher"),
        configuredPackages = setOf("com.example.configured"),
        appPackageName = "ai.ironagent.app",
      )

    assertEquals(
      setOf("com.example.launcher", "com.example.recent", "com.example.configured"),
      packages,
    )
  }

  @Test
  fun resolveNotificationCandidatePackages_filtersBlankAndSelfPackages() {
    val packages =
      resolveNotificationCandidatePackages(
        launcherPackages = setOf(" ", "ai.ironagent.app"),
        recentPackages = listOf("com.example.recent", "  "),
        configuredPackages = setOf("ai.ironagent.app", "com.example.configured"),
        appPackageName = "ai.ironagent.app",
      )

    assertEquals(setOf("com.example.recent", "com.example.configured"), packages)
  }
}
