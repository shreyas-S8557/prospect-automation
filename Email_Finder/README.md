# email-verifier

Free email verification using DNS MX + SMTP RCPT TO. Zero dependencies, pure Node.js.

Same technique used by ZeroBounce, NeverBounce, and other paid email verification services — except this is free and you run it yourself.

## What it does

**Layer 1 — MX Check** (`verify_mx.js`): Checks if email domains have valid mail servers. Catches dead domains instantly. Parallel DNS lookups complete in under 2 seconds for 100+ domains.

**Layer 2 — SMTP Check** (`verify_smtp.js`): Connects to mail servers and checks if individual mailboxes exist using the SMTP `RCPT TO` command. Detects catch-all domains automatically.

## Quick start

```bash
git clone https://github.com/phuaky/email-verifier.git
cd email-verifier

# Create your email list
echo '["alice@example.com", "bob@dead-domain.xyz"]' > emails.json

# Layer 1: Remove dead domains (fast, <2 seconds)
node verify_mx.js emails.json

# Layer 2: Verify individual mailboxes (slower, ~1 sec per domain)
node verify_smtp.js emails_mx_clean.json
```

## Input format

JSON array of email strings:

```json
["alice@example.com", "bob@company.org", "carol@startup.io"]
```

## Usage

### MX verification (Layer 1)

```bash
node verify_mx.js emails.json                  # outputs emails_mx_clean.json
node verify_mx.js emails.json -o clean.json     # custom output path
```

### SMTP verification (Layer 2)

```bash
node verify_smtp.js emails.json                         # outputs emails_verified.json
node verify_smtp.js emails.json -o verified.json         # custom output path
EHLO_DOMAIN=mydomain.com node verify_smtp.js emails.json # custom EHLO domain
```

### Full pipeline

```bash
node verify_mx.js emails.json -o mx_clean.json
node verify_smtp.js mx_clean.json -o verified.json
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EHLO_DOMAIN` | `verify.local` | Domain used in SMTP EHLO greeting |
| `SMTP_TIMEOUT` | `15000` | Connection timeout in milliseconds |

## How SMTP verification works

1. **DNS lookup** — Find the domain's mail server (MX record)
2. **Catch-all detection** — Send a fake email first. If the server accepts it, the domain accepts everything (catch-all) and individual verification is unreliable
3. **RCPT TO check** — For non-catch-all domains, check each email individually:
   - `250` = mailbox exists
   - `550` = mailbox does not exist (removed from output)
   - `450/451` = temporary failure (kept, benefit of the doubt)

The script never actually sends any email. It disconnects after the `RCPT TO` response.

## Verification categories

| Category | Meaning | Action |
|----------|---------|--------|
| EXISTS | Mailbox confirmed | Safe to send |
| CATCH_ALL | Domain accepts everything | Kept (can't verify individually) |
| UNKNOWN | Couldn't determine | Kept (benefit of the doubt) |
| NOT_EXISTS | Mailbox confirmed dead | Removed from output |

## Requirements

- Node.js 14+
- No dependencies (uses only built-in `dns`, `net`, `fs` modules)
- Outbound port 25 access (some cloud providers block this)

## Limitations

- **Port 25 blocking**: AWS, GCP, and some ISPs block outbound port 25. Run from a VPS or your local machine.
- **Rate limiting**: Some mail servers rate-limit SMTP connections. The script adds delays between checks.
- **Catch-all domains**: Can't verify individual addresses on catch-all domains.
- **Greylisting**: Some servers temporarily reject first attempts. UNKNOWN results may be valid.

## License

MIT
