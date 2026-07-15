export const PRODUCT_NAME = "IronAgent";
export const PRODUCT_CONTROL_TITLE = "IronAgent Control";
export const EXPORT_FILENAME_PREFIX = "ironagent";
export const PRODUCT_LOGO_FILENAME = "ironagent-logo.png";
export const PRODUCT_ROOT_TAG = "ironagent-app";
// Internal-fork has no legacy users to migrate; the historical LEGACY tag
// (previous fork-base brand) has been retired. Keeping the exported array
// for callers that iterate accepted tags, even though it has a single entry.
export const PRODUCT_ROOT_TAGS = [PRODUCT_ROOT_TAG] as const;
export const PRODUCT_DOCS_URL = "https://docs.ironagent.ai";
export const PRODUCT_DASHBOARD_DOCS_URL = `${PRODUCT_DOCS_URL}/web/dashboard`;
export const PRODUCT_PAIRING_GUIDE_URL = `${PRODUCT_DOCS_URL}/web/control-ui#device-pairing-first-connection`;
export const PRODUCT_INSECURE_HTTP_GUIDE_URL = `${PRODUCT_DOCS_URL}/web/control-ui#insecure-http`;
export const PRODUCT_TAILSCALE_GUIDE_URL = `${PRODUCT_DOCS_URL}/gateway/tailscale`;
export const PRODUCT_CHANNELS_GUIDE_URL = `${PRODUCT_DOCS_URL}/channels`;
