"use client";

import { useRef, useState } from "react";

import type { StoredResume } from "@/lib/labels";

/**
 * Upload, replace and show the stored resume.
 *
 * IT DOES NOT OWN THE KEYWORDS IT PRODUCES. A successful upload hands its
 * suggestions up to PreferencesEditor via onKeywords and forgets them; the chips
 * live next door and are saved by the same Save button as the rest of the
 * filter. That split is the reason the screen has one save rather than two: the
 * FILE is stored the moment it uploads (it has to be — it is the thing being
 * stored), and the KEYWORDS are a proposal that is not written to
 * preferences.keywords until a person agrees to them.
 *
 * DRAG-AND-DROP AND CLICK ARE THE SAME PATH. The drop zone is a <label> wrapping
 * a real file input, so the click half needs no JavaScript at all and the
 * keyboard reaches it because it is a genuine form control. Only the drag half
 * is scripted, which is the right way round — a person on a phone gets the half
 * that works there.
 */
export function ResumePanel({
  initial,
  onKeywords,
}: {
  initial: StoredResume | null;
  onKeywords: (keywords: string[]) => void;
}) {
  const [resume, setResume] = useState<StoredResume | null>(initial);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "bad"; text: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  async function upload(file: File) {
    setBusy(true);
    setMessage(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch("/api/resume", { method: "POST", body: form });
      const data = await res.json().catch(() => ({}));

      if (!res.ok) {
        // Every refusal from that route carries a sentence written for the
        // person who chose the file. It is shown as-is rather than replaced with
        // a generic failure, which is the entire reason those messages exist.
        throw new Error(data.detail || data.error || `Upload failed (HTTP ${res.status})`);
      }

      setResume(data.resume);
      onKeywords(data.keywords ?? []);
      setMessage({ kind: "ok", text: data.message });
    } catch (err) {
      setMessage({ kind: "bad", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
      // So that choosing the SAME file again still fires a change event, which
      // it otherwise would not — and re-trying the same file after a transient
      // failure is the most likely next action.
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  function onDrop(event: React.DragEvent) {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file) void upload(file);
  }

  return (
    <div className="panel">
      <h2>Resume</h2>
      <p className="hint">
        We read it once to suggest the skills below, then store it in your profile. It is
        private to you, never shared or sold, and it is deleted when you delete your account.
        PDF or Word (.docx), up to 5&nbsp;MB.
      </p>

      {resume ? (
        <div className="resume-current">
          <div className="stack">
            <strong>{resume.filename}</strong>
            <span className="muted small">
              Uploaded {new Date(resume.uploaded_at).toLocaleDateString(undefined, {
                day: "numeric",
                month: "long",
                year: "numeric",
              })}
              {" · "}
              {formatSize(resume.size_bytes)}
              {" · "}
              <a href="/api/resume/download">Open</a>
            </span>
          </div>
        </div>
      ) : null}

      <label
        className={`dropzone${dragging ? " dragging" : ""}${busy ? " busy" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          disabled={busy}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void upload(file);
          }}
        />
        <span className="dropzone-main">
          {busy
            ? "Reading your resume…"
            : resume
              ? "Drop a new file here, or choose one to replace it"
              : "Drop your resume here, or choose a file"}
        </span>
        <span className="muted small">
          {resume
            ? "Replacing overwrites the stored file and suggests skills again."
            : "PDF or .docx — nothing is stored until it can be read."}
        </span>
      </label>

      {message ? <p className={`msg ${message.kind}`}>{message.text}</p> : null}
    </div>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
