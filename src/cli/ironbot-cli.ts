import type { Command } from "commander";
import { formatDocsLink } from "../terminal/links.js";
import { theme } from "../terminal/theme.js";
import { registerQrCli } from "./qr-cli.js";

export function registerIronbotCli(program: Command) {
  const ironbot = program
    .command("ironbot")
    .description("Legacy ironbot command aliases")
    .addHelpText(
      "after",
      () =>
        `\n${theme.muted("Docs:")} ${formatDocsLink("/cli/ironbot", "docs.ironagent.ai/cli/ironbot")}\n`,
    );
  registerQrCli(ironbot);
}
