import { markdown } from "@codemirror/lang-markdown";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import CodeMirror from "@uiw/react-codemirror";
import { useEffect, useState, useSyncExternalStore } from "react";
import { api, ApiError, type DocName } from "../api/client";

const dark = window.matchMedia("(prefers-color-scheme: dark)");
const useDark = () =>
  useSyncExternalStore(
    (fn) => {
      dark.addEventListener("change", fn);
      return () => dark.removeEventListener("change", fn);
    },
    () => dark.matches,
  );

/**
 * Edits one .surplus/*.md file as text. Saves are guarded by the hash the edit started from: if the file changed on
 * disk meanwhile (e.g. in the user's own editor), the save is refused and the user decides what to do.
 */
export function DocEditor({
  projectId,
  doc,
  content,
  hash,
  hint,
}: {
  projectId: string;
  doc: DocName;
  content: string;
  hash: string;
  hint?: string;
}) {
  const qc = useQueryClient();
  const isDark = useDark();
  const [text, setText] = useState(content);
  const [base, setBase] = useState({ content, hash });
  const dirty = text !== base.content;
  const changedOnDisk = hash !== base.hash;

  // Adopt the on-disk version when it changes and there are no local edits to lose.
  useEffect(() => {
    if (hash !== base.hash && text === base.content) {
      setText(content);
      setBase({ content, hash });
    }
  }, [hash, content, base, text]);

  const save = useMutation({
    mutationFn: () => api.writeDoc(projectId, doc, text, base.hash),
    onSuccess: (res) => {
      setBase({ content: text, hash: res.hash });
      void qc.invalidateQueries({ queryKey: ["project", projectId] });
      void qc.invalidateQueries({ queryKey: ["projects"] });
    },
    // A conflict means the disk version is newer: fetch it so the "changed on disk" choice appears.
    onError: () => void qc.invalidateQueries({ queryKey: ["project", projectId] }),
  });

  const reload = () => {
    setText(content);
    setBase({ content, hash });
    save.reset();
  };

  const err = save.error instanceof ApiError ? save.error : null;

  return (
    <div className="space-y-2">
      {changedOnDisk && dirty && (
        <div className="card flex flex-wrap items-center gap-2 p-3 text-sm" role="alert">
          <span aria-hidden style={{ color: "var(--status-warning)" }}>
            ▲
          </span>
          {doc}.md changed on disk while you were editing.
          <button className="btn ml-auto" onClick={reload}>
            Load the disk version (discard my edits)
          </button>
        </div>
      )}
      <div
        className="overflow-hidden rounded-md border"
        style={{ borderColor: "var(--border)" }}
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === "s") {
            e.preventDefault();
            if (dirty && !save.isPending) save.mutate();
          }
        }}
      >
        <CodeMirror
          value={text}
          onChange={setText}
          extensions={[markdown()]}
          theme={isDark ? "dark" : "light"}
          minHeight="320px"
          maxHeight="70vh"
          aria-label={`${doc}.md`}
          basicSetup={{ foldGutter: false, highlightActiveLine: false }}
        />
      </div>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <button className="btn" onClick={() => save.mutate()} disabled={!dirty || save.isPending}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
        <button className="btn" onClick={reload} disabled={!dirty}>
          Revert
        </button>
        <span className="muted text-xs">
          {dirty ? "Unsaved changes · Ctrl+S to save" : `Saved to .surplus/${doc}.md`}
        </span>
        {hint && <span className="ml-auto text-xs muted">{hint}</span>}
      </div>
      {err && (
        <p className="text-sm" role="alert">
          <span aria-hidden style={{ color: "var(--status-critical)" }}>
            ▲
          </span>{" "}
          {err.isConflict
            ? "Not saved: the file changed on disk since you opened it. Load the disk version, or copy your text first."
            : `Not saved${err.line ? ` (line ${err.line})` : ""}: ${err.message}`}
        </p>
      )}
    </div>
  );
}
