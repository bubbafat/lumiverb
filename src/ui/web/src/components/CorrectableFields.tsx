import { useState, type ReactNode } from "react";

const labelClass = "text-xs font-medium uppercase tracking-wide text-gray-500";
const linkClass = "text-xs text-indigo-300 hover:text-indigo-200 disabled:opacity-50";
const buttonClass = "rounded-md px-3 py-1 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-50";

/** "Edited" beside a value a person corrected. */
function EditedBadge() {
  return (
    <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-300">
      Edited
    </span>
  );
}

/** Runs a save; a failure becomes a message, and the form stays as it was. */
function useSave() {
  const [error, setError] = useState<string | null>(null);
  const run = async (save: () => Promise<unknown> | void): Promise<boolean> => {
    setError(null);
    try {
      await save();
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    }
  };
  return { error, run, clear: () => setError(null) };
}

function SaveError({ error }: { error: string | null }) {
  return error ? (
    <p role="alert" className="mt-1 text-xs text-red-400">
      Couldn't save: {error}
    </p>
  ) : null;
}

interface TextProps {
  label: string;
  /** What's shown: the person's correction, else the machine's. */
  value: string | null | undefined;
  /** The machine's value under a correction (shown smaller, to compare). */
  machine?: string | null;
  corrected: boolean;
  canEdit: boolean;
  emptyText: string;
  saving?: boolean;
  /** A string sets the correction; null brings back the machine's. */
  onSave: (value: string | null) => Promise<unknown> | void;
  render?: (value: string) => ReactNode;
}

/** A description or the text in an image: shown, and for editors correctable. */
export function CorrectableText({ label, value, machine, corrected, canEdit, emptyText, saving, onSave, render }: TextProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const { error, run, clear } = useSave();

  return (
    <div>
      <div className="mb-1 flex items-center gap-2">
        <span className={labelClass}>{label}</span>
        {corrected && <EditedBadge />}
        {canEdit && !editing && (
          <span className="ml-auto flex gap-3">
            {corrected && (
              <button
                type="button"
                className={linkClass}
                disabled={saving}
                aria-label={`Use the AI's ${label.toLowerCase()}`}
                onClick={() => run(() => onSave(null))}
              >
                Use the AI's
              </button>
            )}
            <button
              type="button"
              className={linkClass}
              aria-label={`Edit ${label.toLowerCase()}`}
              onClick={() => {
                setDraft(value ?? "");
                setEditing(true);
              }}
            >
              Edit
            </button>
          </span>
        )}
      </div>
      {editing ? (
        <form
          className="space-y-2"
          onSubmit={async (e) => {
            e.preventDefault();
            if (await run(() => onSave(draft))) setEditing(false);
          }}
        >
          <textarea
            aria-label={label}
            value={draft}
            rows={3}
            autoFocus
            onChange={(e) => setDraft(e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-2 py-1.5 text-sm text-gray-100"
          />
          <div className="flex gap-2">
            <button type="submit" disabled={saving} className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}>
              Save
            </button>
            <button
              type="button"
              onClick={() => {
                clear();
                setEditing(false);
              }}
              className={`${buttonClass} text-gray-300 hover:bg-gray-800`}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : value ? (
        render ? render(value) : <p className="text-sm text-gray-300 whitespace-pre-wrap">{value}</p>
      ) : (
        <p className="text-gray-500">{emptyText}</p>
      )}
      {corrected && machine && !editing && (
        <p className="mt-1 text-xs text-gray-500">
          The AI's: <span className="italic">{machine}</span>
        </p>
      )}
      <SaveError error={error} />
    </div>
  );
}

interface TagsProps {
  tags: string[];
  machineTags?: string[];
  corrected: boolean;
  canEdit: boolean;
  saving?: boolean;
  /** The tags to show (kept as adds and removes on the machine's); null brings back the machine's. */
  onSave: (tags: string[] | null) => Promise<unknown> | void;
  renderTag: (tag: string) => ReactNode;
}

/** Tags: shown, and for editors correctable (remove some, add your own). */
export function CorrectableTags({ tags, machineTags, corrected, canEdit, saving, onSave, renderTag }: TagsProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<string[]>([]);
  const [adding, setAdding] = useState("");
  const { error, run, clear } = useSave();

  /** The draft with the tag being typed, if any. */
  const withTyped = () => {
    const tag = adding.trim();
    return tag && !draft.includes(tag) ? [...draft, tag] : draft;
  };
  const add = () => {
    setDraft(withTyped());
    setAdding("");
  };

  return (
    <div>
      <div className="mb-1 flex items-center gap-2">
        <span className={labelClass}>Tags</span>
        {corrected && <EditedBadge />}
        {canEdit && !editing && (
          <span className="ml-auto flex gap-3">
            {corrected && (
              <button
                type="button"
                className={linkClass}
                disabled={saving}
                aria-label="Use the AI's tags"
                onClick={() => run(() => onSave(null))}
              >
                Use the AI's
              </button>
            )}
            <button
              type="button"
              className={linkClass}
              aria-label="Edit tags"
              onClick={() => {
                setDraft(tags);
                setEditing(true);
              }}
            >
              Edit
            </button>
          </span>
        )}
      </div>
      {editing ? (
        <form
          className="space-y-2"
          onSubmit={async (e) => {
            e.preventDefault();
            const tags = withTyped();
            setDraft(tags);
            setAdding("");
            if (await run(() => onSave(tags))) setEditing(false);
          }}
        >
          <div className="flex flex-wrap gap-1.5">
            {draft.map((tag) => (
              <span key={tag} className="flex items-center gap-1 rounded-full bg-gray-700/60 py-0.5 pl-2.5 pr-1 text-xs text-gray-200">
                {tag}
                <button
                  type="button"
                  aria-label={`Remove ${tag}`}
                  onClick={() => setDraft(draft.filter((t) => t !== tag))}
                  className="rounded-full px-1 text-gray-400 hover:bg-gray-600 hover:text-gray-100"
                >
                  ×
                </button>
              </span>
            ))}
          </div>
          <input
            aria-label="Add a tag"
            placeholder="Add a tag, then Enter"
            value={adding}
            onChange={(e) => setAdding(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                add();
              }
            }}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-2 py-1.5 text-sm text-gray-100"
          />
          <div className="flex gap-2">
            <button type="submit" disabled={saving} className={`${buttonClass} bg-indigo-600 text-white hover:bg-indigo-500`}>
              Save
            </button>
            <button
              type="button"
              onClick={() => {
                clear();
                setEditing(false);
              }}
              className={`${buttonClass} text-gray-300 hover:bg-gray-800`}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : tags.length > 0 ? (
        <div className="mt-1 flex flex-wrap gap-1.5">{tags.map((tag) => renderTag(tag))}</div>
      ) : (
        <p className="text-gray-500">No tags yet</p>
      )}
      {corrected && machineTags && !editing && (
        <p className="mt-1 text-xs text-gray-500">The AI's: {machineTags.length ? machineTags.join(", ") : "none"}</p>
      )}
      <SaveError error={error} />
    </div>
  );
}
