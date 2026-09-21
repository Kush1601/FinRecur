"use client";

// A run can be started from more than one place (the header's "Run next batch"
// button, or the Run page's own button), and several pages show data derived
// from the latest run (header status rail, review queue, activity). Same
// mechanism as lib/role.ts's window event: whoever finishes a run tells
// everyone else to refetch, instead of each page only fetching once on mount.
const EVENT = "finrecur-run-updated";

export function emitRunUpdated() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(EVENT));
}

export function onRunUpdated(handler: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener(EVENT, handler);
  return () => window.removeEventListener(EVENT, handler);
}
