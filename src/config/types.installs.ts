export type InstallRecordBase = {
  source: "npm" | "archive" | "path" | "ironhub";
  spec?: string;
  sourcePath?: string;
  installPath?: string;
  version?: string;
  resolvedName?: string;
  resolvedVersion?: string;
  resolvedSpec?: string;
  integrity?: string;
  shasum?: string;
  resolvedAt?: string;
  installedAt?: string;
  ironhubUrl?: string;
  ironhubPackage?: string;
  ironhubFamily?: "code-plugin" | "bundle-plugin";
  ironhubChannel?: "official" | "community" | "private";
};
