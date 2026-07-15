import type { IronAgentConfig } from "../config/config.js";
import type { PluginRuntime } from "./runtime/types.js";
import type { IronAgentPluginApi, PluginLogger } from "./types.js";

export type BuildPluginApiParams = {
  id: string;
  name: string;
  version?: string;
  description?: string;
  source: string;
  rootDir?: string;
  registrationMode: IronAgentPluginApi["registrationMode"];
  config: IronAgentConfig;
  pluginConfig?: Record<string, unknown>;
  runtime: PluginRuntime;
  logger: PluginLogger;
  resolvePath: (input: string) => string;
  handlers?: Partial<
    Pick<
      IronAgentPluginApi,
      | "registerTool"
      | "registerHook"
      | "registerHttpRoute"
      | "registerChannel"
      | "registerGatewayMethod"
      | "registerCli"
      | "registerService"
      | "registerCliBackend"
      | "registerProvider"
      | "registerSpeechProvider"
      | "registerMediaUnderstandingProvider"
      | "registerImageGenerationProvider"
      | "registerWebSearchProvider"
      | "registerInteractiveHandler"
      | "onConversationBindingResolved"
      | "registerCommand"
      | "registerContextEngine"
      | "registerMemoryPromptSection"
      | "registerMemoryFlushPlan"
      | "registerMemoryRuntime"
      | "registerMemoryEmbeddingProvider"
      | "on"
    >
  >;
};

const noopRegisterTool: IronAgentPluginApi["registerTool"] = () => {};
const noopRegisterHook: IronAgentPluginApi["registerHook"] = () => {};
const noopRegisterHttpRoute: IronAgentPluginApi["registerHttpRoute"] = () => {};
const noopRegisterChannel: IronAgentPluginApi["registerChannel"] = () => {};
const noopRegisterGatewayMethod: IronAgentPluginApi["registerGatewayMethod"] = () => {};
const noopRegisterCli: IronAgentPluginApi["registerCli"] = () => {};
const noopRegisterService: IronAgentPluginApi["registerService"] = () => {};
const noopRegisterCliBackend: IronAgentPluginApi["registerCliBackend"] = () => {};
const noopRegisterProvider: IronAgentPluginApi["registerProvider"] = () => {};
const noopRegisterSpeechProvider: IronAgentPluginApi["registerSpeechProvider"] = () => {};
const noopRegisterMediaUnderstandingProvider: IronAgentPluginApi["registerMediaUnderstandingProvider"] =
  () => {};
const noopRegisterImageGenerationProvider: IronAgentPluginApi["registerImageGenerationProvider"] =
  () => {};
const noopRegisterWebSearchProvider: IronAgentPluginApi["registerWebSearchProvider"] = () => {};
const noopRegisterInteractiveHandler: IronAgentPluginApi["registerInteractiveHandler"] = () => {};
const noopOnConversationBindingResolved: IronAgentPluginApi["onConversationBindingResolved"] =
  () => {};
const noopRegisterCommand: IronAgentPluginApi["registerCommand"] = () => {};
const noopRegisterContextEngine: IronAgentPluginApi["registerContextEngine"] = () => {};
const noopRegisterMemoryPromptSection: IronAgentPluginApi["registerMemoryPromptSection"] = () => {};
const noopRegisterMemoryFlushPlan: IronAgentPluginApi["registerMemoryFlushPlan"] = () => {};
const noopRegisterMemoryRuntime: IronAgentPluginApi["registerMemoryRuntime"] = () => {};
const noopRegisterMemoryEmbeddingProvider: IronAgentPluginApi["registerMemoryEmbeddingProvider"] =
  () => {};
const noopOn: IronAgentPluginApi["on"] = () => {};

export function buildPluginApi(params: BuildPluginApiParams): IronAgentPluginApi {
  const handlers = params.handlers ?? {};
  return {
    id: params.id,
    name: params.name,
    version: params.version,
    description: params.description,
    source: params.source,
    rootDir: params.rootDir,
    registrationMode: params.registrationMode,
    config: params.config,
    pluginConfig: params.pluginConfig,
    runtime: params.runtime,
    logger: params.logger,
    registerTool: handlers.registerTool ?? noopRegisterTool,
    registerHook: handlers.registerHook ?? noopRegisterHook,
    registerHttpRoute: handlers.registerHttpRoute ?? noopRegisterHttpRoute,
    registerChannel: handlers.registerChannel ?? noopRegisterChannel,
    registerGatewayMethod: handlers.registerGatewayMethod ?? noopRegisterGatewayMethod,
    registerCli: handlers.registerCli ?? noopRegisterCli,
    registerService: handlers.registerService ?? noopRegisterService,
    registerCliBackend: handlers.registerCliBackend ?? noopRegisterCliBackend,
    registerProvider: handlers.registerProvider ?? noopRegisterProvider,
    registerSpeechProvider: handlers.registerSpeechProvider ?? noopRegisterSpeechProvider,
    registerMediaUnderstandingProvider:
      handlers.registerMediaUnderstandingProvider ?? noopRegisterMediaUnderstandingProvider,
    registerImageGenerationProvider:
      handlers.registerImageGenerationProvider ?? noopRegisterImageGenerationProvider,
    registerWebSearchProvider: handlers.registerWebSearchProvider ?? noopRegisterWebSearchProvider,
    registerInteractiveHandler:
      handlers.registerInteractiveHandler ?? noopRegisterInteractiveHandler,
    onConversationBindingResolved:
      handlers.onConversationBindingResolved ?? noopOnConversationBindingResolved,
    registerCommand: handlers.registerCommand ?? noopRegisterCommand,
    registerContextEngine: handlers.registerContextEngine ?? noopRegisterContextEngine,
    registerMemoryPromptSection:
      handlers.registerMemoryPromptSection ?? noopRegisterMemoryPromptSection,
    registerMemoryFlushPlan: handlers.registerMemoryFlushPlan ?? noopRegisterMemoryFlushPlan,
    registerMemoryRuntime: handlers.registerMemoryRuntime ?? noopRegisterMemoryRuntime,
    registerMemoryEmbeddingProvider:
      handlers.registerMemoryEmbeddingProvider ?? noopRegisterMemoryEmbeddingProvider,
    resolvePath: params.resolvePath,
    on: handlers.on ?? noopOn,
  };
}
