export type MailServerSecurity = "tls" | "starttls";

export interface MailServerConnectionInput {
  emailAddress: string;
  host: string;
  port: string;
  security: MailServerSecurity;
  username: string;
  password: string;
  caFile: string | null;
}

export interface MailServerConnection {
  email_address: string;
  host: string;
  port: number;
  security: MailServerSecurity;
  username: string;
  password: string;
  ca_file?: string;
}

export function buildMailServerConnection(
  input: MailServerConnectionInput,
): MailServerConnection {
  const port = Number(input.port);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error("Mail server port must be between 1 and 65535.");
  }
  return {
    email_address: input.emailAddress.trim(),
    host: input.host.trim(),
    port,
    security: input.security,
    username: input.username,
    password: input.password,
    ...(input.caFile === null ? {} : { ca_file: input.caFile }),
  };
}
