'use strict';

/**
 * Demo: simula lo que HolyBridge empujaria al bridge.
 * Uso: node tools/demo.js [http://127.0.0.1:7333]
 */

const BASE = process.argv[2] || 'http://127.0.0.1:7333';

const script = [
  { text: 'Cantando con el corazon', effect: 'pop' },
  { text: 'Tu gracia me alcanzo', effect: 'rise' },
  { text: 'Y no hay nada\nque me separe de ti', effect: 'slide' },
  { text: 'Eres la luz', effect: 'blur' },
  { text: 'Eres la luz de mi vida', effect: 'typewriter' },
  { text: 'Aleluya, aleluya', effect: 'wave' },
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  console.log(`[demo] enviando a ${BASE}/api/text`);
  for (const entry of script) {
    const started = process.hrtime.bigint();
    const response = await fetch(`${BASE}/api/text`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...entry, title: 'Demo', progress: null }),
    });
    const elapsed = Number(process.hrtime.bigint() - started) / 1e6;
    console.log(`  ${response.status} ${elapsed.toFixed(2)}ms  ${JSON.stringify(entry.text)}`);
    await sleep(2600);
  }
  await fetch(`${BASE}/api/clear`, { method: 'POST' });
  console.log('[demo] listo');
}

main().catch((err) => {
  console.error('[demo] error:', err.message);
  process.exit(1);
});
