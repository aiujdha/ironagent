// Phase 10.2: @aws-sdk/client-s3 is not bundled in this internal-fork build.
// The S3 backend was never wired into the IronAgent deployment surface; keep
// the stub here so callers continue to typecheck and a clear runtime error
// surfaces if anyone ever opts into the S3 storage path.

export type WorkspaceObjectStoreConfig = {
  endpoint: string;
  region: string;
  bucket: string;
  accessKeyId: string;
  secretAccessKey: string;
  forcePathStyle: boolean;
};

export type WorkspaceObjectStore = {
  putObject(params: {
    key: string;
    body: Uint8Array;
    contentType: string;
  }): Promise<{ etag?: string }>;
  getObject(params: { key: string }): Promise<Buffer | null>;
  deleteObject(params: { key: string }): Promise<void>;
};

const S3_NOT_AVAILABLE_MESSAGE =
  "S3 workspace storage backend is not bundled in this build. " +
  "Install @aws-sdk/client-s3 and restore createWorkspaceS3ObjectStore to use this code path.";

export function createWorkspaceS3ObjectStore(
  _config: WorkspaceObjectStoreConfig,
): WorkspaceObjectStore {
  throw new Error(S3_NOT_AVAILABLE_MESSAGE);
}
