// Display names for the language codes the worker detects (Whisper ISO codes).
const NAMES: Record<string, string> = {
  en: "English",
  hi: "Hindi",
};

/** "Hindi", "English", or the upper-cased code for anything else; null when unknown. */
export function languageLabel(code?: string | null): string | null {
  if (!code) return null;
  return NAMES[code.toLowerCase()] ?? code.toUpperCase();
}
