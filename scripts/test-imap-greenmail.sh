#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
greenmail_image="${GREENMAIL_IMAGE:-greenmail/standalone@sha256:9f32971b4f25d32b4de6fa2e297423768441c65e4541f6aecd7631c890a229a7}"
container_name="eom-imap-greenmail-$$-$RANDOM"
fixture_dir="$(mktemp -d "${TMPDIR:-/tmp}/eom-imap-greenmail.XXXXXX")"

cleanup() {
  exit_code=$?
  trap - EXIT INT TERM
  docker stop "$container_name" >/dev/null 2>&1 || true
  rm -f \
    "$fixture_dir/cert.pem" \
    "$fixture_dir/greenmail.p12" \
    "$fixture_dir/key.pem"
  rmdir "$fixture_dir" 2>/dev/null || true
  exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for command in docker openssl uv; do
  if ! command -v "$command" >/dev/null; then
    echo "Required command is unavailable: $command" >&2
    exit 2
  fi
done

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$fixture_dir/key.pem" \
  -out "$fixture_dir/cert.pem" \
  -days 1 \
  -subj /CN=localhost \
  -addext subjectAltName=DNS:localhost >/dev/null 2>&1
openssl pkcs12 -export \
  -in "$fixture_dir/cert.pem" \
  -inkey "$fixture_dir/key.pem" \
  -out "$fixture_dir/greenmail.p12" \
  -passout pass:changeit \
  -name greenmail
chmod 755 "$fixture_dir"
chmod 644 "$fixture_dir/greenmail.p12"

docker run -d --rm \
  --name "$container_name" \
  -p 127.0.0.1::3025 \
  -p 127.0.0.1::3993 \
  -v "$fixture_dir/greenmail.p12:/tmp/greenmail.p12:ro" \
  -e "GREENMAIL_OPTS=-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.tls.keystore.file=/tmp/greenmail.p12 -Dgreenmail.tls.keystore.password=changeit -Dgreenmail.users=owner:fixture-password@example.test -Dgreenmail.users.login=email" \
  "$greenmail_image" >/dev/null

smtp_port="$(docker port "$container_name" 3025/tcp | sed 's/.*://')"
imaps_port="$(docker port "$container_name" 3993/tcp | sed 's/.*://')"
if [[ ! "$smtp_port" =~ ^[0-9]+$ || ! "$imaps_port" =~ ^[0-9]+$ ]]; then
  echo "GreenMail did not publish the required loopback ports" >&2
  exit 1
fi

ready=0
for _attempt in $(seq 1 50); do
  if uv run --project "$repo_root" python -c \
    'import socket, sys; socket.create_connection(("127.0.0.1", int(sys.argv[1])), 1).close()' \
    "$imaps_port" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.1
done
if [ "$ready" -ne 1 ]; then
  docker logs "$container_name" >&2
  echo "GreenMail IMAPS did not become ready" >&2
  exit 1
fi

export EOM_GREENMAIL_HOST=localhost
export EOM_GREENMAIL_IMAPS_PORT="$imaps_port"
export EOM_GREENMAIL_SMTP_PORT="$smtp_port"
export EOM_GREENMAIL_CA_FILE="$fixture_dir/cert.pem"

cd "$repo_root"
uv run pytest -q tests/test_imap_greenmail_integration.py
