export const IRONAGENT_CLI_ENV_VAR = "IRONAGENT_CLI";
export const IRONAGENT_CLI_ENV_VALUE = "1";

export function markIronAgentExecEnv<T extends Record<string, string | undefined>>(env: T): T {
  return {
    ...env,
    [IRONAGENT_CLI_ENV_VAR]: IRONAGENT_CLI_ENV_VALUE,
  };
}

export function ensureIronAgentExecMarkerOnProcess(
  env: NodeJS.ProcessEnv = process.env,
): NodeJS.ProcessEnv {
  env[IRONAGENT_CLI_ENV_VAR] = IRONAGENT_CLI_ENV_VALUE;
  return env;
}
