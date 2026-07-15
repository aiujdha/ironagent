export const CHAT_ATTACHMENT_ACCEPT = "image/*,.csv,.doc,.docx,.json,.md,.pdf,.txt,.xls,.xlsx,.zip";

const CHAT_ATTACHMENT_EXTENSIONS = new Set([
  ".csv",
  ".doc",
  ".docx",
  ".json",
  ".md",
  ".pdf",
  ".txt",
  ".xls",
  ".xlsx",
  ".zip",
]);

export function isSupportedChatAttachmentMimeType(mimeType: string | null | undefined): boolean {
  if (typeof mimeType !== "string") {
    return false;
  }
  const normalized = mimeType.trim().toLowerCase();
  return normalized.startsWith("image/") || normalized === "application/pdf";
}

export function isSupportedChatAttachmentFile(file: Pick<File, "name" | "type">): boolean {
  if (isSupportedChatAttachmentMimeType(file.type)) {
    return true;
  }
  const normalizedName = file.name.trim().toLowerCase();
  const dotIndex = normalizedName.lastIndexOf(".");
  if (dotIndex < 0) {
    return false;
  }
  return CHAT_ATTACHMENT_EXTENSIONS.has(normalizedName.slice(dotIndex));
}

export function revokeChatAttachmentPreview(attachment: { previewUrl?: string | null }): void {
  if (attachment.previewUrl) {
    URL.revokeObjectURL(attachment.previewUrl);
  }
}

export function revokeChatAttachmentPreviews(
  attachments: ReadonlyArray<{
    previewUrl?: string | null;
  }>,
): void {
  for (const attachment of attachments) {
    revokeChatAttachmentPreview(attachment);
  }
}
