interface Sender {
  email: string;
  name: string | null;
}

export function inboxSenderNav(
  senders: readonly Sender[],
  current: string | null,
  error: string | null = null,
  finder = "",
) {
  const selected = senders.find((sender) => sender.email === current);
  const sender = selected !== undefined || error !== null ? current : null;
  const heading = sender === null ? "All senders"
    : selected?.name ? `${selected.name} (${sender})` : sender;
  const needle = finder.toLowerCase();
  const matches = senders.filter(item => item.email.toLowerCase().includes(needle)
    || (item.name ?? "").toLowerCase().includes(needle));
  const pinned = sender !== null && !matches.some(item => item.email === sender)
    ? selected ?? { email: sender, name: null } : null;
  return {
    sender,
    heading,
    error,
    emptyText: error !== null ? null : senders.length === 0 ? "No watched senders yet."
      : matches.length === 0 ? "No watched senders match your search." : null,
    items: [
      { sender: null, label: "All senders", selected: sender === null, pinned: false },
      ...(pinned ? [pinned] : []).concat(matches).map((item) => ({
        sender: item.email, label: item.name || item.email, selected: sender === item.email, pinned: item === pinned,
      })),
    ],
  };
}

export function inboxSenderEmptyText(
  query: {
    keyword?: string | null;
    sender_query?: string | null;
    priority?: string | null;
    category?: string | null;
    status?: string | null;
  },
  accountSelection: string,
): string {
  if ([query.keyword, query.sender_query, query.priority, query.category, query.status].some(Boolean)) {
    return "No retained messages from this sender match these filters.";
  }
  return accountSelection !== "all"
    ? "No retained messages from this sender in this account."
    : "No retained messages from this sender.";
}
