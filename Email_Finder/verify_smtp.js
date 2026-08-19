#!/usr/bin/env node

/**
 * Layer 2: SMTP RCPT TO mailbox verification
 * Connects to mail servers and checks if individual mailboxes exist.
 * Same technique used by ZeroBounce, NeverBounce, and other email verification services.
 *
 * Usage:
 *   node verify_smtp.js emails.json                  # outputs emails_verified.json
 *   node verify_smtp.js emails.json -o output.json   # custom output path
 *   EHLO_DOMAIN=mydomain.com node verify_smtp.js emails.json  # custom EHLO
 *
 * Input format (JSON array of strings):
 *   ["alice@example.com", "bob@company.org"]
 *
 * Output: same format, minus confirmed-bad mailboxes
 *
 * Environment variables:
 *   EHLO_DOMAIN  - Domain to use in EHLO/MAIL FROM (default: "verify.local")
 *   SMTP_TIMEOUT - Connection timeout in ms (default: 15000)
 */

const dns = require('dns');
const net = require('net');
const fs = require('fs');

const EHLO_DOMAIN = process.env.EHLO_DOMAIN || 'verify.local';
const TIMEOUT = parseInt(process.env.SMTP_TIMEOUT || '15000', 10);

function resolveMx(domain) {
  return new Promise((resolve) => {
    dns.resolveMx(domain, (err, records) => {
      if (err || !records || records.length === 0) resolve(null);
      else resolve(records.sort((a, b) => a.priority - b.priority)[0].exchange);
    });
  });
}

function smtpVerify(mxHost, email) {
  return new Promise((resolve) => {
    const socket = new net.Socket();
    let step = 'connect';

    const finish = (result) => {
      try { socket.destroy(); } catch {}
      resolve(result);
    };

    socket.setTimeout(TIMEOUT);
    socket.on('timeout', () => finish({ verdict: 'TIMEOUT', step }));
    socket.on('error', (err) => finish({ verdict: 'CONN_ERROR', error: err.code, step }));

    socket.on('data', (data) => {
      const line = data.toString();
      const code = parseInt(line.substring(0, 3));

      if (step === 'connect') {
        if (code === 220) { step = 'ehlo'; socket.write(`EHLO ${EHLO_DOMAIN}\r\n`); }
        else finish({ verdict: 'REJECTED', code, step });
      } else if (step === 'ehlo') {
        if (code === 250) { step = 'mail_from'; socket.write(`MAIL FROM:<noreply@${EHLO_DOMAIN}>\r\n`); }
        else finish({ verdict: 'EHLO_FAIL', code, step });
      } else if (step === 'mail_from') {
        if (code === 250) { step = 'rcpt_to'; socket.write(`RCPT TO:<${email}>\r\n`); }
        else finish({ verdict: 'MAIL_FROM_FAIL', code, step });
      } else if (step === 'rcpt_to') {
        socket.write('QUIT\r\n');
        if (code === 250) {
          finish({ verdict: 'EXISTS', code });
        } else if ([550, 551, 552, 553, 554].includes(code)) {
          finish({ verdict: 'NOT_EXISTS', code, detail: line.trim().substring(0, 200) });
        } else if ([450, 451, 452].includes(code)) {
          finish({ verdict: 'TEMP_FAIL', code, detail: line.trim().substring(0, 200) });
        } else if (code === 421) {
          finish({ verdict: 'RATE_LIMITED', code });
        } else {
          finish({ verdict: 'UNKNOWN', code, detail: line.trim().substring(0, 200) });
        }
      }
    });

    socket.connect(25, mxHost);
  });
}

function testCatchAll(mxHost, domain) {
  const fake = `xzq9k7m3p2_test_${Date.now()}@${domain}`;
  return smtpVerify(mxHost, fake);
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function main() {
  const args = process.argv.slice(2);
  if (args.length === 0 || args[0] === '--help' || args[0] === '-h') {
    console.log('Usage: node verify_smtp.js <emails.json> [-o output.json]');
    console.log('');
    console.log('Input: JSON array of email strings');
    console.log('Output: JSON array of verified emails (bad mailboxes removed)');
    console.log('');
    console.log('Environment variables:');
    console.log('  EHLO_DOMAIN   Domain for SMTP EHLO (default: verify.local)');
    console.log('  SMTP_TIMEOUT  Connection timeout in ms (default: 15000)');
    process.exit(0);
  }

  const inputFile = args[0];
  const outputIdx = args.indexOf('-o');
  const outputFile = outputIdx !== -1 ? args[outputIdx + 1] : inputFile.replace('.json', '_verified.json');

  const emails = JSON.parse(fs.readFileSync(inputFile, 'utf8'));
  if (!Array.isArray(emails)) {
    console.error('Error: Input must be a JSON array of email strings');
    process.exit(1);
  }

  console.log(`SMTP verifying ${emails.length} mailboxes (EHLO: ${EHLO_DOMAIN})...\n`);

  // Group by domain for efficient verification
  const domainMap = {};
  for (const email of emails) {
    const addr = typeof email === 'string' ? email : email.to || email.email;
    const domain = addr.split('@')[1].toLowerCase();
    if (!domainMap[domain]) domainMap[domain] = [];
    domainMap[domain].push(addr);
  }

  const domains = Object.keys(domainMap);
  const results = { exists: [], not_exists: [], unknown: [], catch_all: [] };

  for (let i = 0; i < domains.length; i++) {
    const domain = domains[i];
    const domainEmails = domainMap[domain];

    const mx = await resolveMx(domain);
    if (!mx) {
      console.log(`[${i+1}/${domains.length}] \u274c ${domain} \u2014 no MX`);
      for (const e of domainEmails) results.unknown.push(e);
      continue;
    }

    // Test for catch-all first (saves time)
    const catchAllResult = await testCatchAll(mx, domain);
    if (catchAllResult.verdict === 'EXISTS') {
      console.log(`[${i+1}/${domains.length}] \ud83d\udd04 ${domain} \u2014 catch-all (accepts everything)`);
      for (const e of domainEmails) results.catch_all.push(e);
      await sleep(500);
      continue;
    }

    // Verify each email individually
    for (const email of domainEmails) {
      const result = await smtpVerify(mx, email);
      const icon = result.verdict === 'EXISTS' ? '\u2705' : result.verdict === 'NOT_EXISTS' ? '\u274c' : '\u2753';
      console.log(`[${i+1}/${domains.length}] ${icon} ${email} \u2014 ${result.verdict}${result.code ? ` (${result.code})` : ''}`);

      if (result.verdict === 'EXISTS') results.exists.push(email);
      else if (result.verdict === 'NOT_EXISTS') results.not_exists.push(email);
      else results.unknown.push(email);
    }

    await sleep(300); // Be polite to mail servers
  }

  // Summary
  console.log('\n=== SMTP VERIFICATION RESULTS ===');
  console.log(`\u2705 EXISTS:      ${results.exists.length}`);
  console.log(`\ud83d\udd04 CATCH-ALL:  ${results.catch_all.length}`);
  console.log(`\u2753 UNKNOWN:    ${results.unknown.length}`);
  console.log(`\u274c NOT EXISTS: ${results.not_exists.length}`);

  // Safe to send = EXISTS + CATCH_ALL + UNKNOWN (benefit of the doubt)
  const verified = [...results.exists, ...results.catch_all, ...results.unknown];

  fs.writeFileSync(outputFile, JSON.stringify(verified, null, 2));
  console.log(`\nVerified: ${outputFile} (${verified.length} emails)`);

  if (results.not_exists.length > 0) {
    console.log('\nRemoved (confirmed bad):');
    for (const e of results.not_exists) console.log(`  ${e}`);
  }

  if (verified.length === 0) {
    console.log('No verified emails remaining.');
    process.exit(1);
  }
}

main().catch(err => { console.error('Fatal:', err); process.exit(1); });
