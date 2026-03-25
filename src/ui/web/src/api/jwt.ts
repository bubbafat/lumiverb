import { API_KEY_STORAGE_KEY } from "./client";

export interface JwtClaims {
  sub: string;
  role: string;
  tenant_id: string;
}

/** Decode base64url (JWT standard) to a regular string. */
function base64urlDecode(str: string): string {
  // Replace base64url chars with standard base64 and pad
  const base64 = str.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  return atob(padded);
}

export function decodeJwtClaims(): JwtClaims | null {
  try {
    const token = localStorage.getItem(API_KEY_STORAGE_KEY);
    if (!token) return null;
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    const payload = JSON.parse(base64urlDecode(parts[1])) as Record<string, unknown>;
    if (
      typeof payload.sub !== "string" ||
      typeof payload.role !== "string" ||
      typeof payload.tenant_id !== "string"
    ) {
      return null;
    }
    return {
      sub: payload.sub,
      role: payload.role,
      tenant_id: payload.tenant_id,
    };
  } catch {
    return null;
  }
}
