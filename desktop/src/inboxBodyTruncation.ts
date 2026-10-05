interface BodyTruncationFields {
  body_truncated?: boolean | null;
  body_analyzed_chars?: number | null;
  body_source_chars?: number | null;
}

export interface BodyTruncationDisplay {
  badge: string;
  note: string;
}

const isCount = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

/** Badge and note for an analysis computed on a strict prefix of the body; null otherwise. */
export function inboxBodyTruncation(
  item: BodyTruncationFields,
  locale?: string,
): BodyTruncationDisplay | null {
  const analyzed = item.body_analyzed_chars;
  const source = item.body_source_chars;
  if (item.body_truncated !== true || !isCount(analyzed) || !isCount(source) || analyzed >= source) {
    return null;
  }
  const count = new Intl.NumberFormat(locale);
  return {
    badge: "Partial summary",
    note:
      `Summary based on the first ${count.format(analyzed)} of ${count.format(source)} characters. ` +
      "Read the full email in your mail app.",
  };
}
