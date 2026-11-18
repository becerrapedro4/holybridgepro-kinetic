'use strict';

/**
 * Mide la latencia extremo a extremo: cambio en Holyrics -> frame SSE en el overlay.
 *
 * El mock cambia de texto exactamente en cada frontera de segundo, asi que
 * `Date.now() % 1000` al recibir el frame es la latencia real del camino
 * (sondeo + parseo + push SSE).
 *
 * Uso: node tools/latency-check.js [http://127.0.0.1:7333] [muestras]
 */

const BASE = process.argv[2] || 'http://127.0.0.1:7333';
const SAMPLES = Number(process.argv[3] || 20);

async function main() {
  const response = await fetch(`${BASE}/api/events`, { headers: { Accept: 'text/event-stream' } });
  if (!response.ok || !response.body) throw new Error(`SSE HTTP ${response.status}`);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const samples = [];
  let lastText = null;

  const deadline = Date.now() + 60000;

  while (samples.length < SAMPLES && Date.now() < deadline) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let split;
    while ((split = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      const dataLine = frame.split('\n').find((l) => l.startsWith('data: '));
      if (!dataLine) continue;
      let payload;
      try {
        payload = JSON.parse(dataLine.slice(6));
      } catch {
        continue;
      }
      if (!payload.text || payload.text === lastText) continue;
      lastText = payload.text;
      samples.push(Date.now() % 1000);
      console.log(`  #${samples.length} ${String(samples[samples.length - 1]).padStart(4)} ms  ${payload.text.split('\n')[0]}`);
    }
  }

  reader.cancel().catch(() => {});
  if (!samples.length) {
    console.log('sin muestras: revisa que el bridge y el mock esten arriba');
    return;
  }

  const sorted = [...samples].sort((a, b) => a - b);
  const avg = sorted.reduce((a, b) => a + b, 0) / sorted.length;
  console.log(`\nlatencia (Holyrics -> overlay SSE), ${sorted.length} muestras:`);
  console.log(`  min ${sorted[0]} ms | mediana ${sorted[Math.floor(sorted.length / 2)]} ms | media ${avg.toFixed(1)} ms | max ${sorted[sorted.length - 1]} ms`);
}

main().catch((err) => {
  console.error('[latency] error:', err.message);
  process.exit(1);
});
