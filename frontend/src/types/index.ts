/** Core domain types mirroring the backend models. */

export type DocumentStatus = "pending" | "processing" | "ready" | "error";
export type UserRole = "admin" | "researcher" | "viewer";

export interface Document {
  id: string;
  filename: string;
  status: DocumentStatus;
  metadata: Record<string, unknown>;
  createdAt: string;
}

export interface AgentRunRequest {
  task: string;
  documentIds: string[];
  agentName?: string;
  sessionId?: string;
}

export interface AgentRunResponse {
  sessionId: string;
  agentName: string;
  success: boolean;
  result?: unknown;
  error?: string;
  latencyMs: number;
}

export interface SearchResult {
  chunk: { content: string; documentId: string };
  score: number;
}

export interface User {
  id: string;
  email: string;
  fullName: string;
  role: UserRole;
}
