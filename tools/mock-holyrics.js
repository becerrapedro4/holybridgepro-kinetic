'use strict';

/**
 * Mock de Holyrics: sirve /view/text.json con el mismo shape que usa HolyBridge Pro.
 * Cambia de "slide" exactamente cada segundo (reloj del sistema), lo que permite
 * medir la latencia real del bridge (el texto N aparece en el segundo N).
 *
 * Uso: node tools/mock-holyrics.js [puerto]
 */

const http = require('node:http');

const PORT = Number(process.argv[2] || 8099);
const VERSICULOS = [
  'Porque de tal manera amó Dios al mundo',
  'que ha dado a su Hijo unigénito',
  'para que todo aquel que en él cree',
  'no se pierda, mas tenga vida eterna',
];

function slideActual() {
  const n = Math.floor(Date.now() / 1000);
  return n;
}

function render(n) {
  const lineas = VERSICULOS[n % VERSICULOS.length].split(/\s{2,}/);
  const html = `<b>${lineas.join('<br>')}</b><br><desc>Juan 3:16 (RVR1960)</desc>`;
  return html;
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/view/text.json') {
    const n = slideActual();
    const body = JSON.stringify({
      map: {
        text: render(n),
        type: 'verse',
        slide: String(n),
      },
    });
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(body);
    return;
  }
  if (url.pathname === '/api/GetCPInfo') {
    const body = JSON.stringify({ status: 'ok', data: { text: render(slideActual()) } });
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(body);
    return;
  }
  res.writeHead(404, { 'Content-Type': 'text/plain' });
  res.end('not found');
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[mock-holyrics] http://127.0.0.1:${PORT}/view/text.json (cambia cada 1s)`);
});
