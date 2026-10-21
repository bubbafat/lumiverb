/** Where an AI machine can be, for Settings › AI's Add: each fills in the URL and a name, says whether it needs a
 * key and where one comes from, and names a model that works for descriptions & text, as each provider has a catch
 * of its own (the demo server, 10-21: OpenAI's URL, Gemini's `models/…` names and thinking models, Anthropic's
 * models list). Custom URL is any other OpenAI-compatible server. */
export interface AiPreset {
  id: string;
  label: string;
  /** Filled in as the machine's name when none is typed yet. */
  name: string;
  url: string;
  /** Where its key comes from; none: it needs none. */
  keyFrom?: string;
  /** A model that works for descriptions & text, as it lists it. */
  model?: string;
  /** Anything else to know. */
  note?: string;
}

export const CUSTOM = "custom";

export const AI_PRESETS: AiPreset[] = [
  { id: CUSTOM, label: "Custom URL", name: "", url: "" },
  { id: "ollama", label: "Ollama on this computer", name: "Ollama", url: "http://localhost:11434/v1",
    model: "qwen3-vl:8b-instruct" },
  { id: "ollama-lan", label: "Ollama on another computer", name: "Ollama", url: "http://192.168.1.20:11434/v1",
    model: "qwen3-vl:8b-instruct",
    note: "Put that computer's address in place of 192.168.1.20; Ollama there must listen on the network." },
  { id: "openai", label: "OpenAI", name: "OpenAI", url: "https://api.openai.com/v1",
    keyFrom: "platform.openai.com › API keys", model: "gpt-4.1-mini" },
  { id: "anthropic", label: "Anthropic (Claude)", name: "Claude", url: "https://api.anthropic.com/v1",
    keyFrom: "console.anthropic.com › API keys", model: "claude-haiku-4-5-20251001" },
  { id: "gemini", label: "Google Gemini", name: "Gemini", url: "https://generativelanguage.googleapis.com/v1beta/openai",
    keyFrom: "aistudio.google.com › API keys",
    note: "Its models are listed as models/…. Pick one that doesn't think before it answers (a lite one): thinking "
      + "can use up the room for the answer." },
];

export function presetOf(id: string): AiPreset {
  return AI_PRESETS.find((p) => p.id === id) ?? AI_PRESETS[0];
}
