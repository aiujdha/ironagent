import {
  startLazyPluginServiceModule,
  type LazyPluginServiceHandle,
  type IronAgentPluginService,
} from "ironagent/plugin-sdk/browser-support";

type BrowserControlHandle = LazyPluginServiceHandle | null;

export function createBrowserPluginService(): IronAgentPluginService {
  let handle: BrowserControlHandle = null;

  return {
    id: "browser-control",
    start: async () => {
      if (handle) {
        return;
      }
      handle = await startLazyPluginServiceModule({
        skipEnvVar: "IRONAGENT_SKIP_BROWSER_CONTROL_SERVER",
        overrideEnvVar: "IRONAGENT_BROWSER_CONTROL_MODULE",
        // Keep the default module import static so compiled builds still bundle it.
        loadDefaultModule: async () => await import("./server.js"),
        startExportNames: [
          "startBrowserControlServiceFromConfig",
          "startBrowserControlServerFromConfig",
        ],
        stopExportNames: ["stopBrowserControlService", "stopBrowserControlServer"],
      });
    },
    stop: async () => {
      const current = handle;
      handle = null;
      if (!current) {
        return;
      }
      await current.stop().catch(() => {});
    },
  };
}
