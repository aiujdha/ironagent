import { html, nothing, type TemplateResult } from "lit";
import { ref } from "lit/directives/ref.js";
import { repeat } from "lit/directives/repeat.js";
import { t } from "../../i18n/index.ts";
import type {
  CompactionStatus as CompactionIndicatorStatus,
  FallbackStatus as FallbackIndicatorStatus,
} from "../app-tool-stream.ts";
import { PRODUCT_NAME } from "../branding.ts";
import {
  CHAT_ATTACHMENT_ACCEPT,
  isSupportedChatAttachmentFile,
  revokeChatAttachmentPreview,
} from "../chat/attachment-support.ts";
import { DeletedMessages } from "../chat/deleted-messages.ts";
import { exportChatMarkdown } from "../chat/export.ts";
import {
  renderMessageGroup,
  renderReadingIndicatorGroup,
  renderStreamingGroup,
} from "../chat/grouped-render.ts";
import { InputHistory } from "../chat/input-history.ts";
import { normalizeMessage, normalizeRoleForGrouping } from "../chat/message-normalizer.ts";
import { PinnedMessages } from "../chat/pinned-messages.ts";
import { getPinnedMessageSummary } from "../chat/pinned-summary.ts";
import { messageMatchesSearchQuery } from "../chat/search-match.ts";
import { getOrCreateSessionCacheValue } from "../chat/session-cache.ts";
import {
  SLASH_COMMANDS,
  getSlashCategoryLabel,
  getSlashCommandCompletions,
  type SlashCommandCategory,
  type SlashCommandDef,
} from "../chat/slash-commands.ts";
import { isSttSupported, startStt, stopStt } from "../chat/speech.ts";
import type { ExecApprovalRequest } from "../controllers/exec-approval.ts";
import { isControlUiActionableApproval } from "../controllers/ironagent-approvals.ts";
import type {
  WorkspaceDeleteResult,
  WorkspaceUploadResult,
} from "../controllers/workspace-files.ts";
import { formatDisplayPath } from "../display-paths.ts";
import { icons } from "../icons.ts";
import { detectTextDirection } from "../text-direction.ts";
import type {
  AgentFileEntry,
  AgentsFilesListResult,
  GatewaySessionRow,
  SessionsListResult,
  SkillStatusEntry,
  SkillStatusReport,
  ToolsEffectiveResult,
} from "../types.ts";
import type { ChatItem, MessageGroup } from "../types/chat-types.ts";
import type { ChatAttachment, ChatQueueItem } from "../ui-types.ts";
import { agentLogoUrl, resolveAgentAvatarUrl } from "./agents-utils.ts";
import "../components/resizable-divider.ts";
import { renderMarkdownSidebar } from "./markdown-sidebar.ts";

export type ChatProps = {
  sessionKey: string;
  onSessionKeyChange: (next: string) => void;
  thinkingLevel: string | null;
  showThinking: boolean;
  showToolCalls: boolean;
  loading: boolean;
  sending: boolean;
  canAbort?: boolean;
  compactionStatus?: CompactionIndicatorStatus | null;
  fallbackStatus?: FallbackIndicatorStatus | null;
  messages: unknown[];
  toolMessages: unknown[];
  streamSegments: Array<{ text: string; ts: number }>;
  stream: string | null;
  streamStartedAt: number | null;
  assistantAvatarUrl?: string | null;
  draft: string;
  queue: ChatQueueItem[];
  connected: boolean;
  canSend: boolean;
  disabledReason: string | null;
  error: string | null;
  sessions: SessionsListResult | null;
  focusMode: boolean;
  sidebarOpen?: boolean;
  sidebarContent?: string | null;
  sidebarError?: string | null;
  splitRatio?: number;
  assistantName: string;
  assistantAvatar: string | null;
  attachments?: ChatAttachment[];
  onAttachmentsChange?: (attachments: ChatAttachment[]) => void;
  showNewMessages?: boolean;
  onScrollToBottom?: () => void;
  onRefresh: () => void;
  onToggleFocusMode: () => void;
  getDraft?: () => string;
  onDraftChange: (next: string) => void;
  onRequestUpdate?: () => void;
  onSend: () => void;
  onAbort?: () => void;
  onQueueRemove: (id: string) => void;
  onNewSession: () => void;
  onClearHistory?: () => void;
  agentsList: {
    agents: Array<{ id: string; name?: string; identity?: { name?: string; avatarUrl?: string } }>;
    defaultId?: string;
  } | null;
  currentAgentId: string;
  onAgentChange: (agentId: string) => void;
  onNavigateToAgent?: () => void;
  onSessionSelect?: (sessionKey: string) => void;
  onOpenSidebar?: (content: string) => void;
  onCloseSidebar?: () => void;
  onSplitRatioChange?: (ratio: number) => void;
  onChatScroll?: (event: Event) => void;
  basePath?: string;
  workbenchCollapsed?: boolean;
  onToggleWorkbench?: () => void;
  workspaceFiles?: AgentsFilesListResult | null;
  workspaceFilesLoading?: boolean;
  workspaceFilesError?: string | null;
  agentFiles?: AgentsFilesListResult | null;
  agentFilesLoading?: boolean;
  agentFilesError?: string | null;
  onRefreshWorkspaceFiles?: () => void;
  onUploadWorkspaceFiles?: (files: File[]) => Promise<WorkspaceUploadResult[]>;
  onDeleteWorkspaceFile?: (
    fileId: string,
    bucket?: "temp" | "persistent",
  ) => void | WorkspaceDeleteResult | Promise<void | WorkspaceDeleteResult | null>;
  approvals?: ExecApprovalRequest[];
  canResolveGatewayApprovals?: boolean;
  refreshingApprovals?: boolean;
  onRefreshApprovals?: () => void;
  onApprovalDecision?: (
    approvalId: string,
    decision: "allow-once" | "allow-always" | "deny",
  ) => void;
  skillsReport?: SkillStatusReport | null;
  skillsLoading?: boolean;
  skillsError?: string | null;
  skillsBusyKey?: string | null;
  onRefreshSkills?: () => void;
  onToggleSkillEnabled?: (skillKey: string, enabled: boolean) => void | Promise<void>;
  toolsEffectiveResult?: ToolsEffectiveResult | null;
  toolsEffectiveLoading?: boolean;
  toolsEffectiveError?: string | null;
};

const COMPACTION_TOAST_DURATION_MS = 5000;
const FALLBACK_TOAST_DURATION_MS = 8000;
type SkillsWorkbenchFilter = "ready" | "disabled";
type SkillsWorkbenchStatus = "ready" | "disabled";
type ChatUploadNoticeKind = "success" | "info";
type ChatUploadNotice = {
  kind: ChatUploadNoticeKind;
  message: string;
};

type WorkspaceTreeNode =
  | {
      kind: "folder";
      name: string;
      itemCount: number;
      children: WorkspaceTreeNode[];
    }
  | {
      kind: "file";
      file: AgentFileEntry;
    };

// Persistent instances keyed by session
const inputHistories = new Map<string, InputHistory>();
const pinnedMessagesMap = new Map<string, PinnedMessages>();
const deletedMessagesMap = new Map<string, DeletedMessages>();

function getInputHistory(sessionKey: string): InputHistory {
  return getOrCreateSessionCacheValue(inputHistories, sessionKey, () => new InputHistory());
}

function getPinnedMessages(sessionKey: string): PinnedMessages {
  return getOrCreateSessionCacheValue(
    pinnedMessagesMap,
    sessionKey,
    () => new PinnedMessages(sessionKey),
  );
}

function getDeletedMessages(sessionKey: string): DeletedMessages {
  return getOrCreateSessionCacheValue(
    deletedMessagesMap,
    sessionKey,
    () => new DeletedMessages(sessionKey),
  );
}

interface ChatEphemeralState {
  sttRecording: boolean;
  sttInterimText: string;
  slashMenuOpen: boolean;
  slashMenuItems: SlashCommandDef[];
  slashMenuIndex: number;
  slashMenuMode: "command" | "args";
  slashMenuCommand: SlashCommandDef | null;
  slashMenuArgItems: string[];
  searchOpen: boolean;
  searchQuery: string;
  pinnedExpanded: boolean;
  skillsWorkbenchFilter: SkillsWorkbenchFilter;
  uploadNotice: ChatUploadNotice | null;
  uploadNoticeTimer: number | null;
}

function createChatEphemeralState(): ChatEphemeralState {
  return {
    sttRecording: false,
    sttInterimText: "",
    slashMenuOpen: false,
    slashMenuItems: [],
    slashMenuIndex: 0,
    slashMenuMode: "command",
    slashMenuCommand: null,
    slashMenuArgItems: [],
    searchOpen: false,
    searchQuery: "",
    pinnedExpanded: false,
    skillsWorkbenchFilter: "ready",
    uploadNotice: null,
    uploadNoticeTimer: null,
  };
}

const vs = createChatEphemeralState();

/**
 * Reset chat view ephemeral state when navigating away.
 * Stops STT recording and clears search/slash UI that should not survive navigation.
 */
export function resetChatViewState() {
  if (vs.sttRecording) {
    stopStt();
  }
  if (vs.uploadNoticeTimer !== null && typeof window !== "undefined") {
    window.clearTimeout(vs.uploadNoticeTimer);
  }
  Object.assign(vs, createChatEphemeralState());
}

export const cleanupChatModuleState = resetChatViewState;

function setUploadNotice(notice: ChatUploadNotice, props: Pick<ChatProps, "onRequestUpdate">) {
  if (vs.uploadNoticeTimer !== null && typeof window !== "undefined") {
    window.clearTimeout(vs.uploadNoticeTimer);
  }
  vs.uploadNotice = notice;
  vs.uploadNoticeTimer =
    typeof window === "undefined"
      ? null
      : window.setTimeout(() => {
          vs.uploadNotice = null;
          vs.uploadNoticeTimer = null;
          props.onRequestUpdate?.();
        }, 5000);
  props.onRequestUpdate?.();
}

function summarizeWorkspaceUploadResults(results: WorkspaceUploadResult[]): ChatUploadNotice {
  const acceptedCount = results.filter((result) => result.status === "accepted").length;
  const pendingCount = results.filter((result) => result.status === "pending_approval").length;
  if (acceptedCount > 0 && pendingCount > 0) {
    return {
      kind: "info",
      message: t("chat.workspaceUploadMixed", {
        acceptedCount: String(acceptedCount),
        pendingCount: String(pendingCount),
      }),
    };
  }
  if (pendingCount > 0) {
    return {
      kind: "info",
      message: t("chat.workspaceUploadPending", { count: String(pendingCount) }),
    };
  }
  return {
    kind: "success",
    message: t("chat.workspaceUploadAccepted", { count: String(acceptedCount) }),
  };
}

function adjustTextareaHeight(el: HTMLTextAreaElement) {
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 150)}px`;
}

function renderCompactionIndicator(status: CompactionIndicatorStatus | null | undefined) {
  if (!status) {
    return nothing;
  }
  if (status.phase === "active") {
    return html`
      <div
        class="compaction-indicator compaction-indicator--active"
        role="status"
        aria-live="polite"
      >
        ${icons.loader} ${t("chat.compactionActive")}
      </div>
    `;
  }
  if (status.phase === "retrying") {
    return html`
      <div
        class="compaction-indicator compaction-indicator--active"
        role="status"
        aria-live="polite"
      >
        ${icons.loader} ${t("chat.compactionRetrying")}
      </div>
    `;
  }
  if (status.phase === "complete" && status.completedAt) {
    const elapsed = Date.now() - status.completedAt;
    if (elapsed < COMPACTION_TOAST_DURATION_MS) {
      return html`
        <div
          class="compaction-indicator compaction-indicator--complete"
          role="status"
          aria-live="polite"
        >
          ${icons.check} ${t("chat.compactionComplete")}
        </div>
      `;
    }
  }
  return nothing;
}

function renderFallbackIndicator(status: FallbackIndicatorStatus | null | undefined) {
  if (!status) {
    return nothing;
  }
  const phase = status.phase ?? "active";
  const elapsed = Date.now() - status.occurredAt;
  if (elapsed >= FALLBACK_TOAST_DURATION_MS) {
    return nothing;
  }
  const details = [
    `${t("chat.fallbackSelectedLabel")}: ${status.selected}`,
    phase === "cleared"
      ? `${t("chat.fallbackActiveLabel")}: ${status.selected}`
      : `${t("chat.fallbackActiveLabel")}: ${status.active}`,
    phase === "cleared" && status.previous
      ? `${t("chat.fallbackPreviousLabel")}: ${status.previous}`
      : null,
    status.reason ? `${t("chat.fallbackReasonLabel")}: ${status.reason}` : null,
    status.attempts.length > 0
      ? `${t("chat.fallbackAttemptsLabel")}: ${status.attempts.slice(0, 3).join(" | ")}`
      : null,
  ]
    .filter(Boolean)
    .join(" • ");
  const message =
    phase === "cleared"
      ? t("chat.fallbackCleared", { selected: status.selected })
      : t("chat.fallbackActive", { active: status.active });
  const className =
    phase === "cleared"
      ? "compaction-indicator compaction-indicator--fallback-cleared"
      : "compaction-indicator compaction-indicator--fallback";
  const icon = phase === "cleared" ? icons.check : icons.brain;
  return html`
    <div class=${className} role="status" aria-live="polite" title=${details}>
      ${icon} ${message}
    </div>
  `;
}

/**
 * Compact notice when context usage reaches 85%+.
 * Progressively shifts from amber (85%) to red (90%+).
 */
/** Parse a 6-digit CSS hex color string to [r, g, b] integer components. */
function parseHexRgb(hex: string): [number, number, number] | null {
  const h = hex.trim().replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(h)) {
    return null;
  }
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

let cachedThemeNoticeColors: {
  warnHex: string;
  dangerHex: string;
  warnRgb: [number, number, number];
  dangerRgb: [number, number, number];
} | null = null;

function getThemeNoticeColors() {
  if (cachedThemeNoticeColors) {
    return cachedThemeNoticeColors;
  }
  const rootStyle = getComputedStyle(document.documentElement);
  const warnHex = rootStyle.getPropertyValue("--warn").trim() || "#f59e0b";
  const dangerHex = rootStyle.getPropertyValue("--danger").trim() || "#ef4444";
  cachedThemeNoticeColors = {
    warnHex,
    dangerHex,
    warnRgb: parseHexRgb(warnHex) ?? [245, 158, 11],
    dangerRgb: parseHexRgb(dangerHex) ?? [239, 68, 68],
  };
  return cachedThemeNoticeColors;
}

function renderContextNotice(
  session: GatewaySessionRow | undefined,
  defaultContextTokens: number | null,
) {
  if (session?.totalTokensFresh === false) {
    return nothing;
  }
  const used = session?.totalTokens ?? 0;
  const limit = session?.contextTokens ?? defaultContextTokens ?? 0;
  if (!used || !limit) {
    return nothing;
  }
  const ratio = used / limit;
  if (ratio < 0.85) {
    return nothing;
  }
  const pct = Math.min(Math.round(ratio * 100), 100);
  // Read theme semantic tokens so color tracks the active theme (Dash, dark, light …)
  const { warnRgb, dangerRgb } = getThemeNoticeColors();
  const [wr, wg, wb] = warnRgb;
  const [dr, dg, db] = dangerRgb;
  // Blend from --warn at 85% usage to --danger at 95%+ usage
  const blend = Math.min(Math.max((ratio - 0.85) / 0.1, 0), 1);
  const r = Math.round(wr + (dr - wr) * blend);
  const g = Math.round(wg + (dg - wg) * blend);
  const b = Math.round(wb + (db - wb) * blend);
  const color = `rgb(${r}, ${g}, ${b})`;
  const bgOpacity = 0.08 + 0.08 * blend;
  const bg = `rgba(${r}, ${g}, ${b}, ${bgOpacity})`;
  return html`
    <div class="context-notice" role="status" style="--ctx-color:${color};--ctx-bg:${bg}">
      <svg
        class="context-notice__icon"
        width="24"
        height="24"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      >
        <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" />
        <line x1="12" y1="9" x2="12" y2="13" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
      <span>${t("chat.contextUsed", { percent: String(pct) })}</span>
      <span class="context-notice__detail"
        >${formatTokensCompact(used)} / ${formatTokensCompact(limit)}</span
      >
    </div>
  `;
}

/** Format token count compactly (e.g. 128000 → "128k"). */
function formatTokensCompact(n: number): string {
  if (n >= 1_000_000) {
    return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  }
  if (n >= 1_000) {
    return `${(n / 1_000).toFixed(1).replace(/\.0$/, "")}k`;
  }
  return String(n);
}

function generateAttachmentId(): string {
  return `att-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

function summarizeSkillBuckets(skills: SkillStatusEntry[]) {
  let ready = 0;
  let disabled = 0;
  for (const skill of skills) {
    if (!isSkillConfiguredForWorkbench(skill)) {
      continue;
    }
    if (isSkillDisabledForWorkbench(skill)) {
      disabled += 1;
      continue;
    }
    ready += 1;
  }
  return { ready, disabled };
}

function countMissingSkillRequirements(skill: SkillStatusEntry) {
  return (
    skill.missing.bins.length +
    skill.missing.env.length +
    skill.missing.config.length +
    skill.missing.os.length
  );
}

function isSkillConfiguredForWorkbench(skill: SkillStatusEntry) {
  return countMissingSkillRequirements(skill) === 0;
}

function isSkillDisabledForWorkbench(skill: SkillStatusEntry) {
  return skill.disabled || skill.blockedByAllowlist;
}

function isWorkspaceSkillForWorkbench(skill: SkillStatusEntry) {
  return skill.source === "ironagent-workspace" || skill.source === "workspace";
}

function getWorkbenchSkills(skills: SkillStatusEntry[]) {
  return skills.filter(isWorkspaceSkillForWorkbench);
}

function getSkillsWorkbenchStatus(skill: SkillStatusEntry): SkillsWorkbenchStatus {
  return isSkillDisabledForWorkbench(skill) ? "disabled" : "ready";
}

function skillMatchesWorkbenchFilter(skill: SkillStatusEntry, filter: SkillsWorkbenchFilter) {
  return getSkillsWorkbenchStatus(skill) === filter;
}

function getSkillsWorkbenchStatusLabel(skill: SkillStatusEntry) {
  const status = getSkillsWorkbenchStatus(skill);
  if (status === "disabled") {
    return t("chat.skillStateDisabled");
  }
  return t("chat.skillStateReady");
}

function countEnabledTools(result: ToolsEffectiveResult | null | undefined): number {
  return result?.groups.reduce((total, group) => total + group.tools.length, 0) ?? 0;
}

function appendWorkspaceReference(props: ChatProps, filePath: string) {
  const draft = props.getDraft?.() ?? props.draft;
  const prefix = draft.trim().length > 0 ? "\n" : "";
  props.onDraftChange(`${draft}${prefix}${t("chat.useWorkspaceFile", { path: filePath })}`);
}

function renderWorkbenchSectionHeader(params: {
  title: string;
  subtitle?: string;
  actionLabel?: string;
  onAction?: () => void;
  disabled?: boolean;
}) {
  return html`
    <div class="chat-workbench__section-header">
      <div>
        <div class="chat-workbench__section-title">${params.title}</div>
        ${params.subtitle
          ? html`<div class="chat-workbench__section-subtitle">${params.subtitle}</div>`
          : nothing}
      </div>
      ${params.onAction
        ? html`
            <button
              type="button"
              class="btn btn--sm"
              ?disabled=${params.disabled}
              @click=${params.onAction}
            >
              ${params.actionLabel ?? t("common.refresh")}
            </button>
          `
        : nothing}
    </div>
  `;
}

function renderQueuePanel(props: ChatProps) {
  if (!props.queue.length) {
    return nothing;
  }
  return html`
    <div class="chat-queue" role="status" aria-live="polite">
      <div class="chat-queue__title">
        ${t("chat.queuedMessages", { count: String(props.queue.length) })}
      </div>
      <div class="chat-queue__list">
        ${props.queue.map(
          (item) => html`
            <div class="chat-queue__item">
              <div class="chat-queue__text">
                ${item.text ||
                (item.attachments?.length
                  ? t("chat.queuedFiles", { count: String(item.attachments.length) })
                  : "")}
              </div>
              <button
                class="btn chat-queue__remove"
                type="button"
                aria-label=${t("chat.removeQueuedMessage")}
                @click=${() => props.onQueueRemove(item.id)}
              >
                ${icons.x}
              </button>
            </div>
          `,
        )}
      </div>
    </div>
  `;
}

function renderWorkspaceWorkbench(props: ChatProps) {
  const workspaceFiles = props.workspaceFiles?.files ?? [];
  const { persistentFiles, tempFiles, missingWorkspaceFiles } = groupWorkbenchFiles({
    workspaceFiles,
    agentFiles: [],
    agentWorkspace: null,
  });
  const workspaceLabel = formatDisplayPath(
    props.workspaceFiles?.workspace ?? t("chat.workspaceDefaultLabel"),
  );
  return html`
    <section class="chat-workbench__section">
      ${renderWorkbenchSectionHeader({
        title: t("chat.workspaceTitle"),
        subtitle: workspaceLabel,
        onAction: props.onRefreshWorkspaceFiles,
        disabled: props.workspaceFilesLoading,
      })}
      ${props.workspaceFilesError
        ? html`<div class="chat-workbench__empty chat-workbench__empty--error">
            ${props.workspaceFilesError}
          </div>`
        : nothing}
      ${props.onUploadWorkspaceFiles
        ? html`
            <div class="chat-workbench__actions">
              <button
                type="button"
                class="btn btn--sm"
                ?disabled=${props.workspaceFilesLoading}
                @click=${(e: Event) => handleWorkspaceUploadClick(e)}
              >
                ${t("chat.uploadWorkspaceFiles")}
              </button>
              <input
                type="file"
                accept=${CHAT_ATTACHMENT_ACCEPT}
                multiple
                class="chat-workbench__upload-input"
                @change=${(e: Event) => handleWorkspaceUploadSelect(e, props)}
              />
            </div>
          `
        : nothing}
      <div class="chat-workbench__stats">
        <span class="chat-workbench__stat"
          >${t("chat.workspaceReady", {
            count: String(persistentFiles.length + tempFiles.length),
          })}</span
        >
        <span class="chat-workbench__stat"
          >${t("chat.workspaceMissing", { count: String(missingWorkspaceFiles.length) })}</span
        >
      </div>
      ${props.workspaceFilesLoading
        ? html`<div class="chat-workbench__empty">${t("chat.loadingWorkspaceFiles")}</div>`
        : html`
            <div class="chat-workbench__list">
              ${renderWorkspaceFileGroup(
                props,
                t("chat.workspacePersistentLabel"),
                persistentFiles,
                "persistent",
              )}
              ${renderWorkspaceFileGroup(props, t("chat.workspaceTempLabel"), tempFiles, "temp")}
            </div>
          `}
    </section>
  `;
}

function handleWorkspaceUploadClick(e: Event): void {
  const section = (e.currentTarget as HTMLElement | null)?.closest(".chat-workbench__section");
  section?.querySelector<HTMLInputElement>(".chat-workbench__upload-input")?.click();
}

async function handleWorkspaceUploadSelect(e: Event, props: ChatProps): Promise<void> {
  const input = e.target as HTMLInputElement;
  const selectedFiles = Array.from(input.files ?? []);
  input.value = "";
  if (selectedFiles.length === 0 || !props.onUploadWorkspaceFiles) {
    return;
  }
  const results = await props.onUploadWorkspaceFiles(selectedFiles);
  setUploadNotice(summarizeWorkspaceUploadResults(results), props);
}

function normalizeWorkbenchPath(path: string, workspaceRoot?: string | null): string {
  const normalized = path.replaceAll("\\", "/").trim();
  if (!normalized) {
    return "";
  }
  const normalizedRoot = workspaceRoot?.replaceAll("\\", "/").replace(/\/+$/, "") ?? "";
  if (normalizedRoot && normalized.startsWith(`${normalizedRoot}/`)) {
    return normalized.slice(normalizedRoot.length + 1);
  }
  if (normalized.startsWith("/workspace/")) {
    return normalized.slice("/workspace/".length);
  }
  return normalized.replace(/^\.\/+/, "");
}

function inferWorkbenchBucket(path: string): "persistent" | "temp" | null {
  if (path === "persistent" || path.startsWith("persistent/")) {
    return "persistent";
  }
  if (path === "temp" || path.startsWith("temp/")) {
    return "temp";
  }
  return null;
}

function mergeWorkbenchFileEntry(existing: AgentFileEntry | undefined, incoming: AgentFileEntry) {
  if (!existing) {
    return incoming;
  }
  return {
    ...incoming,
    ...existing,
    path: incoming.path || existing.path,
    bucket: existing.bucket ?? incoming.bucket,
    viewUrl: existing.viewUrl ?? incoming.viewUrl,
    downloadUrl: existing.downloadUrl ?? incoming.downloadUrl,
    fileId: existing.fileId ?? incoming.fileId,
    contentType: existing.contentType ?? incoming.contentType,
    sourceType: existing.sourceType ?? incoming.sourceType,
    sourceRef: existing.sourceRef ?? incoming.sourceRef,
  };
}

function groupWorkbenchFiles(params: {
  workspaceFiles: AgentFileEntry[];
  agentFiles: AgentFileEntry[];
  agentWorkspace?: string | null;
}) {
  const persistent = new Map<string, AgentFileEntry>();
  const temp = new Map<string, AgentFileEntry>();
  const assistant = new Map<string, AgentFileEntry>();
  const missingWorkspaceFiles = params.workspaceFiles.filter((file) => file.missing);
  const missingAssistantFiles = params.agentFiles.filter((file) => file.missing);

  const addEntry = (entry: AgentFileEntry, source: "workspace" | "agent") => {
    const normalizedPath = normalizeWorkbenchPath(
      entry.path || entry.name,
      source === "agent" ? params.agentWorkspace : null,
    );
    if (!normalizedPath || entry.missing) {
      return;
    }
    const bucket = entry.bucket ?? inferWorkbenchBucket(normalizedPath);
    const mappedEntry: AgentFileEntry = {
      ...entry,
      path: normalizedPath,
      ...(bucket ? { bucket } : {}),
    };
    if (bucket === "persistent") {
      persistent.set(
        normalizedPath,
        mergeWorkbenchFileEntry(persistent.get(normalizedPath), mappedEntry),
      );
      return;
    }
    if (bucket === "temp") {
      temp.set(normalizedPath, mergeWorkbenchFileEntry(temp.get(normalizedPath), mappedEntry));
      return;
    }
    assistant.set(
      normalizedPath,
      mergeWorkbenchFileEntry(assistant.get(normalizedPath), mappedEntry),
    );
  };

  for (const file of params.workspaceFiles) {
    addEntry(file, "workspace");
  }
  for (const file of params.agentFiles) {
    addEntry(file, "agent");
  }

  return {
    persistentFiles: [...persistent.values()],
    tempFiles: [...temp.values()],
    assistantFiles: [...assistant.values()],
    missingWorkspaceFiles,
    missingAssistantFiles,
  };
}

function renderWorkspaceFileGroup(
  props: ChatProps,
  label: string,
  files: AgentFileEntry[],
  kind: "persistent" | "temp" | "assistant",
) {
  const tree = buildWorkspaceFileTree(files, kind);
  return html`
    <details
      class="chat-workbench__group chat-workbench__group--collapsible"
      ?open=${files.length > 0}
    >
      <summary class="chat-workbench__group-summary">
        <span class="chat-workbench__group-summary-main">
          <span class="chat-workbench__group-chevron" aria-hidden="true"
            >${icons.chevronRight}</span
          >
          <span class="chat-workbench__group-icon" aria-hidden="true">${icons.folder}</span>
          <span class="chat-workbench__group-label">${label}/</span>
        </span>
        <span class="chat-workbench__item-badge">${files.length}</span>
      </summary>
      <div class="chat-workbench__group-body">
        ${files.length > 0
          ? renderWorkspaceTreeNodes(props, tree, 0)
          : html`
              <div class="chat-workbench__empty">
                ${kind === "assistant"
                  ? t("chat.noWorkspaceAssistantFiles")
                  : t("chat.noWorkspaceBucketFiles")}
              </div>
            `}
      </div>
    </details>
  `;
}

function buildWorkspaceFileTree(
  files: AgentFileEntry[],
  kind: "persistent" | "temp" | "assistant",
): WorkspaceTreeNode[] {
  type MutableFolder = {
    name: string;
    folders: Map<string, MutableFolder>;
    files: AgentFileEntry[];
  };

  const root: MutableFolder = {
    name: "",
    folders: new Map(),
    files: [],
  };

  const normalizeRelativePath = (file: AgentFileEntry) => {
    const normalized = file.path.replaceAll("\\", "/").replace(/^\/+/, "");
    if (kind === "persistent" && normalized.startsWith("persistent/")) {
      return normalized.slice("persistent/".length);
    }
    if (kind === "temp" && normalized.startsWith("temp/")) {
      return normalized.slice("temp/".length);
    }
    return normalized;
  };

  for (const file of files) {
    const relativePath = normalizeRelativePath(file);
    const parts = relativePath.split("/").filter(Boolean);
    if (parts.length <= 1) {
      root.files.push(file);
      continue;
    }
    let cursor = root;
    for (const part of parts.slice(0, -1)) {
      const next = cursor.folders.get(part) ?? {
        name: part,
        folders: new Map(),
        files: [],
      };
      cursor.folders.set(part, next);
      cursor = next;
    }
    cursor.files.push(file);
  }

  const toNodes = (folder: MutableFolder): WorkspaceTreeNode[] => {
    const folderNodes = [...folder.folders.values()]
      .toSorted((a, b) => a.name.localeCompare(b.name))
      .map((entry) => {
        const children = toNodes(entry);
        return {
          kind: "folder" as const,
          name: entry.name,
          itemCount: countWorkspaceTreeItems(children),
          children,
        };
      });
    const fileNodes = [...folder.files]
      .toSorted((a, b) => a.path.localeCompare(b.path))
      .map((file) => ({
        kind: "file" as const,
        file,
      }));
    return [...folderNodes, ...fileNodes];
  };

  return toNodes(root);
}

function countWorkspaceTreeItems(nodes: WorkspaceTreeNode[]): number {
  return nodes.reduce(
    (total, node) => total + (node.kind === "file" ? 1 : countWorkspaceTreeItems(node.children)),
    0,
  );
}

function renderWorkspaceTreeNodes(
  props: ChatProps,
  nodes: WorkspaceTreeNode[],
  depth: number,
): TemplateResult[] {
  return nodes.map((node) =>
    node.kind === "folder"
      ? html`
          <details class="chat-workbench__tree-folder" ?open=${depth === 0}>
            <summary
              class="chat-workbench__tree-folder-summary"
              style=${`--workspace-tree-depth:${depth};`}
            >
              <span class="chat-workbench__tree-folder-main">
                <span class="chat-workbench__tree-chevron" aria-hidden="true"
                  >${icons.chevronRight}</span
                >
                <span class="chat-workbench__tree-folder-icon" aria-hidden="true"
                  >${icons.folder}</span
                >
                <span class="chat-workbench__tree-folder-label">${node.name}/</span>
              </span>
              <span class="chat-workbench__item-badge">${node.itemCount}</span>
            </summary>
            <div class="chat-workbench__tree-folder-children">
              ${renderWorkspaceTreeNodes(props, node.children, depth + 1)}
            </div>
          </details>
        `
      : html`
          <div class="chat-workbench__tree-file" style=${`--workspace-tree-depth:${depth};`}>
            ${renderWorkspaceFileItem(props, node.file)}
          </div>
        `,
  );
}

function renderWorkspaceFileItem(props: ChatProps, file: AgentFileEntry) {
  const downloadName = file.name.split("/").pop() || file.name;
  const canDelete = Boolean(file.fileId && props.onDeleteWorkspaceFile);
  return html`
    <div class="chat-workbench__item chat-workbench__item--file">
      <button
        type="button"
        class="chat-workbench__item-main chat-workbench__item-main--button"
        @click=${() => appendWorkspaceReference(props, file.path)}
        title=${t("chat.insertWorkspaceFile")}
      >
        <span class="chat-workbench__item-title">${downloadName}</span>
        <span class="chat-workbench__item-sub">${formatDisplayPath(file.path)}</span>
      </button>
      <span class="chat-workbench__item-actions">
        ${file.downloadUrl
          ? html`
              <a
                class="chat-workbench__item-action"
                href=${file.viewUrl ?? file.downloadUrl}
                target="_blank"
                rel="noreferrer"
                title=${t("chat.viewWorkspaceFile")}
                aria-label=${t("chat.viewWorkspaceFile")}
              >
                ${icons.eye}
              </a>
              <a
                class="chat-workbench__item-action"
                href=${file.downloadUrl}
                download=${downloadName}
                title=${t("chat.downloadWorkspaceFile")}
                aria-label=${t("chat.downloadWorkspaceFile")}
              >
                ${icons.download}
              </a>
              ${canDelete
                ? html`
                    <button
                      type="button"
                      class="chat-workbench__item-action"
                      title=${t("chat.deleteWorkspaceFile")}
                      aria-label=${t("chat.deleteWorkspaceFile")}
                      @click=${() => void requestDeleteWorkspaceFile(props, file)}
                    >
                      ${icons.trash}
                    </button>
                  `
                : nothing}
            `
          : html`<span class="chat-workbench__item-meta">${icons.paperclip}</span>`}
      </span>
    </div>
  `;
}

async function requestDeleteWorkspaceFile(props: ChatProps, file: AgentFileEntry): Promise<void> {
  if (!file.fileId || !props.onDeleteWorkspaceFile) {
    return;
  }
  const confirmed =
    typeof window === "undefined" ||
    window.confirm(t("chat.deleteWorkspaceFileConfirm", { name: file.name }));
  if (!confirmed) {
    return;
  }
  const result = await props.onDeleteWorkspaceFile(file.fileId, file.bucket);
  if (result && typeof result === "object" && result.status === "pending_approval") {
    setUploadNotice(
      {
        kind: "info",
        message: result.message?.trim() || t("chat.workspaceDeletePending", { name: file.name }),
      },
      props,
    );
  }
}

function renderApprovalWorkbench(props: ChatProps) {
  const approvals = (props.approvals ?? []).filter((entry) =>
    isControlUiActionableApproval(entry, {
      canResolveGatewayApprovals: props.canResolveGatewayApprovals,
    }),
  );
  return html`
    <section class="chat-workbench__section">
      ${renderWorkbenchSectionHeader({
        title: t("chat.approvalsTitle"),
        subtitle: t("chat.approvalsPending", { count: String(approvals.length) }),
        actionLabel: t("common.refresh"),
        onAction: props.onRefreshApprovals,
        disabled: props.refreshingApprovals,
      })}
      ${approvals.length === 0
        ? html`<div class="chat-workbench__empty">${t("chat.noPendingApprovals")}</div>`
        : html`
            <div class="chat-workbench__list">
              ${approvals.slice(0, 3).map((entry) => {
                return html`<div class="chat-workbench__item chat-workbench__item--static">
                  <span class="chat-workbench__item-main">
                    <span class="chat-workbench__item-title">
                      ${entry.kind === "plugin"
                        ? (entry.pluginTitle ?? entry.request.command)
                        : entry.request.command}
                    </span>
                    <span class="chat-workbench__item-sub">
                      ${entry.pluginDescription ??
                      entry.pluginMetadata?.target ??
                      entry.request.resolvedPath ??
                      entry.request.cwd ??
                      entry.request.security ??
                      t("chat.approvalRequired")}
                    </span>
                    ${entry.pluginDescription && entry.pluginMetadata?.target
                      ? html`
                          <span class="chat-workbench__item-sub">
                            ${entry.pluginMetadata.target}
                          </span>
                        `
                      : nothing}
                  </span>
                  <span class="chat-workbench__item-badge">${entry.kind}</span>
                  ${props.onApprovalDecision
                    ? html`
                        <div class="exec-approval-actions">
                          <button
                            class="btn primary"
                            type="button"
                            @click=${() => props.onApprovalDecision?.(entry.id, "allow-once")}
                          >
                            ${t("chat.approvalApproveOnce")}
                          </button>
                          <button
                            class="btn danger"
                            type="button"
                            @click=${() => props.onApprovalDecision?.(entry.id, "deny")}
                          >
                            ${t("chat.approvalReject")}
                          </button>
                        </div>
                      `
                    : nothing}
                </div>`;
              })}
            </div>
          `}
    </section>
  `;
}

function renderSkillsWorkbench(props: ChatProps) {
  const skills = getWorkbenchSkills(props.skillsReport?.skills ?? []);
  const configuredSkills = skills.filter(isSkillConfiguredForWorkbench);
  const summary = summarizeSkillBuckets(configuredSkills);
  const activeFilter = vs.skillsWorkbenchFilter;
  const filteredSkills = configuredSkills.filter((skill) =>
    skillMatchesWorkbenchFilter(skill, activeFilter),
  );
  const skillFilters: Array<{
    id: SkillsWorkbenchFilter;
    label: string;
  }> = [
    { id: "ready", label: t("chat.skillsReady", { count: String(summary.ready) }) },
    { id: "disabled", label: t("chat.skillsDisabled", { count: String(summary.disabled) }) },
  ];
  return html`
    <section class="chat-workbench__section">
      ${renderWorkbenchSectionHeader({
        title: t("chat.skillsTitle"),
        subtitle: formatDisplayPath(props.skillsReport?.workspaceDir ?? t("chat.skillsOverview")),
        onAction: props.onRefreshSkills,
        disabled: props.skillsLoading,
      })}
      ${props.skillsError
        ? html`<div class="chat-workbench__empty chat-workbench__empty--error">
            ${props.skillsError}
          </div>`
        : nothing}
      <div class="chat-workbench__stats chat-workbench__stats--filters">
        ${skillFilters.map(
          (filter) => html`
            <button
              type="button"
              class="chat-workbench__stat chat-workbench__stat--button ${activeFilter === filter.id
                ? "is-active"
                : ""}"
              aria-pressed=${activeFilter === filter.id}
              @click=${() => {
                if (vs.skillsWorkbenchFilter === filter.id) {
                  return;
                }
                vs.skillsWorkbenchFilter = filter.id;
                props.onRequestUpdate?.();
              }}
            >
              ${filter.label}
            </button>
          `,
        )}
      </div>
      ${props.skillsLoading
        ? html`<div class="chat-workbench__empty">${t("chat.loadingSkills")}</div>`
        : skills.length === 0
          ? html`<div class="chat-workbench__empty">${t("chat.noInstalledSkills")}</div>`
          : configuredSkills.length === 0
            ? html`<div class="chat-workbench__empty">${t("chat.noConfiguredSkills")}</div>`
            : filteredSkills.length > 0
              ? html`
                  <div class="chat-workbench__list">
                    ${filteredSkills.map(
                      (skill) => html`
                        <div
                          class="chat-workbench__item chat-workbench__item--static chat-workbench__item--skill"
                        >
                          <span class="chat-workbench__item-main">
                            <span class="chat-workbench__item-title">${skill.name}</span>
                            <span class="chat-workbench__item-sub">${skill.description}</span>
                          </span>
                          <div class="chat-workbench__skill-meta">
                            <button
                              type="button"
                              class="chat-workbench__skill-toggle chat-workbench__skill-toggle--${getSkillsWorkbenchStatus(
                                skill,
                              )}"
                              data-skill-key=${skill.skillKey}
                              aria-pressed=${!skill.disabled}
                              aria-label=${`${
                                skill.disabled ? t("chat.enableSkill") : t("chat.disableSkill")
                              }: ${skill.name}`}
                              title=${skill.disabled
                                ? t("chat.enableSkill")
                                : t("chat.disableSkill")}
                              ?disabled=${props.skillsBusyKey === skill.skillKey ||
                              !props.onToggleSkillEnabled}
                              @click=${() =>
                                props.onToggleSkillEnabled?.(skill.skillKey, skill.disabled)}
                            >
                              ${props.skillsBusyKey === skill.skillKey
                                ? html`<span aria-hidden="true">${icons.loader}</span>`
                                : nothing}
                              <span>${getSkillsWorkbenchStatusLabel(skill)}</span>
                            </button>
                          </div>
                        </div>
                      `,
                    )}
                  </div>
                `
              : html`<div class="chat-workbench__empty">${t("chat.noSkillsForFilter")}</div>`}
    </section>
  `;
}

function renderToolsWorkbench(props: ChatProps) {
  const toolCount = countEnabledTools(props.toolsEffectiveResult);
  const groupCount = props.toolsEffectiveResult?.groups.length ?? 0;
  return html`
    <section class="chat-workbench__section">
      ${renderWorkbenchSectionHeader({
        title: t("chat.liveToolsTitle"),
        subtitle: props.toolsEffectiveResult?.profile
          ? t("chat.liveToolsProfile", { profile: props.toolsEffectiveResult.profile })
          : t("chat.liveToolsEnvelope"),
      })}
      ${props.toolsEffectiveError
        ? html`<div class="chat-workbench__empty chat-workbench__empty--error">
            ${props.toolsEffectiveError}
          </div>`
        : nothing}
      <div class="chat-workbench__stats">
        <span class="chat-workbench__stat"
          >${t("chat.liveToolsEnabled", { count: String(toolCount) })}</span
        >
        <span class="chat-workbench__stat"
          >${t("chat.liveToolsGroups", { count: String(groupCount) })}</span
        >
      </div>
      ${props.toolsEffectiveLoading
        ? html`<div class="chat-workbench__empty">${t("chat.loadingLiveTools")}</div>`
        : props.toolsEffectiveResult?.groups?.length
          ? html`
              <div class="chat-workbench__list">
                ${props.toolsEffectiveResult.groups.slice(0, 4).map(
                  (group) => html`
                    <div class="chat-workbench__item chat-workbench__item--static">
                      <span class="chat-workbench__item-main">
                        <span class="chat-workbench__item-title">${group.label}</span>
                        <span class="chat-workbench__item-sub"
                          >${t("chat.liveToolsToolCount", {
                            count: String(group.tools.length),
                          })}</span
                        >
                      </span>
                      <span class="chat-workbench__item-badge">${group.source}</span>
                    </div>
                  `,
                )}
              </div>
            `
          : html`<div class="chat-workbench__empty">${t("chat.noLiveTools")}</div>`}
    </section>
  `;
}

function renderWorkbenchRail(props: ChatProps) {
  const isCollapsed = Boolean(props.workbenchCollapsed);
  const approvals = props.approvals ?? [];
  const skillSummary = summarizeSkillBuckets(getWorkbenchSkills(props.skillsReport?.skills ?? []));
  const enabledTools = countEnabledTools(props.toolsEffectiveResult);
  const workspaceCount = props.workspaceFiles?.files.filter((file) => !file.missing).length ?? 0;
  const logoUrl = agentLogoUrl(props.basePath ?? "");
  return html`
    <aside class="chat-workbench ${isCollapsed ? "chat-workbench--collapsed" : ""}">
      <div class="chat-workbench__header">
        ${isCollapsed
          ? html`
              <div class="chat-workbench__logo-shell" aria-hidden="true">
                <img class="chat-workbench__logo" src=${logoUrl} alt=${PRODUCT_NAME} />
              </div>
            `
          : html`
              <div class="chat-workbench__title-group">
                <div class="chat-workbench__title">${t("chat.workbenchTitle")}</div>
                <div class="chat-workbench__subtitle">${t("chat.workbenchSubtitle")}</div>
              </div>
            `}
        ${props.onToggleWorkbench
          ? html`
              <button
                type="button"
                class="btn btn--sm btn--icon chat-workbench__toggle"
                aria-pressed=${isCollapsed}
                title=${isCollapsed ? t("chat.expandWorkbench") : t("chat.collapseWorkbench")}
                @click=${props.onToggleWorkbench}
              >
                ${icons.panelRightOpen}
              </button>
            `
          : nothing}
      </div>
      ${isCollapsed
        ? html`
            <div class="chat-workbench__collapsed-metrics">
              <div
                class="chat-workbench__collapsed-metric"
                title=${t("chat.collapsedMetricWorkspace")}
                aria-label=${`${t("chat.collapsedMetricWorkspace")}: ${workspaceCount}`}
              >
                <span class="chat-workbench__collapsed-icon" aria-hidden="true"
                  >${icons.folder}</span
                >
                <span class="chat-workbench__collapsed-count">${workspaceCount}</span>
              </div>
              <div
                class="chat-workbench__collapsed-metric"
                title=${t("chat.collapsedMetricApprovals")}
                aria-label=${`${t("chat.collapsedMetricApprovals")}: ${approvals.length}`}
              >
                <span class="chat-workbench__collapsed-icon" aria-hidden="true"
                  >${icons.wrench}</span
                >
                <span class="chat-workbench__collapsed-count">${approvals.length}</span>
              </div>
              <div
                class="chat-workbench__collapsed-metric"
                title=${t("chat.collapsedMetricSkills")}
                aria-label=${`${t("chat.collapsedMetricSkills")}: ${skillSummary.ready}`}
              >
                <span class="chat-workbench__collapsed-icon" aria-hidden="true">${icons.zap}</span>
                <span class="chat-workbench__collapsed-count">${skillSummary.ready}</span>
              </div>
              <div
                class="chat-workbench__collapsed-metric"
                title=${t("chat.collapsedMetricTools")}
                aria-label=${`${t("chat.collapsedMetricTools")}: ${enabledTools}`}
              >
                <span class="chat-workbench__collapsed-icon" aria-hidden="true"
                  >${icons.brain}</span
                >
                <span class="chat-workbench__collapsed-count">${enabledTools}</span>
              </div>
            </div>
          `
        : html`
            <div class="chat-workbench__body">
              ${renderWorkspaceWorkbench(props)} ${renderApprovalWorkbench(props)}
              ${renderSkillsWorkbench(props)} ${renderToolsWorkbench(props)}
            </div>
          `}
    </aside>
  `;
}

function handlePaste(e: ClipboardEvent, props: ChatProps) {
  const items = e.clipboardData?.items;
  if (!items || !props.onAttachmentsChange) {
    return;
  }
  const imageItems: DataTransferItem[] = [];
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (item.type.startsWith("image/")) {
      imageItems.push(item);
    }
  }
  if (imageItems.length === 0) {
    return;
  }
  e.preventDefault();
  for (const item of imageItems) {
    const file = item.getAsFile();
    if (!file) {
      continue;
    }
    const current = props.attachments ?? [];
    props.onAttachmentsChange?.([...current, createChatAttachment(file)]);
  }
}

function createChatAttachment(file: File): ChatAttachment {
  const normalizedMimeType = file.type.trim() || "application/octet-stream";
  return {
    id: generateAttachmentId(),
    file,
    name: file.name,
    mimeType: normalizedMimeType,
    size: file.size,
    previewUrl: normalizedMimeType.startsWith("image/") ? URL.createObjectURL(file) : null,
  };
}

function addFileAttachments(files: File[], props: ChatProps) {
  if (!props.onAttachmentsChange) {
    return;
  }
  const current = props.attachments ?? [];
  const additions: ChatAttachment[] = [];
  for (const file of files) {
    if (!isSupportedChatAttachmentFile(file)) {
      continue;
    }
    additions.push(createChatAttachment(file));
  }
  if (additions.length === 0) {
    return;
  }
  props.onAttachmentsChange?.([...current, ...additions]);
}

function handleFileSelect(e: Event, props: ChatProps) {
  const input = e.target as HTMLInputElement;
  if (!input.files) {
    return;
  }
  const selectedFiles = Array.from(input.files);
  addFileAttachments(selectedFiles, props);
  input.value = "";
}

function handleDrop(e: DragEvent, props: ChatProps) {
  e.preventDefault();
  const files = e.dataTransfer?.files;
  if (!files) {
    return;
  }
  const selectedFiles = Array.from(files);
  addFileAttachments(selectedFiles, props);
}

function formatAttachmentSize(size: number): string {
  if (!Number.isFinite(size) || size < 1024) {
    return `${Math.max(0, Math.round(size))} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function renderAttachmentPreview(props: ChatProps): TemplateResult | typeof nothing {
  const attachments = props.attachments ?? [];
  if (attachments.length === 0) {
    return nothing;
  }
  return html`
    <div class="chat-attachments-preview">
      ${attachments.map(
        (att) => html`
          <div class="chat-attachment-thumb">
            ${att.previewUrl
              ? html`<img src=${att.previewUrl} alt=${t("chat.attachmentPreviewAlt")} />`
              : html`<div class="chat-attachment-thumb__file-icon">${icons.paperclip}</div>`}
            <div class="chat-attachment-thumb__meta">
              <span class="chat-attachment-thumb__name">${att.name}</span>
              <span class="chat-attachment-thumb__size">${formatAttachmentSize(att.size)}</span>
            </div>
            <button
              class="chat-attachment-remove"
              type="button"
              aria-label=${t("chat.removeAttachment")}
              @click=${() => {
                revokeChatAttachmentPreview(att);
                const next = (props.attachments ?? []).filter((a) => a.id !== att.id);
                props.onAttachmentsChange?.(next);
              }}
            >
              &times;
            </button>
          </div>
        `,
      )}
    </div>
  `;
}

function resetSlashMenuState(): void {
  vs.slashMenuMode = "command";
  vs.slashMenuCommand = null;
  vs.slashMenuArgItems = [];
  vs.slashMenuItems = [];
}

function updateSlashMenu(value: string, requestUpdate: () => void): void {
  // Arg mode: /command <partial-arg>
  const argMatch = value.match(/^\/(\S+)\s(.*)$/);
  if (argMatch) {
    const cmdName = argMatch[1].toLowerCase();
    const argFilter = argMatch[2].toLowerCase();
    const cmd = SLASH_COMMANDS.find((c) => c.name === cmdName);
    if (cmd?.argOptions?.length) {
      const filtered = argFilter
        ? cmd.argOptions.filter((opt) => opt.toLowerCase().startsWith(argFilter))
        : cmd.argOptions;
      if (filtered.length > 0) {
        vs.slashMenuMode = "args";
        vs.slashMenuCommand = cmd;
        vs.slashMenuArgItems = filtered;
        vs.slashMenuOpen = true;
        vs.slashMenuIndex = 0;
        vs.slashMenuItems = [];
        requestUpdate();
        return;
      }
    }
    vs.slashMenuOpen = false;
    resetSlashMenuState();
    requestUpdate();
    return;
  }

  // Command mode: /partial-command
  const match = value.match(/^\/(\S*)$/);
  if (match) {
    const items = getSlashCommandCompletions(match[1]);
    vs.slashMenuItems = items;
    vs.slashMenuOpen = items.length > 0;
    vs.slashMenuIndex = 0;
    vs.slashMenuMode = "command";
    vs.slashMenuCommand = null;
    vs.slashMenuArgItems = [];
  } else {
    vs.slashMenuOpen = false;
    resetSlashMenuState();
  }
  requestUpdate();
}

function selectSlashCommand(
  cmd: SlashCommandDef,
  props: ChatProps,
  requestUpdate: () => void,
): void {
  // Transition to arg picker when the command has fixed options
  if (cmd.argOptions?.length) {
    props.onDraftChange(`/${cmd.name} `);
    vs.slashMenuMode = "args";
    vs.slashMenuCommand = cmd;
    vs.slashMenuArgItems = cmd.argOptions;
    vs.slashMenuOpen = true;
    vs.slashMenuIndex = 0;
    vs.slashMenuItems = [];
    requestUpdate();
    return;
  }

  vs.slashMenuOpen = false;
  resetSlashMenuState();

  if (cmd.executeLocal && !cmd.args) {
    props.onDraftChange(`/${cmd.name}`);
    requestUpdate();
    props.onSend();
  } else {
    props.onDraftChange(`/${cmd.name} `);
    requestUpdate();
  }
}

function tabCompleteSlashCommand(
  cmd: SlashCommandDef,
  props: ChatProps,
  requestUpdate: () => void,
): void {
  // Tab: fill in the command text without executing
  if (cmd.argOptions?.length) {
    props.onDraftChange(`/${cmd.name} `);
    vs.slashMenuMode = "args";
    vs.slashMenuCommand = cmd;
    vs.slashMenuArgItems = cmd.argOptions;
    vs.slashMenuOpen = true;
    vs.slashMenuIndex = 0;
    vs.slashMenuItems = [];
    requestUpdate();
    return;
  }

  vs.slashMenuOpen = false;
  resetSlashMenuState();
  props.onDraftChange(cmd.args ? `/${cmd.name} ` : `/${cmd.name}`);
  requestUpdate();
}

function selectSlashArg(
  arg: string,
  props: ChatProps,
  requestUpdate: () => void,
  execute: boolean,
): void {
  const cmdName = vs.slashMenuCommand?.name ?? "";
  vs.slashMenuOpen = false;
  resetSlashMenuState();
  props.onDraftChange(`/${cmdName} ${arg}`);
  requestUpdate();
  if (execute) {
    props.onSend();
  }
}

function tokenEstimate(draft: string): string | null {
  if (draft.length < 100) {
    return null;
  }
  return `~${Math.ceil(draft.length / 4)} tokens`;
}

/**
 * Export chat markdown - delegates to shared utility.
 */
function exportMarkdown(props: ChatProps): void {
  exportChatMarkdown(props.messages, props.assistantName);
}

function renderWelcomeState(props: ChatProps): TemplateResult {
  const name = props.assistantName || t("chat.assistant");
  const avatar = resolveAgentAvatarUrl({
    identity: {
      avatar: props.assistantAvatar ?? undefined,
      avatarUrl: props.assistantAvatarUrl ?? undefined,
    },
  });
  const logoUrl = agentLogoUrl(props.basePath ?? "");

  return html`
    <div class="agent-chat__welcome" style="--agent-color: var(--accent)">
      <div class="agent-chat__welcome-glow"></div>
      ${avatar
        ? html`<img
            src=${avatar}
            alt=${name}
            style="width:56px; height:56px; border-radius:50%; object-fit:cover;"
          />`
        : html`<div class="agent-chat__avatar agent-chat__avatar--logo">
            <img src=${logoUrl} alt=${PRODUCT_NAME} />
          </div>`}
      <h2>${name}</h2>
      <div class="agent-chat__badges">
        <span class="agent-chat__badge"
          ><img src=${logoUrl} alt="" /> ${t("chat.welcomeReady")}</span
        >
      </div>
      <p class="agent-chat__hint">${t("chat.welcomeHint")}</p>
      <div class="agent-chat__suggestions">
        ${[
          t("chat.welcomeSuggestionWhatCanYouDo"),
          t("chat.welcomeSuggestionSummarize"),
          t("chat.welcomeSuggestionChannel"),
          t("chat.welcomeSuggestionStatus"),
        ].map(
          (text) => html`
            <button
              type="button"
              class="agent-chat__suggestion"
              @click=${() => {
                props.onDraftChange(text);
                props.onSend();
              }}
            >
              ${text}
            </button>
          `,
        )}
      </div>
    </div>
  `;
}

function renderSearchBar(requestUpdate: () => void): TemplateResult | typeof nothing {
  if (!vs.searchOpen) {
    return nothing;
  }
  return html`
    <div class="agent-chat__search-bar">
      ${icons.search}
      <input
        type="text"
        placeholder=${t("chat.searchPlaceholder")}
        aria-label=${t("chat.searchAria")}
        .value=${vs.searchQuery}
        @input=${(e: Event) => {
          vs.searchQuery = (e.target as HTMLInputElement).value;
          requestUpdate();
        }}
      />
      <button
        class="btn btn--ghost"
        aria-label=${t("chat.closeSearch")}
        @click=${() => {
          vs.searchOpen = false;
          vs.searchQuery = "";
          requestUpdate();
        }}
      >
        ${icons.x}
      </button>
    </div>
  `;
}

function renderPinnedSection(
  props: ChatProps,
  pinned: PinnedMessages,
  requestUpdate: () => void,
): TemplateResult | typeof nothing {
  const messages = Array.isArray(props.messages) ? props.messages : [];
  const entries: Array<{ index: number; text: string; role: string }> = [];
  for (const idx of pinned.indices) {
    const msg = messages[idx] as Record<string, unknown> | undefined;
    if (!msg) {
      continue;
    }
    const text = getPinnedMessageSummary(msg);
    const role = typeof msg.role === "string" ? msg.role : "unknown";
    entries.push({ index: idx, text, role });
  }
  if (entries.length === 0) {
    return nothing;
  }
  return html`
    <div class="agent-chat__pinned">
      <button
        class="agent-chat__pinned-toggle"
        @click=${() => {
          vs.pinnedExpanded = !vs.pinnedExpanded;
          requestUpdate();
        }}
      >
        ${icons.bookmark} ${t("chat.pinnedCount", { count: String(entries.length) })}
        <span class="collapse-chevron ${vs.pinnedExpanded ? "" : "collapse-chevron--collapsed"}"
          >${icons.chevronDown}</span
        >
      </button>
      ${vs.pinnedExpanded
        ? html`
            <div class="agent-chat__pinned-list">
              ${entries.map(
                ({ index, text, role }) => html`
                  <div class="agent-chat__pinned-item">
                    <span class="agent-chat__pinned-role"
                      >${role === "user" ? t("chat.you") : t("chat.assistant")}</span
                    >
                    <span class="agent-chat__pinned-text"
                      >${text.slice(0, 100)}${text.length > 100 ? "..." : ""}</span
                    >
                    <button
                      class="btn btn--ghost"
                      @click=${() => {
                        pinned.unpin(index);
                        requestUpdate();
                      }}
                      title=${t("chat.unpin")}
                    >
                      ${icons.x}
                    </button>
                  </div>
                `,
              )}
            </div>
          `
        : nothing}
    </div>
  `;
}

function renderSlashMenu(
  requestUpdate: () => void,
  props: ChatProps,
): TemplateResult | typeof nothing {
  if (!vs.slashMenuOpen) {
    return nothing;
  }

  // Arg-picker mode: show options for the selected command
  if (vs.slashMenuMode === "args" && vs.slashMenuCommand && vs.slashMenuArgItems.length > 0) {
    return html`
      <div class="slash-menu" role="listbox" aria-label=${t("chat.commandArguments")}>
        <div class="slash-menu-group">
          <div class="slash-menu-group__label">
            /${vs.slashMenuCommand.name} ${vs.slashMenuCommand.description}
          </div>
          ${vs.slashMenuArgItems.map(
            (arg, i) => html`
              <div
                class="slash-menu-item ${i === vs.slashMenuIndex ? "slash-menu-item--active" : ""}"
                role="option"
                aria-selected=${i === vs.slashMenuIndex}
                @click=${() => selectSlashArg(arg, props, requestUpdate, true)}
                @mouseenter=${() => {
                  vs.slashMenuIndex = i;
                  requestUpdate();
                }}
              >
                ${vs.slashMenuCommand?.icon
                  ? html`<span class="slash-menu-icon">${icons[vs.slashMenuCommand.icon]}</span>`
                  : nothing}
                <span class="slash-menu-name">${arg}</span>
                <span class="slash-menu-desc">/${vs.slashMenuCommand?.name} ${arg}</span>
              </div>
            `,
          )}
        </div>
        <div class="slash-menu-footer">
          <kbd>↑↓</kbd> ${t("chat.slashNavigate")} <kbd>Tab</kbd> ${t("chat.slashFill")}
          <kbd>Enter</kbd> ${t("chat.slashRun")} <kbd>Esc</kbd> ${t("chat.slashClose")}
        </div>
      </div>
    `;
  }

  // Command mode: show grouped commands
  if (vs.slashMenuItems.length === 0) {
    return nothing;
  }

  const grouped = new Map<
    SlashCommandCategory,
    Array<{ cmd: SlashCommandDef; globalIdx: number }>
  >();
  for (let i = 0; i < vs.slashMenuItems.length; i++) {
    const cmd = vs.slashMenuItems[i];
    const cat = cmd.category ?? "session";
    let list = grouped.get(cat);
    if (!list) {
      list = [];
      grouped.set(cat, list);
    }
    list.push({ cmd, globalIdx: i });
  }

  const sections: TemplateResult[] = [];
  for (const [cat, entries] of grouped) {
    sections.push(html`
      <div class="slash-menu-group">
        <div class="slash-menu-group__label">${getSlashCategoryLabel(cat)}</div>
        ${entries.map(
          ({ cmd, globalIdx }) => html`
            <div
              class="slash-menu-item ${globalIdx === vs.slashMenuIndex
                ? "slash-menu-item--active"
                : ""}"
              role="option"
              aria-selected=${globalIdx === vs.slashMenuIndex}
              @click=${() => selectSlashCommand(cmd, props, requestUpdate)}
              @mouseenter=${() => {
                vs.slashMenuIndex = globalIdx;
                requestUpdate();
              }}
            >
              ${cmd.icon ? html`<span class="slash-menu-icon">${icons[cmd.icon]}</span>` : nothing}
              <span class="slash-menu-name">/${cmd.name}</span>
              ${cmd.args ? html`<span class="slash-menu-args">${cmd.args}</span>` : nothing}
              <span class="slash-menu-desc">${cmd.description}</span>
              ${cmd.argOptions?.length
                ? html`<span class="slash-menu-badge"
                    >${t("chat.slashOptions", { count: String(cmd.argOptions.length) })}</span
                  >`
                : cmd.executeLocal && !cmd.args
                  ? html` <span class="slash-menu-badge">${t("chat.slashInstant")}</span> `
                  : nothing}
            </div>
          `,
        )}
      </div>
    `);
  }

  return html`
    <div class="slash-menu" role="listbox" aria-label=${t("chat.slashCommands")}>
      ${sections}
      <div class="slash-menu-footer">
        <kbd>↑↓</kbd> ${t("chat.slashNavigate")} <kbd>Tab</kbd> ${t("chat.slashFill")}
        <kbd>Enter</kbd> ${t("chat.slashSelect")} <kbd>Esc</kbd> ${t("chat.slashClose")}
      </div>
    </div>
  `;
}

export function renderChat(props: ChatProps) {
  const canCompose = props.connected && !props.disabledReason;
  const isBusy = props.sending || props.stream !== null;
  const canAbort = Boolean(props.canAbort && props.onAbort);
  const activeSession = props.sessions?.sessions?.find((row) => row.key === props.sessionKey);
  const reasoningLevel = activeSession?.reasoningLevel ?? "off";
  const showReasoning = props.showThinking && reasoningLevel !== "off";
  const assistantIdentity = {
    name: props.assistantName,
    avatar:
      resolveAgentAvatarUrl({
        identity: {
          avatar: props.assistantAvatar ?? undefined,
          avatarUrl: props.assistantAvatarUrl ?? undefined,
        },
      }) ?? null,
  };
  const pinned = getPinnedMessages(props.sessionKey);
  const deleted = getDeletedMessages(props.sessionKey);
  const inputHistory = getInputHistory(props.sessionKey);
  const hasAttachments = (props.attachments?.length ?? 0) > 0;
  const tokens = tokenEstimate(props.draft);

  const placeholder = props.connected
    ? hasAttachments
      ? t("chat.placeholderWithAttachments")
      : t("chat.placeholderMessageAgent", {
          assistant: props.assistantName || t("chat.assistant"),
        })
    : t("chat.placeholderDisconnected");

  const requestUpdate = props.onRequestUpdate ?? (() => {});
  const getDraft = props.getDraft ?? (() => props.draft);

  const splitRatio = props.splitRatio ?? 0.6;
  const sidebarOpen = Boolean(props.sidebarOpen && props.onCloseSidebar);
  const workbenchCollapsed = Boolean(props.workbenchCollapsed);

  const handleCodeBlockCopy = (e: Event) => {
    const btn = (e.target as HTMLElement).closest(".code-block-copy");
    if (!btn) {
      return;
    }
    const code = (btn as HTMLElement).dataset.code ?? "";
    navigator.clipboard.writeText(code).then(
      () => {
        btn.classList.add("copied");
        setTimeout(() => btn.classList.remove("copied"), 1500);
      },
      () => {},
    );
  };

  const chatItems = buildChatItems(props);
  const isEmpty = chatItems.length === 0 && !props.loading;

  const thread = html`
    <div
      class="chat-thread"
      role="log"
      aria-live="polite"
      @scroll=${props.onChatScroll}
      @click=${handleCodeBlockCopy}
    >
      <div class="chat-thread-inner">
        ${props.loading
          ? html`
              <div class="chat-loading-skeleton" aria-label=${t("chat.loadingChat")}>
                <div class="chat-line assistant">
                  <div class="chat-msg">
                    <div class="chat-bubble">
                      <div
                        class="skeleton skeleton-line skeleton-line--long"
                        style="margin-bottom: 8px"
                      ></div>
                      <div
                        class="skeleton skeleton-line skeleton-line--medium"
                        style="margin-bottom: 8px"
                      ></div>
                      <div class="skeleton skeleton-line skeleton-line--short"></div>
                    </div>
                  </div>
                </div>
                <div class="chat-line user" style="margin-top: 12px">
                  <div class="chat-msg">
                    <div class="chat-bubble">
                      <div class="skeleton skeleton-line skeleton-line--medium"></div>
                    </div>
                  </div>
                </div>
                <div class="chat-line assistant" style="margin-top: 12px">
                  <div class="chat-msg">
                    <div class="chat-bubble">
                      <div
                        class="skeleton skeleton-line skeleton-line--long"
                        style="margin-bottom: 8px"
                      ></div>
                      <div class="skeleton skeleton-line skeleton-line--short"></div>
                    </div>
                  </div>
                </div>
              </div>
            `
          : nothing}
        ${isEmpty && !vs.searchOpen ? renderWelcomeState(props) : nothing}
        ${isEmpty && vs.searchOpen
          ? html` <div class="agent-chat__empty">${t("chat.noMatchingMessages")}</div> `
          : nothing}
        ${repeat(
          chatItems,
          (item) => item.key,
          (item) => {
            if (item.kind === "divider") {
              return html`
                <div class="chat-divider" role="separator" data-ts=${String(item.timestamp)}>
                  <span class="chat-divider__line"></span>
                  <span class="chat-divider__label">${item.label}</span>
                  <span class="chat-divider__line"></span>
                </div>
              `;
            }
            if (item.kind === "reading-indicator") {
              return renderReadingIndicatorGroup(assistantIdentity, props.basePath);
            }
            if (item.kind === "stream") {
              return renderStreamingGroup(
                item.text,
                item.startedAt,
                props.onOpenSidebar,
                assistantIdentity,
                props.basePath,
              );
            }
            if (item.kind === "group") {
              if (deleted.has(item.key)) {
                return nothing;
              }
              return renderMessageGroup(item, {
                onOpenSidebar: props.onOpenSidebar,
                showReasoning,
                showToolCalls: props.showToolCalls,
                assistantName: props.assistantName,
                assistantAvatar: assistantIdentity.avatar,
                basePath: props.basePath,
                contextWindow:
                  activeSession?.contextTokens ?? props.sessions?.defaults?.contextTokens ?? null,
                onDelete: () => {
                  deleted.delete(item.key);
                  requestUpdate();
                },
              });
            }
            return nothing;
          },
        )}
      </div>
    </div>
  `;

  const handleKeyDown = (e: KeyboardEvent) => {
    // Slash menu navigation — arg mode
    if (vs.slashMenuOpen && vs.slashMenuMode === "args" && vs.slashMenuArgItems.length > 0) {
      const len = vs.slashMenuArgItems.length;
      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          vs.slashMenuIndex = (vs.slashMenuIndex + 1) % len;
          requestUpdate();
          return;
        case "ArrowUp":
          e.preventDefault();
          vs.slashMenuIndex = (vs.slashMenuIndex - 1 + len) % len;
          requestUpdate();
          return;
        case "Tab":
          e.preventDefault();
          selectSlashArg(vs.slashMenuArgItems[vs.slashMenuIndex], props, requestUpdate, false);
          return;
        case "Enter":
          e.preventDefault();
          selectSlashArg(vs.slashMenuArgItems[vs.slashMenuIndex], props, requestUpdate, true);
          return;
        case "Escape":
          e.preventDefault();
          vs.slashMenuOpen = false;
          resetSlashMenuState();
          requestUpdate();
          return;
      }
    }

    // Slash menu navigation — command mode
    if (vs.slashMenuOpen && vs.slashMenuItems.length > 0) {
      const len = vs.slashMenuItems.length;
      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          vs.slashMenuIndex = (vs.slashMenuIndex + 1) % len;
          requestUpdate();
          return;
        case "ArrowUp":
          e.preventDefault();
          vs.slashMenuIndex = (vs.slashMenuIndex - 1 + len) % len;
          requestUpdate();
          return;
        case "Tab":
          e.preventDefault();
          tabCompleteSlashCommand(vs.slashMenuItems[vs.slashMenuIndex], props, requestUpdate);
          return;
        case "Enter":
          e.preventDefault();
          selectSlashCommand(vs.slashMenuItems[vs.slashMenuIndex], props, requestUpdate);
          return;
        case "Escape":
          e.preventDefault();
          vs.slashMenuOpen = false;
          resetSlashMenuState();
          requestUpdate();
          return;
      }
    }

    // Input history (only when input is empty)
    if (!props.draft.trim()) {
      if (e.key === "ArrowUp") {
        const prev = inputHistory.up();
        if (prev !== null) {
          e.preventDefault();
          props.onDraftChange(prev);
        }
        return;
      }
      if (e.key === "ArrowDown") {
        const next = inputHistory.down();
        e.preventDefault();
        props.onDraftChange(next ?? "");
        return;
      }
    }

    // Cmd+F for search
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && e.key === "f") {
      e.preventDefault();
      vs.searchOpen = !vs.searchOpen;
      if (!vs.searchOpen) {
        vs.searchQuery = "";
      }
      requestUpdate();
      return;
    }

    // Send on Enter (without shift)
    if (e.key === "Enter" && !e.shiftKey) {
      if (e.isComposing || e.keyCode === 229) {
        return;
      }
      if (!props.connected) {
        return;
      }
      e.preventDefault();
      if (canCompose) {
        if (props.draft.trim()) {
          inputHistory.push(props.draft);
        }
        props.onSend();
      }
    }
  };

  const handleInput = (e: Event) => {
    const target = e.target as HTMLTextAreaElement;
    adjustTextareaHeight(target);
    updateSlashMenu(target.value, requestUpdate);
    inputHistory.reset();
    props.onDraftChange(target.value);
  };

  return html`
    <section
      class="card chat"
      @drop=${(e: DragEvent) => handleDrop(e, props)}
      @dragover=${(e: DragEvent) => e.preventDefault()}
    >
      ${props.disabledReason ? html`<div class="callout">${props.disabledReason}</div>` : nothing}
      ${props.error ? html`<div class="callout danger">${props.error}</div>` : nothing}
      ${props.focusMode
        ? html`
            <button
              class="chat-focus-exit"
              type="button"
              @click=${props.onToggleFocusMode}
              aria-label=${t("chat.exitFocusMode")}
              title=${t("chat.exitFocusMode")}
            >
              ${icons.x}
            </button>
          `
        : nothing}
      ${renderSearchBar(requestUpdate)} ${renderPinnedSection(props, pinned, requestUpdate)}

      <div
        class=${[
          "chat-stage",
          sidebarOpen ? "chat-stage--sidebar-open" : "",
          workbenchCollapsed ? "chat-stage--workbench-collapsed" : "",
          sidebarOpen && workbenchCollapsed ? "chat-stage--dual-rail-compact" : "",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        <div class="chat-stage__main">
          <div class="chat-split-container ${sidebarOpen ? "chat-split-container--open" : ""}">
            <div
              class="chat-main"
              style="flex: ${sidebarOpen ? `0 0 ${splitRatio * 100}%` : "1 1 100%"}"
            >
              ${thread}
            </div>

            ${sidebarOpen
              ? html`
                  <resizable-divider
                    .splitRatio=${splitRatio}
                    @resize=${(e: CustomEvent) => props.onSplitRatioChange?.(e.detail.splitRatio)}
                  ></resizable-divider>
                  <div class="chat-sidebar">
                    ${renderMarkdownSidebar({
                      content: props.sidebarContent ?? null,
                      error: props.sidebarError ?? null,
                      onClose: props.onCloseSidebar!,
                      onViewRawText: () => {
                        if (!props.sidebarContent || !props.onOpenSidebar) {
                          return;
                        }
                        props.onOpenSidebar(`\`\`\`\n${props.sidebarContent}\n\`\`\``);
                      },
                    })}
                  </div>
                `
              : nothing}
          </div>

          ${renderFallbackIndicator(props.fallbackStatus)}
          ${renderCompactionIndicator(props.compactionStatus)}
          ${renderContextNotice(activeSession, props.sessions?.defaults?.contextTokens ?? null)}
          ${props.showNewMessages
            ? html`
                <button class="chat-new-messages" type="button" @click=${props.onScrollToBottom}>
                  ${icons.arrowDown} ${t("chat.newMessages")}
                </button>
              `
            : nothing}

          <div class="agent-chat__input">
            ${renderSlashMenu(requestUpdate, props)} ${renderAttachmentPreview(props)}
            ${renderQueuePanel(props)}

            <input
              type="file"
              accept=${CHAT_ATTACHMENT_ACCEPT}
              multiple
              class="agent-chat__file-input"
              @change=${(e: Event) => handleFileSelect(e, props)}
            />
            ${vs.uploadNotice
              ? html`<div class="callout ${vs.uploadNotice.kind}">${vs.uploadNotice.message}</div>`
              : nothing}
            ${props.workspaceFilesError
              ? html`<div class="callout danger">${props.workspaceFilesError}</div>`
              : nothing}
            ${props.workspaceFilesLoading
              ? html`<div class="agent-chat__upload-status">
                  ${icons.loader} ${t("chat.uploadingWorkspaceFiles")}
                </div>`
              : nothing}
            ${vs.sttRecording && vs.sttInterimText
              ? html`<div class="agent-chat__stt-interim">${vs.sttInterimText}</div>`
              : nothing}

            <textarea
              ${ref((el) => el && adjustTextareaHeight(el as HTMLTextAreaElement))}
              .value=${props.draft}
              dir=${detectTextDirection(props.draft)}
              ?disabled=${!canCompose}
              @keydown=${handleKeyDown}
              @input=${handleInput}
              @paste=${(e: ClipboardEvent) => handlePaste(e, props)}
              placeholder=${vs.sttRecording ? t("chat.listening") : placeholder}
              rows="1"
            ></textarea>

            <div class="agent-chat__toolbar">
              <div class="agent-chat__toolbar-left">
                <button
                  class="agent-chat__input-btn"
                  @click=${() => {
                    document.querySelector<HTMLInputElement>(".agent-chat__file-input")?.click();
                  }}
                  title=${t("chat.attachFile")}
                  aria-label=${t("chat.attachFile")}
                  ?disabled=${!canCompose}
                >
                  ${icons.paperclip}
                </button>

                ${isSttSupported()
                  ? html`
                      <button
                        class="agent-chat__input-btn ${vs.sttRecording
                          ? "agent-chat__input-btn--recording"
                          : ""}"
                        @click=${() => {
                          if (vs.sttRecording) {
                            stopStt();
                            vs.sttRecording = false;
                            vs.sttInterimText = "";
                            requestUpdate();
                          } else {
                            const started = startStt({
                              onTranscript: (text, isFinal) => {
                                if (isFinal) {
                                  const current = getDraft();
                                  const sep = current && !current.endsWith(" ") ? " " : "";
                                  props.onDraftChange(current + sep + text);
                                  vs.sttInterimText = "";
                                } else {
                                  vs.sttInterimText = text;
                                }
                                requestUpdate();
                              },
                              onStart: () => {
                                vs.sttRecording = true;
                                requestUpdate();
                              },
                              onEnd: () => {
                                vs.sttRecording = false;
                                vs.sttInterimText = "";
                                requestUpdate();
                              },
                              onError: () => {
                                vs.sttRecording = false;
                                vs.sttInterimText = "";
                                requestUpdate();
                              },
                            });
                            if (started) {
                              vs.sttRecording = true;
                              requestUpdate();
                            }
                          }
                        }}
                        title=${vs.sttRecording ? t("chat.stopRecording") : t("chat.voiceInput")}
                        ?disabled=${!canCompose}
                      >
                        ${vs.sttRecording ? icons.micOff : icons.mic}
                      </button>
                    `
                  : nothing}
                ${tokens ? html`<span class="agent-chat__token-count">${tokens}</span>` : nothing}
              </div>

              <div class="agent-chat__toolbar-right">
                ${nothing /* search hidden for now */}
                ${canAbort
                  ? nothing
                  : html`
                      <button
                        class="btn btn--ghost"
                        @click=${props.onNewSession}
                        title=${t("chat.newSession")}
                        aria-label=${t("chat.newSession")}
                      >
                        ${icons.plus}
                      </button>
                    `}
                <button
                  class="btn btn--ghost"
                  @click=${() => exportMarkdown(props)}
                  title=${t("chat.exportChat")}
                  aria-label=${t("chat.exportChat")}
                  ?disabled=${props.messages.length === 0}
                >
                  ${icons.download}
                </button>

                ${canAbort && (isBusy || props.sending)
                  ? html`
                      <button
                        class="chat-send-btn chat-send-btn--stop"
                        @click=${props.onAbort}
                        title=${t("chat.stopGenerating")}
                        aria-label=${t("chat.stopGenerating")}
                      >
                        ${icons.stop}
                      </button>
                    `
                  : nothing}
                <button
                  class="chat-send-btn"
                  @click=${() => {
                    if (props.draft.trim()) {
                      inputHistory.push(props.draft);
                    }
                    props.onSend();
                  }}
                  ?disabled=${!canCompose}
                  title=${isBusy ? t("chat.queueMessage") : t("chat.sendMessage")}
                  aria-label=${isBusy ? t("chat.queueMessage") : t("chat.sendMessage")}
                >
                  ${icons.send}
                </button>
              </div>
            </div>
          </div>
        </div>

        ${renderWorkbenchRail(props)}
      </div>
    </section>
  `;
}

const CHAT_HISTORY_RENDER_LIMIT = 200;

function groupMessages(items: ChatItem[]): Array<ChatItem | MessageGroup> {
  const result: Array<ChatItem | MessageGroup> = [];
  let currentGroup: MessageGroup | null = null;

  for (const item of items) {
    if (item.kind !== "message") {
      if (currentGroup) {
        result.push(currentGroup);
        currentGroup = null;
      }
      result.push(item);
      continue;
    }

    const normalized = normalizeMessage(item.message);
    const role = normalizeRoleForGrouping(normalized.role);
    const senderLabel = role.toLowerCase() === "user" ? (normalized.senderLabel ?? null) : null;
    const timestamp = normalized.timestamp || Date.now();

    if (
      !currentGroup ||
      currentGroup.role !== role ||
      (role.toLowerCase() === "user" && currentGroup.senderLabel !== senderLabel)
    ) {
      if (currentGroup) {
        result.push(currentGroup);
      }
      currentGroup = {
        kind: "group",
        key: `group:${role}:${item.key}`,
        role,
        senderLabel,
        messages: [{ message: item.message, key: item.key }],
        timestamp,
        isStreaming: false,
      };
    } else {
      currentGroup.messages.push({ message: item.message, key: item.key });
    }
  }

  if (currentGroup) {
    result.push(currentGroup);
  }
  return result;
}

function buildChatItems(props: ChatProps): Array<ChatItem | MessageGroup> {
  const items: ChatItem[] = [];
  const history = Array.isArray(props.messages) ? props.messages : [];
  const tools = Array.isArray(props.toolMessages) ? props.toolMessages : [];
  const historyStart = Math.max(0, history.length - CHAT_HISTORY_RENDER_LIMIT);
  if (historyStart > 0) {
    items.push({
      kind: "message",
      key: "chat:history:notice",
      message: {
        role: "system",
        content: t("chat.recentMessagesNotice", {
          limit: String(CHAT_HISTORY_RENDER_LIMIT),
          hidden: String(historyStart),
        }),
        timestamp: Date.now(),
      },
    });
  }
  for (let i = historyStart; i < history.length; i++) {
    const msg = history[i];
    const normalized = normalizeMessage(msg);
    const raw = msg as Record<string, unknown>;
    const marker = raw.__ironagent as Record<string, unknown> | undefined;
    if (marker && marker.kind === "compaction") {
      items.push({
        kind: "divider",
        key:
          typeof marker.id === "string"
            ? `divider:compaction:${marker.id}`
            : `divider:compaction:${normalized.timestamp}:${i}`,
        label: t("chat.compactionDivider"),
        timestamp: normalized.timestamp ?? Date.now(),
      });
      continue;
    }

    if (!props.showToolCalls && normalized.role.toLowerCase() === "toolresult") {
      continue;
    }

    // Apply search filter if active
    if (vs.searchOpen && vs.searchQuery.trim() && !messageMatchesSearchQuery(msg, vs.searchQuery)) {
      continue;
    }

    items.push({
      kind: "message",
      key: messageKey(msg, i),
      message: msg,
    });
  }
  // Interleave stream segments and tool cards in order. Each segment
  // contains text that was streaming before the corresponding tool started.
  // This ensures correct visual ordering: text → tool → text → tool → ...
  const segments = props.streamSegments ?? [];
  const maxLen = Math.max(segments.length, tools.length);
  for (let i = 0; i < maxLen; i++) {
    if (i < segments.length && segments[i].text.trim().length > 0) {
      items.push({
        kind: "stream" as const,
        key: `stream-seg:${props.sessionKey}:${i}`,
        text: segments[i].text,
        startedAt: segments[i].ts,
      });
    }
    if (i < tools.length && props.showToolCalls) {
      items.push({
        kind: "message",
        key: messageKey(tools[i], i + history.length),
        message: tools[i],
      });
    }
  }

  if (props.stream !== null) {
    const key = `stream:${props.sessionKey}:${props.streamStartedAt ?? "live"}`;
    if (props.stream.trim().length > 0) {
      items.push({
        kind: "stream",
        key,
        text: props.stream,
        startedAt: props.streamStartedAt ?? Date.now(),
      });
    } else {
      items.push({ kind: "reading-indicator", key });
    }
  }

  return groupMessages(items);
}

function messageKey(message: unknown, index: number): string {
  const m = message as Record<string, unknown>;
  const toolCallId = typeof m.toolCallId === "string" ? m.toolCallId : "";
  if (toolCallId) {
    return `tool:${toolCallId}`;
  }
  const id = typeof m.id === "string" ? m.id : "";
  if (id) {
    return `msg:${id}`;
  }
  const messageId = typeof m.messageId === "string" ? m.messageId : "";
  if (messageId) {
    return `msg:${messageId}`;
  }
  const timestamp = typeof m.timestamp === "number" ? m.timestamp : null;
  const role = typeof m.role === "string" ? m.role : "unknown";
  if (timestamp != null) {
    return `msg:${role}:${timestamp}:${index}`;
  }
  return `msg:${role}:${index}`;
}
