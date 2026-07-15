import type { IronAgentConfig } from "../config/config.js";
import { buildPluginApi } from "./api-builder.js";
import type { PluginRuntime } from "./runtime/types.js";
import type {
  AnyAgentTool,
  CliBackendPlugin,
  ImageGenerationProviderPlugin,
  MediaUnderstandingProviderPlugin,
  IronAgentPluginApi,
  IronAgentPluginCliCommandDescriptor,
  IronAgentPluginCliRegistrar,
  ProviderPlugin,
  SpeechProviderPlugin,
  WebSearchProviderPlugin,
} from "./types.js";

type CapturedPluginCliRegistration = {
  register: IronAgentPluginCliRegistrar;
  commands: string[];
  descriptors: IronAgentPluginCliCommandDescriptor[];
};

export type CapturedPluginRegistration = {
  api: IronAgentPluginApi;
  providers: ProviderPlugin[];
  cliRegistrars: CapturedPluginCliRegistration[];
  cliBackends: CliBackendPlugin[];
  speechProviders: SpeechProviderPlugin[];
  mediaUnderstandingProviders: MediaUnderstandingProviderPlugin[];
  imageGenerationProviders: ImageGenerationProviderPlugin[];
  webSearchProviders: WebSearchProviderPlugin[];
  tools: AnyAgentTool[];
};

export function createCapturedPluginRegistration(params?: {
  config?: IronAgentConfig;
  registrationMode?: IronAgentPluginApi["registrationMode"];
}): CapturedPluginRegistration {
  const providers: ProviderPlugin[] = [];
  const cliRegistrars: CapturedPluginCliRegistration[] = [];
  const cliBackends: CliBackendPlugin[] = [];
  const speechProviders: SpeechProviderPlugin[] = [];
  const mediaUnderstandingProviders: MediaUnderstandingProviderPlugin[] = [];
  const imageGenerationProviders: ImageGenerationProviderPlugin[] = [];
  const webSearchProviders: WebSearchProviderPlugin[] = [];
  const tools: AnyAgentTool[] = [];
  const noopLogger = {
    info() {},
    warn() {},
    error() {},
    debug() {},
  };

  return {
    providers,
    cliRegistrars,
    cliBackends,
    speechProviders,
    mediaUnderstandingProviders,
    imageGenerationProviders,
    webSearchProviders,
    tools,
    api: buildPluginApi({
      id: "captured-plugin-registration",
      name: "Captured Plugin Registration",
      source: "captured-plugin-registration",
      registrationMode: params?.registrationMode ?? "full",
      config: params?.config ?? ({} as IronAgentConfig),
      runtime: {} as PluginRuntime,
      logger: noopLogger,
      resolvePath: (input) => input,
      handlers: {
        registerCli(registrar, opts) {
          const descriptors = (opts?.descriptors ?? [])
            .map((descriptor) => ({
              name: descriptor.name.trim(),
              description: descriptor.description.trim(),
              hasSubcommands: descriptor.hasSubcommands,
            }))
            .filter((descriptor) => descriptor.name && descriptor.description);
          const commands = [
            ...(opts?.commands ?? []),
            ...descriptors.map((descriptor) => descriptor.name),
          ]
            .map((command) => command.trim())
            .filter(Boolean);
          if (commands.length === 0) {
            return;
          }
          cliRegistrars.push({
            register: registrar,
            commands,
            descriptors,
          });
        },
        registerProvider(provider: ProviderPlugin) {
          providers.push(provider);
        },
        registerCliBackend(backend: CliBackendPlugin) {
          cliBackends.push(backend);
        },
        registerSpeechProvider(provider: SpeechProviderPlugin) {
          speechProviders.push(provider);
        },
        registerMediaUnderstandingProvider(provider: MediaUnderstandingProviderPlugin) {
          mediaUnderstandingProviders.push(provider);
        },
        registerImageGenerationProvider(provider: ImageGenerationProviderPlugin) {
          imageGenerationProviders.push(provider);
        },
        registerWebSearchProvider(provider: WebSearchProviderPlugin) {
          webSearchProviders.push(provider);
        },
        registerTool(tool) {
          if (typeof tool !== "function") {
            tools.push(tool);
          }
        },
      },
    }),
  };
}

export function capturePluginRegistration(params: {
  register(api: IronAgentPluginApi): void;
}): CapturedPluginRegistration {
  const captured = createCapturedPluginRegistration();
  params.register(captured.api);
  return captured;
}
