import { useState } from "react";
import { ApiError, deleteTranscript, uploadTranscript } from "../api/client";

const linkClass = "text-xs text-indigo-400 hover:text-indigo-300 disabled:opacity-50";

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function Alert({ text }: { text: string | null }) {
  return text ? (
    <p role="alert" className="mt-1 text-xs text-red-400">
      {text}
    </p>
  ) : null;
}

interface UploadProps {
  assetId: string;
  label: string;
  className: string;
  onChanged: () => void;
  onError: (text: string | null) => void;
}

/** A file picker that uploads an SRT as a person's transcript. */
function UploadSrt({ assetId, label, className, onChanged, onError }: UploadProps) {
  return (
    <label className={className}>
      {label}
      <input
        type="file"
        accept=".srt"
        aria-label={label}
        className="hidden"
        onChange={async (e) => {
          const input = e.target;
          const file = input.files?.[0];
          if (!file) return;
          onError(null);
          try {
            await uploadTranscript(assetId, await file.text());
            onChanged();
          } catch (err) {
            onError(`Couldn't upload it: ${errorText(err)}`);
          } finally {
            input.value = "";
          }
        }}
      />
    </label>
  );
}

interface ActionsProps {
  assetId: string;
  /** Whose transcript is shown: "manual" (a person's) or the machine that made it. */
  source: string | null | undefined;
  /** A person's is shown with the machine's kept under it. */
  machineUnderneath: boolean;
  canEdit: boolean;
  onDownload: () => void;
  /** The clip's transcript changed: reload it. */
  onChanged: () => void;
}

/** Download, and for editors Replace and Remove, beside a transcript. */
export function TranscriptActions({ assetId, source, machineUnderneath, canEdit, onDownload, onChanged }: ActionsProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mine = source === "manual";
  // Removing a person's brings back the machine's, when it's kept under theirs.
  const remove = mine && machineUnderneath
    ? { text: "Use the machine's", label: "Remove your transcript and use the machine's" }
    : { text: "Remove", label: mine ? "Remove your transcript" : "Remove the machine's transcript" };

  return (
    <div className="flex flex-col items-end">
      <div className="flex gap-2">
        <button type="button" className={linkClass} onClick={onDownload}>
          Download
        </button>
        {canEdit && (
          <>
            <UploadSrt
              assetId={assetId}
              label="Replace"
              className="cursor-pointer text-xs text-indigo-400 hover:text-indigo-300"
              onChanged={onChanged}
              onError={setError}
            />
            <button
              type="button"
              className="text-xs text-red-400 hover:text-red-300 disabled:opacity-50"
              aria-label={remove.label}
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setError(null);
                try {
                  await deleteTranscript(assetId, mine ? "manual" : "machine");
                  onChanged();
                } catch (err) {
                  if (err instanceof ApiError && err.code === "transcript_changed") {
                    setError("The transcript changed meanwhile, so nothing was removed. Showing it now.");
                    onChanged();
                  } else {
                    setError(`Couldn't remove it: ${errorText(err)}`);
                  }
                } finally {
                  setBusy(false);
                }
              }}
            >
              {remove.text}
            </button>
          </>
        )}
      </div>
      <Alert text={error} />
    </div>
  );
}

interface EmptyProps {
  assetId: string;
  canEdit: boolean;
  onChanged: () => void;
}

/** A clip with no transcript: editors can upload one. */
export function NoTranscript({ assetId, canEdit, onChanged }: EmptyProps) {
  const [error, setError] = useState<string | null>(null);
  if (!canEdit) return <p className="text-gray-500">No transcript</p>;
  return (
    <div>
      <UploadSrt
        assetId={assetId}
        label="Upload SRT"
        className="inline-flex cursor-pointer items-center gap-1.5 rounded bg-gray-700/60 px-3 py-1.5 text-xs text-gray-300 hover:bg-indigo-600/40 hover:text-indigo-200 transition-colors"
        onChanged={onChanged}
        onError={setError}
      />
      <Alert text={error} />
    </div>
  );
}
