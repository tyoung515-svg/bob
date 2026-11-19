import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

// The IPC surface of the shell's Rust side (lib.rs). Every line is a
// verbatim pass-through of the engine's NDJSON binding (STALE-9: the
// Tauri IPC and NDJSON bindings carry byte-equivalent payloads).

export async function sliceStart(dataRoot?: string): Promise<string> {
  return invoke<string>("slice_start", { dataRoot: dataRoot ?? null });
}

export async function sliceSend(line: string): Promise<void> {
  return invoke<void>("slice_send", { line });
}

export async function sliceShutdown(): Promise<void> {
  return invoke<void>("slice_shutdown");
}

export function onLine(cb: (line: string) => void): Promise<UnlistenFn> {
  return listen<string>("slice://line", (e) => cb(e.payload));
}

export function onStderr(cb: (line: string) => void): Promise<UnlistenFn> {
  return listen<string>("slice://stderr", (e) => cb(e.payload));
}

export function onExit(cb: (code: number | null) => void): Promise<UnlistenFn> {
  return listen<number | null>("slice://exit", (e) => cb(e.payload));
}
