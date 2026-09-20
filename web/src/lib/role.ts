"use client";

import { useSyncExternalStore } from "react";

// Simulated role switch. This is a cookie, not auth -- every screen that shows a role
// must also show the "simulated roles" tag (see components/RoleSwitch.tsx).
export type Role = "reviewer" | "approver";

export type RoleState = { role: Role; name: string };

const COOKIE = "finrecur_role";

export function readRole(): RoleState {
  if (typeof document === "undefined") return { role: "reviewer", name: "Reviewer" };
  const match = document.cookie.match(new RegExp(`${COOKIE}=([^;]+)`));
  if (!match) return { role: "reviewer", name: "Reviewer" };
  try {
    const parsed = JSON.parse(decodeURIComponent(match[1]));
    if (parsed && (parsed.role === "reviewer" || parsed.role === "approver") && typeof parsed.name === "string") {
      return parsed;
    }
  } catch {
    // fall through to default
  }
  return { role: "reviewer", name: "Reviewer" };
}

export function writeRole(state: RoleState) {
  if (typeof document === "undefined") return;
  document.cookie = `${COOKIE}=${encodeURIComponent(JSON.stringify(state))}; path=/; max-age=31536000`;
  window.dispatchEvent(new Event("finrecur-role"));
}

function subscribeRole(onChange: () => void) {
  window.addEventListener("finrecur-role", onChange);
  return () => window.removeEventListener("finrecur-role", onChange);
}

export function useRole(): RoleState {
  const value = useSyncExternalStore(subscribeRole, () => JSON.stringify(readRole()), () => '{"role":"reviewer","name":"Reviewer"}');
  return JSON.parse(value) as RoleState;
}
