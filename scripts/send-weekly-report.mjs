import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const envPath = path.join(os.homedir(), '.claude', 'services.env');
const env = Object.fromEntries(
  fs.readFileSync(envPath, 'utf8')
    .split(/\r?\n/)
    .filter((line) => line && !line.startsWith('#') && line.includes('='))
    .map((line) => {
      const idx = line.indexOf('=');
      return [line.slice(0, idx).trim(), line.slice(idx + 1).trim()];
    })
);

const url = env.BRIAN_EMAIL_URL;
const apiKey = env.BRIAN_EMAIL_API_KEY;
const to = env.BRIAN_EMAIL_TO;

const subject = process.env.SUBJECT;
const body = process.env.BODY;

if (!url || !apiKey || !to || !subject || !body) {
  console.error('Missing required env vars or arguments');
  process.exit(1);
}

const res = await fetch(url, {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${apiKey}`,
  },
  body: JSON.stringify({ to, subject, body }),
});

const text = await res.text();
console.log('HTTP', res.status, text);

let parsed;
try {
  parsed = JSON.parse(text);
} catch {
  parsed = null;
}

if (!parsed || parsed.ok !== true) {
  process.exit(1);
}
