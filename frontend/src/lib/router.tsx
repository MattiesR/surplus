import { type AnchorHTMLAttributes, type MouseEvent, useEffect, useSyncExternalStore } from "react";

/** A tiny history-API router: the app has a handful of screens, so a routing library is not needed. */

const listeners = new Set<() => void>();
const subscribe = (fn: () => void) => {
  listeners.add(fn);
  return () => listeners.delete(fn);
};
window.addEventListener("popstate", () => listeners.forEach((l) => l()));

export function navigate(to: string, replace = false): void {
  if (to === location.pathname + location.search) return;
  if (replace) history.replaceState(null, "", to);
  else history.pushState(null, "", to);
  listeners.forEach((l) => l());
  window.scrollTo(0, 0);
}

export function usePath(): string {
  return useSyncExternalStore(subscribe, () => location.pathname);
}

/** Match "/projects/:id/:tab" style patterns. Returns params or null. */
export function match(pattern: string, path: string): Record<string, string> | null {
  const p = pattern.split("/").filter(Boolean);
  const s = path.split("/").filter(Boolean);
  const params: Record<string, string> = {};
  for (let i = 0; i < p.length; i++) {
    const part = p[i]!;
    const optional = part.endsWith("?");
    const name = part.replace(/^:/, "").replace(/\?$/, "");
    const seg = s[i];
    if (part.startsWith(":")) {
      if (seg === undefined) {
        if (optional) continue;
        return null;
      }
      params[name] = decodeURIComponent(seg);
    } else if (seg !== part) {
      return null;
    }
  }
  return s.length <= p.length ? params : null;
}

export function Link({ href, onClick, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) {
  const handle = (e: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(e);
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    navigate(href);
  };
  return <a href={href} onClick={handle} {...rest} />;
}

/** Two-key shortcuts like "g p" (go to projects). Ignored while typing in a field. */
export function useShortcuts(map: Record<string, () => void>): void {
  useEffect(() => {
    let prefix = "";
    let timer: number | undefined;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (t && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName))) return;
      const combo = prefix ? `${prefix} ${e.key}` : e.key;
      if (map[combo]) {
        e.preventDefault();
        prefix = "";
        map[combo]!();
        return;
      }
      if (Object.keys(map).some((k) => k.startsWith(`${e.key} `))) {
        prefix = e.key;
        window.clearTimeout(timer);
        timer = window.setTimeout(() => (prefix = ""), 1000);
      } else {
        prefix = "";
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [map]);
}
