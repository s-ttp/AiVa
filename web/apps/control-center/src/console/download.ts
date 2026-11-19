import { ApiError, toApiError, type GatewayClient } from '../api/client';

/** How long the object URL outlives the click that starts the download. */
const REVOKE_AFTER_MS = 30_000;

/**
 * Fetches the document with the session's bearer token and hands it to the
 * browser as a download; a plain link could not carry the token.
 */
export async function downloadArtifact(client: GatewayClient, artifactId: string, fileName: string): Promise<true> {
  let result;
  try {
    result = await client.GET('/v1/artifacts/{artifact_id}/content', {
      params: { path: { artifact_id: artifactId } },
      parseAs: 'blob',
    });
  } catch {
    throw new ApiError({ kind: 'network', message: 'The AiVa gateway could not be reached.' });
  }
  if (!result.response.ok || !result.data) throw toApiError(result.response.status, result.error, undefined, result.response.headers);
  const url = URL.createObjectURL(result.data as Blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  link.click();
  // Revoking at once can cancel the download in some browsers (Firefox, Safari on large files).
  setTimeout(() => URL.revokeObjectURL(url), REVOKE_AFTER_MS);
  return true;
}
