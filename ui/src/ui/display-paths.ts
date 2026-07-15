const DISPLAY_PREFIX_ALIASES: Array<{ raw: string; display: string }> = [
  { raw: "/home/node/.ironagent/workspace", display: "IronAgent 工作空间" },
  { raw: "~/.ironagent", display: "IronAgent 认证目录" },
];

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function replacePathSegment(value: string, segment: string, replacement: string): string {
  const pattern = new RegExp(String.raw`(^|[\\/])${escapeRegex(segment)}(?=([\\/]|$))`, "g");
  return value.replace(pattern, (_, prefix: string) => `${prefix}${replacement}`);
}

export function formatDisplayPath(value: string | null | undefined): string {
  if (!value) {
    return "";
  }

  let text = value;
  for (const alias of DISPLAY_PREFIX_ALIASES) {
    if (text === alias.raw) {
      return alias.display;
    }
    if (text.startsWith(`${alias.raw}/`) || text.startsWith(`${alias.raw}\\`)) {
      return `${alias.display}${text.slice(alias.raw.length)}`;
    }
  }

  text = text.replaceAll("ironagent.json", "IronAgent 运行配置");
  text = replacePathSegment(text, ".ironagent", "IronAgent");
  text = replacePathSegment(text, "ironagent-main", "IronAgent");
  return text;
}
