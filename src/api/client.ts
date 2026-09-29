import type { FileConflict } from "../types";

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown) {
    const detail = typeof body === "object" && body !== null && "detail" in body ? (body as { detail?: unknown }).detail : undefined;
    super(typeof detail === "string" ? detail : `请求失败：${status}`);
    this.status = status;
    this.body = body;
  }
}

export async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options?.headers ?? {}) },
    ...options
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export function isFileConflict(value: unknown): value is ApiError & { body: FileConflict } {
  if (!(value instanceof ApiError) || value.status !== 409) return false;
  const body = value.body;
  return (
    typeof body === "object" &&
    body !== null &&
    "conflict_id" in body &&
    "source_path" in body &&
    "target_path" in body
  );
}
