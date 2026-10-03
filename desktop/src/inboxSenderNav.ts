interface Sender {
  email: string;
  name: string | null;
}

export function inboxSenderNav(
  senders: readonly Sender[],
  current: string | null,
  error: string | null = null,
) {
  const selected = senders.find((sender) => sender.email === current);
  const sender = selected !== undefined || error !== null ? current : null;
  const heading = sender === null ? "All messages"
    : selected?.name ? `${selected.name} (${sender})` : sender;
  return {
    sender,
    heading,
    error,
    items: [
      { sender: null, label: "All messages", selected: sender === null },
      ...senders.map((item) => ({
        sender: item.email, label: item.name || item.email, selected: sender === item.email,
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
