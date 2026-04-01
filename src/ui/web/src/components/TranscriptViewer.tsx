import { useMemo } from "react";

interface TranscriptEntry {
  startTime: string;
  text: string;
}

function parseSrt(srt: string): TranscriptEntry[] {
  const entries: TranscriptEntry[] = [];
  const blocks = srt.trim().split(/\n\s*\n/);

  for (const block of blocks) {
    const lines = block.trim().split("\n");
    if (lines.length < 3) continue;

    // Line 0: sequence number
    // Line 1: timestamp line "00:00:05,000 --> 00:00:10,000"
    const tsMatch = lines[1]?.match(
      /(\d{2}:\d{2}:\d{2})[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}/,
    );
    if (!tsMatch) continue;

    const startTime = tsMatch[1];
    const text = lines.slice(2).join(" ").trim();
    if (text) {
      entries.push({ startTime, text });
    }
  }

  return entries;
}

export default function TranscriptViewer({ srt }: { srt: string }) {
  const entries = useMemo(() => parseSrt(srt), [srt]);

  if (entries.length === 0) {
    return <p className="text-sm italic text-gray-500">Could not parse transcript.</p>;
  }

  return (
    <div className="max-h-60 overflow-auto rounded border border-gray-700 bg-gray-950 p-2 text-sm">
      {entries.map((entry, i) => (
        <div key={i} className="flex gap-3 py-0.5">
          <span className="shrink-0 font-mono text-xs text-gray-500">
            {entry.startTime}
          </span>
          <span className="text-gray-300">{entry.text}</span>
        </div>
      ))}
    </div>
  );
}
