const unsafeTechnicalDetail =
  /(?:\bHTTP\b|https?:|127\.0\.0\.1|localhost|traceback|authorization|bearer|api[_ -]?key|token|secret|password|[a-z]:\\|\\\\[^\\]+\\|\\users\\|\/(?:users|home|var|tmp|opt|etc)\/|\.\.)/iu;

export function safeDisplayCopy(value: string, fallback: string): string {
  const normalized = value.trim();
  if (
    normalized.length === 0 ||
    normalized.length > 240 ||
    unsafeTechnicalDetail.test(normalized)
  ) {
    return fallback;
  }
  return normalized;
}

export function safeUserMessage(error: unknown, fallback: string): string {
  if (!(error instanceof Error)) return fallback;
  return safeDisplayCopy(error.message, fallback);
}
