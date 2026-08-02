const URL_PATTERN = /https?:\/\/[^\s<>"']+/i;

export function firstUrl(value?: string | null): string | null {
  if (!value) return null;
  const match = value.match(URL_PATTERN);
  if (!match) return null;
  return match[0].replace(/[),.;!?]+$/, "");
}
