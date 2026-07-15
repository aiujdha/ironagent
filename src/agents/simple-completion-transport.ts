import { getApiProvider, type Api, type Model } from "@mariozechner/pi-ai";
import type { IronAgentConfig } from "../config/config.js";
import { registerProviderStreamForModel } from "./provider-stream.js";

export function prepareModelForSimpleCompletion<TApi extends Api>(params: {
  model: Model<TApi>;
  cfg?: IronAgentConfig;
}): Model<Api> {
  const { model, cfg } = params;
  // Only provider-owned custom APIs need runtime stream registration here.
  if (!getApiProvider(model.api) && registerProviderStreamForModel({ model, cfg })) {
    return model;
  }

  // Phase 10.2: anthropic-vertex provider was removed; no special transport
  // wiring needed for the kept providers.
  return model;
}
