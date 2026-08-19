#!/usr/bin/env node

/**
 * Layer 1: DNS MX record validation
 * Checks if email domains have valid mail servers. Catches dead domains fast.
 * Parallel lookups — completes in <2 seconds for 100+ domains.
 *
 * Usage:
 *   node verify_mx.js emails.json                  # outputs mx_clean.json
 *   node verify_mx.js emails.json -o verified.json  # custom output path
 *
 * Input format (JSON array of strings):
 *   ["alice@example.com", "bob@dead-domain.xyz"]
 *
 * Output: same format, minus emails with dead domains
 */

const dns = require('dns');
const fs = require('fs');
const path = require('path');

// DNS error codes that genuinely mean "this domain does not exist / has no
// such records" -- a real, confirmable verdict. Anything else (timeout,
// server failure, connection refused, rate limiting, no network at all) is
// an INFRASTRUCTURE problem with the *lookup*, not proof the domain is
// dead, and must never be reported as DEAD -- doing so would silently
// disqualify every real domain the moment DNS is flaky, unreachable behind
// a firewall/proxy, or rate-limited, which is exactly the kind of "treat
// unknown as confirmed" mistake this project avoids everywhere else.
const DEFINITIVE_ABSENCE_CODES = new Set(['ENOTFOUND', 'ENODATA']);

function checkDomain(domain) {
  return new Promise((resolve) => {
    dns.resolveMx(domain, (mxErr, mxRecords) => {
      if (!mxErr && mxRecords && mxRecords.length > 0) {
        resolve({ domain, verdict: 'VALID', mx: mxRecords.map(r => r.exchange) });
        return;
      }
      const mxDefinitive = !mxErr || DEFINITIVE_ABSENCE_CODES.has(mxErr.code);
      // Fallback: check A record (some domains receive mail without MX)
      dns.resolve4(domain, (aErr, addresses) => {
        if (!aErr && addresses && addresses.length > 0) {
          resolve({ domain, verdict: 'NO_MX', a: addresses, mxError: mxErr?.code });
          return;
        }
        const aDefinitive = !aErr || DEFINITIVE_ABSENCE_CODES.has(aErr.code);
        if (mxDefinitive && aDefinitive) {
          // Both lookups came back with an actual "no such domain/record"
          // response -- a real, confirmable dead domain.
          resolve({ domain, verdict: 'DEAD', mxError: mxErr?.code, aError: aErr?.code });
        } else {
          // At least one lookup failed for an infrastructure reason
          // (ETIMEOUT, ESERVFAIL, ECONNREFUSED, EREFUSED, ...) rather than
          // a definitive absence -- we genuinely don't know.
          resolve({ domain, verdict: 'UNKNOWN', mxError: mxErr?.code, aError: aErr?.code });
        }
      });
    });
  });
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length === 0 || args[0] === '--help' || args[0] === '-h') {
    console.log('Usage: node verify_mx.js <emails.json> [-o output.json]');
    console.log('');
    console.log('Input: JSON array of email strings');
    console.log('Output: {"valid": [...], "dead": [...], "unknown": [...]} email lists');
    process.exit(0);
  }

  const inputFile = args[0];
  const outputIdx = args.indexOf('-o');
  const outputFile = outputIdx !== -1 ? args[outputIdx + 1] : inputFile.replace('.json', '_mx_clean.json');

  const emails = JSON.parse(fs.readFileSync(inputFile, 'utf8'));
  if (!Array.isArray(emails)) {
    console.error('Error: Input must be a JSON array of email strings');
    process.exit(1);
  }

  // Group by domain
  const domainMap = {};
  for (const email of emails) {
    const addr = typeof email === 'string' ? email : email.to || email.email;
    const domain = addr.split('@')[1].toLowerCase();
    if (!domainMap[domain]) domainMap[domain] = [];
    domainMap[domain].push(addr);
  }

  const domains = Object.keys(domainMap);
  console.log(`Checking ${emails.length} emails across ${domains.length} domains...`);

  // Parallel DNS lookups — fast
  const checks = await Promise.all(domains.map(d => checkDomain(d)));

  const valid = [];
  const dead = [];
  const unknown = [];

  for (const check of checks) {
    const addrs = domainMap[check.domain];
    if (check.verdict === 'DEAD') {
      for (const a of addrs) dead.push(a);
      console.log(`  \u274c ${check.domain} \u2014 DEAD (${check.mxError})`);
    } else if (check.verdict === 'UNKNOWN') {
      for (const a of addrs) unknown.push(a);
      console.log(`  \u2753 ${check.domain} \u2014 UNKNOWN (lookup failed: mx=${check.mxError} a=${check.aError}) — NOT treated as dead`);
    } else {
      for (const a of addrs) valid.push(a);
      if (check.verdict === 'NO_MX') {
        console.log(`  \u26a0\ufe0f  ${check.domain} \u2014 no MX but has A record`);
      }
    }
  }

  fs.writeFileSync(outputFile, JSON.stringify({ valid, dead, unknown }, null, 2));
  console.log(`\n\u2705 MX check done: ${valid.length} valid, ${dead.length} dead, ${unknown.length} unknown`);
  console.log(`Output: ${outputFile}`);

  if (dead.length > 0) {
    console.log('\nDead domains removed:');
    for (const d of dead) console.log(`  ${d}`);
  }

  if (unknown.length > 0) {
    console.log('\nUnknown (DNS lookup failed, not disqualified):');
    for (const u of unknown) console.log(`  ${u}`);
  }
}

main().catch(err => { console.error('Fatal:', err); process.exit(1); });
