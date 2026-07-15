export type AssistantSingleEntryRoute = {
  assistantId: string;
  apiBasePath: string;
  wsBasePath: string;
};

export function parseAssistantSingleEntryRoute(pathname: string): AssistantSingleEntryRoute | null {
  const match = /^\/assistants\/([^/]+)\/ui(?:\/|$)/.exec(pathname);
  const encodedId = match?.[1];
  if (!encodedId) {
    return null;
  }
  const assistantId = safeDecodePathSegment(encodedId).trim();
  if (!assistantId) {
    return null;
  }
  const assistantPath = `/assistants/${encodeURIComponent(assistantId)}`;
  return {
    assistantId,
    apiBasePath: `${assistantPath}/control`,
    wsBasePath: `${assistantPath}/ws`,
  };
}

export function resolveCurrentAssistantSingleEntryRoute(): AssistantSingleEntryRoute | null {
  if (typeof window === "undefined") {
    return null;
  }
  return parseAssistantSingleEntryRoute(window.location.pathname);
}

function safeDecodePathSegment(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}
