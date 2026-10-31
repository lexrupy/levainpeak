'use strict';

// Painel só de leitura do LevainPeak. Lê a planilha pelo Apps Script (o mesmo
// endereço que o app do PC usa para enviar) e desenha o gráfico em SVG, sem
// bibliotecas. O endereço e o token ficam só no localStorage deste aparelho.

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 60 * 1000;
const AGES_MS = 30 * 1000;

const store = {
  get(key) {
    try { return JSON.parse(localStorage.getItem(key)); } catch (erro) { return null; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (erro) { /* cheio ou bloqueado */ }
  },
};

const esc = (texto) => String(texto).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// ---------------------------------------------------------------- configuração

function readConfig() {
  // Primeira visita: #u=<endereço /exec>&t=<token>. Guarda e tira do endereço da página.
  const parametros = new URLSearchParams(location.hash.slice(1));
  if (parametros.get('u') && parametros.get('t')) {
    store.set('cfg', { u: parametros.get('u'), t: parametros.get('t') });
    history.replaceState(null, '', location.pathname + location.search);
  }
  return store.get('cfg');
}

function showSetup() {
  $('configurar').hidden = false;
  $('painel').hidden = true;
  $('subtitulo').textContent = 'Não configurado.';
  $('subtitulo').className = '';
}

// ---------------------------------------------------------------- tempo

function ageMinutes(iso) {
  const instante = Date.parse(iso);
  return Number.isNaN(instante) ? null : Math.max(0, (Date.now() - instante) / 60000);
}

function ageText(minutos) {
  if (minutos === null) return '—';
  if (minutos < 1) return 'agora';
  if (minutos < 90) return `há ${Math.round(minutos)} min`;
  const horas = minutos / 60;
  if (horas < 48) return `há ${horas.toFixed(1).replace('.', ',')} h`;
  return `há ${Math.round(horas / 24)} dias`;
}

const horaCurta = new Intl.DateTimeFormat('pt-BR', { hour: '2-digit', minute: '2-digit' });
const dataHora = new Intl.DateTimeFormat('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });

// ---------------------------------------------------------------- gráfico

const CLASSE_DA_FASE = {
  'Fase inicial': 'faixa-fase-inicial',
  'Crescimento': 'faixa-crescimento',
  'Pico': 'faixa-pico',
  'Declínio': 'faixa-declinio',
};

function smoothPath(pontos) {
  if (pontos.length < 3) return 'M' + pontos.map((p) => `${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' L');
  let caminho = `M${pontos[0][0].toFixed(1)} ${pontos[0][1].toFixed(1)}`;
  for (let i = 1; i < pontos.length - 1; i++) {
    const meioX = (pontos[i][0] + pontos[i + 1][0]) / 2;
    const meioY = (pontos[i][1] + pontos[i + 1][1]) / 2;
    caminho += ` Q${pontos[i][0].toFixed(1)} ${pontos[i][1].toFixed(1)} ${meioX.toFixed(1)} ${meioY.toFixed(1)}`;
  }
  const ultimo = pontos[pontos.length - 1];
  return `${caminho} L${ultimo[0].toFixed(1)} ${ultimo[1].toFixed(1)}`;
}

function drawChart(chart) {
  const L = 34, R = 34, T = 24, B = 26, W = 360, H = 250;
  const pts = chart.pts || [];
  if (pts.length < 2) {
    $('grafico').textContent = 'O gráfico aparece depois de algumas medições.';
    return;
  }
  const duracao = Math.max(...pts.map((p) => p[0]), 1);
  const x = (dt) => L + (dt / duracao) * (W - L - R);
  // Folga sem rótulo abaixo do 0% e acima do 100%, como no app do PC.
  const alturaUtil = H - T - B;
  const chao = H - B - alturaUtil * 0.12;
  const teto = T + alturaUtil * 0.09;
  const yVolume = (p) => chao - (Math.min(100, Math.max(0, p)) / 100) * (chao - teto);

  const temperaturas = pts.map((p) => p[2]).filter((v) => v !== null && v !== undefined);
  let baixo = Math.min(...temperaturas);
  let alto = Math.max(...temperaturas);
  if (temperaturas.length) {
    if (Array.isArray(chart.temp_eixo) && chart.temp_eixo.length === 2) {
      // Os limites escolhidos no app (a mesma escala do gráfico de lá).
      [baixo, alto] = chart.temp_eixo;
    } else if (alto - baixo < 1) {
      const centro = (alto + baixo) / 2;
      baixo = centro - 0.5;
      alto = centro + 0.5;
    } else {
      const margem = (alto - baixo) * 0.08;
      baixo -= margem;
      alto += margem;
    }
  }
  const yTemperatura = (v) => chao - ((v - baixo) / (alto - baixo)) * (chao - teto);

  const partes = [];
  // Faixas das fases, com o nome em cima.
  const fases = chart.fases || [];
  fases.forEach((fase, i) => {
    const [nome, inicio] = fase;
    const esquerda = i === 0 ? L : x(inicio);
    const direita = i + 1 < fases.length ? x(fases[i + 1][1]) : x(duracao);
    partes.push(`<rect class="${CLASSE_DA_FASE[nome] || 'faixa-fase-inicial'}" x="${esquerda.toFixed(1)}" y="${T}" width="${Math.max(0, direita - esquerda).toFixed(1)}" height="${H - B - T}"/>`);
    if (i > 0) partes.push(`<line class="divisor" x1="${esquerda.toFixed(1)}" y1="${T}" x2="${esquerda.toFixed(1)}" y2="${H - B}"/>`);
    if (direita - esquerda >= 46) partes.push(`<text class="fase-nome" x="${((esquerda + direita) / 2).toFixed(1)}" y="${T + 11}" text-anchor="middle">${esc(nome)}</text>`);
  });
  // Grade e eixos: 0 a 100% à esquerda, temperatura à direita nas mesmas linhas.
  [0, 25, 50, 75, 100].forEach((percentual) => {
    const y = yVolume(percentual);
    partes.push(`<line class="grade" x1="${L}" y1="${y.toFixed(1)}" x2="${W - R}" y2="${y.toFixed(1)}"/>`);
    partes.push(`<text class="eixo" x="${L - 4}" y="${(y + 3).toFixed(1)}" text-anchor="end">${percentual}%</text>`);
    if (temperaturas.length) {
      const valor = baixo + (percentual / 100) * (alto - baixo);
      partes.push(`<text class="eixo" x="${W - R + 4}" y="${(y + 3).toFixed(1)}" text-anchor="start">${Math.abs(valor - Math.round(valor)) < 1e-6 ? valor.toFixed(0) : valor.toFixed(1)}°</text>`);
    }
  });
  // Horas no eixo de baixo.
  const inicioMs = Date.parse(chart.t0);
  const formato = duracao > 86400 ? dataHora : horaCurta;
  for (let i = 0; i <= 4; i++) {
    const dt = (duracao * i) / 4;
    const ancora = i === 0 ? 'start' : (i === 4 ? 'end' : 'middle');
    const rotulo = Number.isNaN(inicioMs) ? '' : formato.format(new Date(inicioMs + dt * 1000));
    partes.push(`<text class="eixo" x="${x(dt).toFixed(1)}" y="${H - 8}" text-anchor="${ancora}">${esc(rotulo)}</text>`);
  }
  // Temperatura (linha fina, interrompida onde não há leitura) e volume (curva).
  if (temperaturas.length) {
    let segmento = [];
    const fechar = () => {
      if (segmento.length > 1) partes.push(`<polyline class="temp" points="${segmento.map((p) => p.map((v) => v.toFixed(1)).join(',')).join(' ')}"/>`);
      segmento = [];
    };
    pts.forEach((p) => {
      if (p[2] === null || p[2] === undefined) fechar(); else segmento.push([x(p[0]), yTemperatura(p[2])]);
    });
    fechar();
  }
  const volume = pts.map((p) => [x(p[0]), yVolume(p[1])]);
  partes.push(`<path class="vol" d="${smoothPath(volume)}"/>`);
  const ultimo = volume[volume.length - 1];
  partes.push(`<circle class="ponta" cx="${ultimo[0].toFixed(1)}" cy="${ultimo[1].toFixed(1)}" r="3.2"/>`);

  $('grafico').innerHTML = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">${partes.join('')}</svg>`;
  $('grafico-titulo').textContent = `Volume (% do ${chart.escala}; 0% = início) · temperatura em °C`;
}

// ---------------------------------------------------------------- tela

let ultimo = null; // última resposta (para atualizar só as idades a cada 30 s)
let ultimoFoiDoCache = false;

function render(dados, doCache) {
  ultimo = dados;
  ultimoFoiDoCache = doCache;
  const aoVivo = (dados.ao_vivo && dados.ao_vivo.live) || {};
  const chart = dados.grafico && dados.grafico.chart;

  $('configurar').hidden = true;
  $('painel').hidden = false;
  $('sessao').textContent = aoVivo.sessao || (chart && chart.nome) || 'LevainPeak';

  const volume = aoVivo.volume_pct;
  $('volume').textContent = volume === undefined || volume === null ? '—' : `${Number(volume).toFixed(0)}%`;
  $('volume-nota').textContent = aoVivo.crescimento ? `${Number(aoVivo.crescimento).toFixed(2).replace('.', ',')}× o nível inicial` : 'da capacidade do pote';
  $('fase').textContent = aoVivo.fase || '—';
  $('fase-nota').textContent = '';
  const temperatura = aoVivo.temperatura_c;
  $('temperatura').textContent = temperatura === undefined || temperatura === null ? '—' : `${Number(temperatura).toFixed(1).replace('.', ',')} °C`;
  $('temperatura-nota').textContent = aoVivo.temperatura_nota || '';
  $('previsao').textContent = aoVivo.previsao || '—';

  if (chart) drawChart(chart);
  else $('grafico').textContent = 'O gráfico aparece depois de algumas medições.';

  const imagem = dados.snapshot;
  if (imagem && imagem.jpeg_b64) {
    $('imagem').src = `data:image/jpeg;base64,${imagem.jpeg_b64}`;
    $('imagem-area').hidden = false;
  } else {
    $('imagem-area').hidden = true;
  }
  $('rodape').textContent = [aoVivo.perfil && `Perfil: ${aoVivo.perfil}`, aoVivo.status && `câmera: ${aoVivo.status}`].filter(Boolean).join(' · ');
  renderAges();
}

function renderAges() {
  if (!ultimo) return;
  const aoVivo = ultimo.ao_vivo || {};
  const idade = ageMinutes(aoVivo.atualizado_em);
  const limite = (((aoVivo.live && aoVivo.live.intervalo_s) || 240) * 3) / 60; // 3 ciclos sem dados
  const subtitulo = $('subtitulo');
  if (idade === null) {
    subtitulo.textContent = 'Sem dados ainda.';
    subtitulo.className = '';
  } else if (idade > limite) {
    subtitulo.textContent = `Sem dados novos ${ageText(idade)} — o app do PC parou de enviar?`;
    subtitulo.className = 'velho';
  } else {
    subtitulo.textContent = `Atualizado ${ageText(idade)}${ultimoFoiDoCache ? ' (sem conexão: dado guardado)' : ''}`;
    subtitulo.className = 'fresco';
  }
  const imagem = ultimo.snapshot;
  if (imagem && imagem.jpeg_b64) {
    const instante = Date.parse(imagem.t);
    $('imagem-legenda').textContent = Number.isNaN(instante)
      ? 'Última imagem recebida'
      : `Última imagem recebida: ${horaCurta.format(new Date(instante))} (${ageText(ageMinutes(imagem.t))})`;
  }
}

function showWarning(texto) {
  $('aviso').hidden = !texto;
  $('aviso').textContent = texto || '';
}

// ---------------------------------------------------------------- dados

async function load() {
  const cfg = readConfig();
  if (!cfg) { showSetup(); return; }
  const guardado = store.get('ultimo');
  const snap = guardado && guardado.snapshot && guardado.snapshot.t ? guardado.snapshot.t : '';
  try {
    const url = `${cfg.u}?token=${encodeURIComponent(cfg.t)}&o=painel&snap=${encodeURIComponent(snap)}`;
    const resposta = await fetch(url, { cache: 'no-store' });
    const dados = await resposta.json();
    if (!dados.ok) {
      const falha = new Error(dados.erro || 'resposta inválida');
      falha.tipo = /token/i.test(falha.message) ? 'recusado' : (/desconhecido/i.test(falha.message) ? 'desatualizado' : 'erro');
      throw falha;
    }
    // O servidor não repete a imagem que já temos: reaproveita a guardada.
    if (dados.snapshot && dados.snapshot.igual && guardado && guardado.snapshot) dados.snapshot = guardado.snapshot;
    store.set('ultimo', dados);
    showWarning('');
    render(dados, false);
  } catch (erro) {
    const tipo = erro.tipo || 'rede'; // sem 'tipo': a requisição nem chegou (rede, endereço errado)
    if (tipo === 'recusado') {
      showWarning('O token foi recusado. Confira o token na configuração.');
      showSetup();
      return;
    }
    if (tipo === 'desatualizado') showWarning('O script do Google Sheets está desatualizado e não conhece o painel. Cole o Code.gs novo no Apps Script e crie uma nova versão da implantação.');
    else if (tipo === 'erro') showWarning(`A planilha respondeu com erro: ${erro.message}.`);
    else showWarning(`Sem conexão com a planilha (${erro.message}).`);
    if (guardado) render(guardado, true);
    else $('subtitulo').textContent = 'Sem dados.';
  }
}

function saveConfig() {
  const u = $('cfg-url').value.trim();
  const t = $('cfg-token').value.trim();
  if (!u || !t) { showWarning('Preencha o endereço e o token.'); return; }
  store.set('cfg', { u, t });
  $('cfg-token').value = '';
  showWarning('');
  load();
}

document.addEventListener('DOMContentLoaded', () => {
  $('atualizar').addEventListener('click', load);
  $('cfg-salvar').addEventListener('click', saveConfig);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  setInterval(load, REFRESH_MS);
  setInterval(renderAges, AGES_MS);
  const seguro = location.protocol === 'https:' || ['localhost', '127.0.0.1'].includes(location.hostname);
  if ('serviceWorker' in navigator && seguro) navigator.serviceWorker.register('sw.js').catch(() => {});
  load();
});
