import assert from "node:assert/strict";
import test from "node:test";
import { buildMailServerConnection } from "../src/mailServerConnection.ts";

test("mail server connection uses the engine contract without changing credentials", () => {
  assert.deepEqual(
    buildMailServerConnection({
      emailAddress: " owner@example.com ",
      host: " mail.example.com ",
      port: "993",
      security: "tls",
      username: " owner ",
      password: " private password ",
      caFile: "/private/root.pem",
      sentFolder: "  ",
    }),
    {
      email_address: "owner@example.com",
      host: "mail.example.com",
      port: 993,
      security: "tls",
      username: " owner ",
      password: " private password ",
      ca_file: "/private/root.pem",
    },
  );
});

test("mail server connection omits an unselected CA path", () => {
  const connection = buildMailServerConnection({
    emailAddress: "owner@example.com",
    host: "mail.example.com",
    port: "143",
    security: "starttls",
    username: "owner",
    password: "private",
    caFile: null,
  });

  assert.equal(connection.port, 143);
  assert.equal(connection.security, "starttls");
  assert.equal("ca_file" in connection, false);
});

for (const port of ["", "0", "65536", "1.5", "not-a-port"]) {
  test(`mail server connection rejects invalid port ${JSON.stringify(port)}`, () => {
    assert.throws(
      () =>
        buildMailServerConnection({
          emailAddress: "owner@example.com",
          host: "mail.example.com",
          port,
          security: "tls",
          username: "owner",
          password: "private",
          caFile: null,
        }),
      /between 1 and 65535/,
    );
  });
}

test("a configured Sent folder name travels with the connection, trimmed", () => {
  const base = {
    emailAddress: "owner@example.com",
    host: "mail.example.com",
    port: "993",
    security: "tls" as const,
    username: "owner",
    password: "private",
    caFile: null,
  };
  assert.equal("sent_folder" in buildMailServerConnection({ ...base, sentFolder: "" }), false);
  assert.equal("sent_folder" in buildMailServerConnection({ ...base, sentFolder: "  " }), false);
  assert.equal("sent_folder" in buildMailServerConnection(base), false);
  assert.equal(
    buildMailServerConnection({ ...base, sentFolder: " Enviados " }).sent_folder,
    "Enviados",
  );
});
