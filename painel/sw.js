// Guarda a "casca" do painel para ele abrir mesmo sem conexão. Os dados vêm do
// Apps Script (outra origem) e não passam por aqui; o painel guarda a última
// resposta no localStorage.
const CACHE = 'levainpeak-painel-v12';
const CASCA = ['./', 'index.html', 'painel.css', 'painel.js', 'manifest.webmanifest', 'icon.svg', 'icon-192.png', 'icon-512.png'];

self.addEventListener('install', (evento) => {
  evento.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(CASCA)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (evento) => {
  evento.waitUntil(
    caches.keys()
      .then((chaves) => Promise.all(chaves.filter((chave) => chave !== CACHE).map((chave) => caches.delete(chave))))
      .then(() => self.clients.claim())
  );
});

// Rede primeiro (uma edição aparece na hora), cache só quando está sem rede.
self.addEventListener('fetch', (evento) => {
  const url = new URL(evento.request.url);
  if (url.origin !== location.origin) return;
  evento.respondWith(fetch(evento.request).catch(() => caches.match(evento.request)));
});
