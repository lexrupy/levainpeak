'use strict';

// Painel só de leitura do LevainPeak. Lê a planilha pelo Apps Script (o mesmo
// endereço que o app do PC usa para enviar) e desenha o gráfico em SVG, sem
// bibliotecas. O endereço e o token ficam só no localStorage deste aparelho.

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 60 * 1000;
const AGES_MS = 30 * 1000;
const VERSAO_PAINEL = '2026-11-13.10'; // sobe a cada mudança do painel, junto com o CACHE do sw.js
const QUADROS_MAX = 20; // imagens recebidas que o aparelho guarda para a animação
const FPS_OPCOES = [1, 2, 3, 5, 10, 15]; // quadros por segundo no seletor; o aparelho lembra a escolha

const store = {
  get(key) {
    try { return JSON.parse(localStorage.getItem(key)); } catch (erro) { return null; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch (erro) { return false; /* cheio ou bloqueado */ }
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
  'Aquecimento': 'faixa-fase-inicial',
  'Fase inicial': 'faixa-fase-inicial', // nome antigo, de gráficos já enviados
  'Crescimento': 'faixa-crescimento',
  'Pico': 'faixa-pico',
  'Decaimento': 'faixa-declinio',
  'Declínio': 'faixa-declinio', // nome antigo, de gráficos já enviados
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
  const L = 34, R = 34, T = 24, B = 26, W = 360, Hp = 250;
  // A taxa de crescimento ganha uma faixa própria sob o gráfico, no mesmo eixo de tempo.
  const taxa = Array.isArray(chart.taxa) && chart.taxa.length >= 2 ? chart.taxa : null;
  const H = Hp + (taxa ? 96 : 0);
  const pts = chart.pts || [];
  if (pts.length < 2) {
    $('grafico').textContent = 'O gráfico aparece depois de algumas medições.';
    return;
  }
  const duracao = Math.max(...pts.map((p) => p[0]), 1);
  const x = (dt) => L + (dt / duracao) * (W - L - R);
  // Folga sem rótulo abaixo do 0% e acima do 100%, como no app do PC.
  const alturaUtil = Hp - T - B;
  const chao = Hp - B - alturaUtil * 0.12;
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
    partes.push(`<rect class="${CLASSE_DA_FASE[nome] || 'faixa-fase-inicial'}" x="${esquerda.toFixed(1)}" y="${T}" width="${Math.max(0, direita - esquerda).toFixed(1)}" height="${Hp - B - T}"/>`);
    if (i > 0) partes.push(`<line class="divisor" x1="${esquerda.toFixed(1)}" y1="${T}" x2="${esquerda.toFixed(1)}" y2="${Hp - B}"/>`);
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
    if (taxa) partes.push(`<line class="grade" x1="${x(dt).toFixed(1)}" y1="${T}" x2="${x(dt).toFixed(1)}" y2="${H - B}"/>`);
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

  if (taxa) {
    const topoFaixa = Hp - B + 22;
    const baseFaixa = H - B;
    const maiorTaxa = Math.max(10, ...taxa.map((p) => p[1]));
    const menorTaxa = Math.min(0, ...taxa.map((p) => p[1]));
    const altoTaxa = Math.min(40, Math.ceil((Math.min(maiorTaxa, 40) * 1.1) / 5) * 5);
    const baixoTaxa = Math.max(-20, Math.min(0, Math.floor(menorTaxa / 5) * 5));
    const yTaxa = (v) => baseFaixa - ((Math.min(altoTaxa, Math.max(baixoTaxa, v)) - baixoTaxa) / (altoTaxa - baixoTaxa)) * (baseFaixa - topoFaixa);
    partes.push(`<text class="taxa-titulo" x="${L}" y="${topoFaixa - 7}">Taxa de crescimento (%/h, últimos 15 min)</text>`);
    [...new Set([baixoTaxa, 0, altoTaxa, (baixoTaxa + altoTaxa) / 2])].sort((a, b) => a - b).forEach((valor) => {
      const y = yTaxa(valor);
      partes.push(`<line class="grade" x1="${L}" y1="${y.toFixed(1)}" x2="${W - R}" y2="${y.toFixed(1)}"/>`);
      partes.push(`<text class="eixo" x="${L - 4}" y="${(y + 3).toFixed(1)}" text-anchor="end">${valor.toFixed(0)}</text>`);
    });
    partes.push(`<line class="taxa-zero" x1="${L}" y1="${yTaxa(0).toFixed(1)}" x2="${W - R}" y2="${yTaxa(0).toFixed(1)}"/>`);
    partes.push(`<polyline class="taxa" points="${taxa.map((p) => `${x(p[0]).toFixed(1)},${yTaxa(p[1]).toFixed(1)}`).join(' ')}"/>`);
  }

  $('grafico').innerHTML = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">${partes.join('')}</svg>`;
  $('grafico-titulo').textContent = `Volume (% do ${chart.escala}; 0% = início) · temperatura em °C`;
}

// ---------------------------------------------------------------- tela

let slotAtual = store.get('slot') || null; // qual das (até duas) sessões da planilha o painel mostra
let ultimo = null; // última resposta (para atualizar só as idades a cada 30 s)
let ultimoFoiDoCache = false;

function render(dados, doCache) {
  ultimo = dados;
  ultimoFoiDoCache = doCache;
  const aoVivo = (dados.ao_vivo && dados.ao_vivo.live) || {};
  const chart = dados.grafico && dados.grafico.chart;

  $('configurar').hidden = true;
  $('painel').hidden = false;
  desenharSessoes(dados.sessoes, dados.slot);
  $('sessao').textContent = aoVivo.sessao || (chart && chart.nome) || 'LevainPeak';

  const volume = aoVivo.volume_pct;
  $('volume').textContent = volume === undefined || volume === null ? '—' : `${Number(volume).toFixed(0)}%`;
  $('volume-nota').textContent = aoVivo.crescimento ? `${Number(aoVivo.crescimento).toFixed(2).replace('.', ',')}× o nível inicial` : 'da capacidade do pote';
  // A leitura idêntica em atualizações seguidas pode ser imagem congelada ou medição travada.
  // No aquecimento o volume parado é o normal: sem aviso.
  const aquecendo = /aquecendo|aquecimento|fase inicial/i.test(aoVivo.fase || '');
  const iguais = aquecendo ? 0 : Number(aoVivo.leituras_iguais) || 0;
  if (iguais >= 2) {
    const minutos = Math.round(((aoVivo.intervalo_s || 120) * (iguais - 1)) / 60);
    $('volume-nota').textContent += ` · ⚠ igual há ${iguais} atualizações (${minutos} min)`;
  }
  if (aoVivo.taxa) $('volume-nota').textContent += ` · ${aoVivo.taxa}`;
  $('volume-nota').classList.toggle('repetida', iguais >= 2);
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
    guardarQuadro(imagem, aoVivo.sessao || '');
    $('imagem-area').hidden = false;
    prepararQuadros();
  } else {
    $('imagem-area').hidden = true;
  }
  $('rodape').textContent = [aoVivo.perfil && `Perfil: ${aoVivo.perfil}`, aoVivo.status && `câmera: ${aoVivo.status}`].filter(Boolean).join(' · ');
  renderAges();
}

// Com duas sessões na planilha, um seletor para trocar entre elas; com uma só, nada aparece.
function desenharSessoes(sessoes, slot) {
  const caixa = $('sessoes');
  const lista = Array.isArray(sessoes) ? sessoes : [];
  caixa.hidden = lista.length < 2;
  if (lista.length < 2) { caixa.textContent = ''; return; }
  caixa.innerHTML = lista.map((s) => `<button type="button" data-slot="${s.slot}" aria-pressed="${s.slot === slot}"><span>${esc(s.nome)}</span><small>${esc(ageText(ageMinutes(s.atualizado_em)))}</small></button>`).join('');
  caixa.querySelectorAll('button').forEach((botao) => botao.addEventListener('click', () => {
    slotAtual = Number(botao.dataset.slot);
    store.set('slot', slotAtual);
    quadros = [];
    const guardado = store.get(`ultimo${slotAtual}`);
    if (guardado) render(guardado, true); // mostra já o que se tem dessa sessão, até a resposta nova chegar
    load();
  }));
}

function renderAges() {
  if (!ultimo) return;
  desenharSessoes(ultimo.sessoes, ultimo.slot);
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
  if (imagem && imagem.jpeg_b64 && quadros.length < 2) {
    const instante = Date.parse(imagem.t);
    $('imagem-legenda').textContent = Number.isNaN(instante)
      ? 'Última imagem recebida'
      : `Última imagem recebida: ${horaCurta.format(new Date(instante))} (${ageText(ageMinutes(imagem.t))})`;
  }
}

// ---------------------------------------------------------------- animação das imagens

// Cada imagem nova do pote que chega é guardada neste aparelho; a animação passa as últimas a 1 quadro
// por segundo. Só guarda o que chegou com o painel aberto: nada é pedido a mais à planilha.
let quadros = [];
let quadroAtual = 0;
let relogio = null;
let fps = FPS_OPCOES.includes(store.get('fps')) ? store.get('fps') : 1;
let pausado = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// Largura e altura de um JPEG em base64, lidas do cabeçalho (sem decodificar a imagem). null se não achar.
function tamanhoJpeg(b64) {
  try {
    const bytes = atob(b64.slice(0, 16384));
    let i = 2;
    while (i + 9 < bytes.length) {
      if (bytes.charCodeAt(i) !== 0xFF) { i++; continue; }
      const marcador = bytes.charCodeAt(i + 1);
      if (marcador >= 0xC0 && marcador <= 0xCF && ![0xC4, 0xC8, 0xCC].includes(marcador)) {
        return { h: (bytes.charCodeAt(i + 5) << 8) | bytes.charCodeAt(i + 6), w: (bytes.charCodeAt(i + 7) << 8) | bytes.charCodeAt(i + 8) };
      }
      i += 2 + ((bytes.charCodeAt(i + 2) << 8) | bytes.charCodeAt(i + 3));
    }
  } catch (erro) { /* base64 cortado: sem tamanho */ }
  return null;
}

// Os quadros da forma que a maioria tem (proporção largura/altura, com 4% de folga). Um quadro de forma
// diferente é erro ou o pote foi reposicionado: não entra na animação, e a página não muda de tamanho por causa dele.
// Se a forma nova persistir, ela vira a maioria sozinha (empate: vale a mais recente).
function quadrosDaForma(itens) {
  const parecidos = (a, b) => Math.abs(a / b - 1) <= 0.04;
  const comForma = itens.filter((item) => item.r);
  if (comForma.length < 2) return itens;
  let melhor = null;
  let votos = 0;
  comForma.forEach((item) => {
    const n = comForma.filter((outro) => parecidos(outro.r, item.r)).length;
    if (n >= votos) { melhor = item; votos = n; } // >=: no empate fica o mais recente (a lista é cronológica)
  });
  return itens.filter((item) => !item.r || parecidos(item.r, melhor.r));
}

function guardarQuadro(snapshot, sessao) {
  if (!snapshot || !snapshot.jpeg_b64 || !snapshot.t) return;
  const chave = `quadros-${sessao}`; // uma lista por sessão: trocar de sessão não apaga as imagens da outra
  let guardado = store.get(chave);
  if (!guardado || !Array.isArray(guardado.itens)) guardado = { sessao, itens: [] };
  // Quadros guardados por uma versão antiga do painel não têm a proporção: mede agora, para valerem na limpeza.
  guardado.itens.forEach((item) => {
    if (item.r === undefined) {
      const medido = tamanhoJpeg(item.b);
      item.r = medido && medido.h ? medido.w / medido.h : 0;
    }
  });
  if (!guardado.itens.some((item) => item.t === snapshot.t)) {
    const tamanho = tamanhoJpeg(snapshot.jpeg_b64);
    guardado.itens.push({ t: snapshot.t, b: snapshot.jpeg_b64, r: tamanho && tamanho.h ? tamanho.w / tamanho.h : 0 });
    guardado.itens.sort((a, b) => Date.parse(a.t) - Date.parse(b.t));
    guardado.itens = guardado.itens.slice(-QUADROS_MAX);
    while (!store.set(chave, guardado) && guardado.itens.length > 2) guardado.itens = guardado.itens.slice(Math.ceil(guardado.itens.length / 2));
  }
  store.set(chave, guardado); // também grava a proporção dos quadros que já estavam guardados
  quadros = quadrosDaForma(guardado.itens);
  const referencia = quadros.filter((item) => item.r).pop();
  if (referencia) $('imagem').style.aspectRatio = `${referencia.r}`; // a área da imagem mantém a forma: sem pulos
}

function apagarCacheDeImagens() { // não mexe na conexão (endereço e token), na sessão escolhida nem no fps
  try {
    Object.keys(localStorage).filter((k) => k.startsWith('quadros') || k.startsWith('ultimo')).forEach((k) => localStorage.removeItem(k));
  } catch (erro) { /* sem acesso ao armazenamento */ }
}

// Botão "Limpar cache" da conexão: esquece as imagens e as respostas guardadas e busca tudo de novo.
function limparCache() {
  apagarCacheDeImagens();
  quadros = [];
  quadroAtual = 0;
  ultimo = null;
  clearInterval(relogio);
  relogio = null;
  $('imagem-area').hidden = true;
  $('con-aviso').textContent = 'Cache limpo; buscando de novo.';
  load();
}

// Tira do aparelho as imagens de sessões que a planilha já não guarda.
function limparQuadrosAntigos(nomes) {
  try {
    localStorage.removeItem('quadros'); // formato antigo, uma lista só
    Object.keys(localStorage).filter((k) => k.startsWith('quadros-') && !nomes.includes(k.slice(8))).forEach((k) => localStorage.removeItem(k));
  } catch (erro) { /* sem acesso ao armazenamento */ }
}

function mostrarQuadro(indice) {
  if (!quadros.length) return;
  quadroAtual = Math.min(quadros.length - 1, Math.max(0, indice));
  const quadro = quadros[quadroAtual];
  $('imagem').src = `data:image/jpeg;base64,${quadro.b}`;
  $('quadros-slider').value = quadroAtual;
  const instante = Date.parse(quadro.t);
  $('imagem-legenda').textContent = quadros.length < 2
    ? `Última imagem recebida: ${Number.isNaN(instante) ? '' : horaCurta.format(new Date(instante))} (${ageText(ageMinutes(quadro.t))})`
    : `Quadro ${quadroAtual + 1}/${quadros.length} · ${Number.isNaN(instante) ? '' : dataHora.format(new Date(instante))}`;
}

function prepararQuadros() {
  const varios = quadros.length >= 2;
  $('quadros-ctrl').hidden = !varios;
  $('quadros-slider').max = Math.max(0, quadros.length - 1);
  $('quadros-play').textContent = pausado ? 'Reproduzir' : 'Pausar';
  $('quadros-fps').textContent = `${fps} fps`;
  clearInterval(relogio);
  relogio = null;
  if (varios && !pausado) {
    relogio = setInterval(() => mostrarQuadro((quadroAtual + 1) % quadros.length), 1000 / fps);
    mostrarQuadro(Math.min(quadroAtual, quadros.length - 1));
  } else {
    mostrarQuadro(varios && pausado ? quadroAtual : quadros.length - 1);
  }
}

function ligarQuadros() {
  $('quadros-play').addEventListener('click', () => { pausado = !pausado; prepararQuadros(); });
  $('quadros-fps').addEventListener('click', () => { // cada toque passa para o próximo (1, 2, 3, 5, 10, 15 e volta ao 1)
    fps = FPS_OPCOES[(FPS_OPCOES.indexOf(fps) + 1) % FPS_OPCOES.length];
    store.set('fps', fps);
    prepararQuadros();
  });
  // Arrastar a barra percorre os quadros e pausa. O valor é lido antes de prepararQuadros(), que devolve a
  // barra ao quadro de antes.
  $('quadros-slider').addEventListener('input', (evento) => {
    const alvo = Number(evento.target.value);
    pausado = true;
    prepararQuadros();
    mostrarQuadro(alvo);
  });
}

function showWarning(texto) {
  $('aviso').hidden = !texto;
  $('aviso').textContent = texto || '';
}

// ---------------------------------------------------------------- dados

async function load() {
  const cfg = readConfig();
  if (!cfg) { showSetup(); return; }
  const pedido = slotAtual; // a sessão que este pedido busca: se o usuário trocar enquanto espera, a resposta é descartada
  const guardado = store.get(`ultimo${pedido || ''}`);
  const snap = guardado && guardado.snapshot && guardado.snapshot.t ? guardado.snapshot.t : '';
  try {
    const url = `${cfg.u}?token=${encodeURIComponent(cfg.t)}&o=painel&snap=${encodeURIComponent(snap)}${pedido ? `&slot=${pedido}` : ''}`;
    const resposta = await fetch(url, { cache: 'no-store' });
    const dados = await resposta.json();
    if (slotAtual !== pedido) return; // trocou de sessão no meio: esta resposta é da outra
    if (!dados.ok) {
      const falha = new Error(dados.erro || 'resposta inválida');
      falha.tipo = /token/i.test(falha.message) ? 'recusado' : (/desconhecido/i.test(falha.message) ? 'desatualizado' : 'erro');
      throw falha;
    }
    // O servidor não repete a imagem que já temos: reaproveita a guardada.
    if (dados.snapshot && dados.snapshot.igual && guardado && guardado.snapshot) dados.snapshot = guardado.snapshot;
    // As imagens que a planilha guarda (as 3 últimas) e que este aparelho ainda não tem, para não deixar buracos.
    const sessao = (dados.ao_vivo && dados.ao_vivo.live && dados.ao_vivo.live.sessao) || '';
    (dados.quadros || []).forEach((quadro) => guardarQuadro(quadro, sessao));
    delete dados.quadros;
    if (dados.slot && dados.slot !== slotAtual) { slotAtual = dados.slot; store.set('slot', slotAtual); } // o escolhido sumiu: vem o padrão
    store.set(`ultimo${slotAtual || ''}`, dados);
    limparQuadrosAntigos((dados.sessoes || []).map((s) => s.nome));
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

// ---------------------------------------------------------------- conexão (ver, copiar, editar)

function copyText(texto, campo) {
  const pronto = () => { $('con-aviso').textContent = 'Copiado.'; };
  const falhou = () => {
    // Sem a área de transferência (página sem https): seleciona o campo para copiar à mão.
    if (campo) {
      const tipo = campo.type;
      campo.type = 'text';
      campo.focus();
      campo.select();
      try { if (document.execCommand('copy')) { campo.type = tipo; pronto(); return; } } catch (erro) { /* segue */ }
      campo.type = tipo;
    }
    $('con-aviso').textContent = 'Não consegui copiar sozinho: segure o campo e copie.';
  };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(texto).then(pronto, falhou);
  else falhou();
}

function openConnection() {
  const cfg = store.get('cfg') || { u: '', t: '' };
  $('con-url').value = cfg.u;
  $('con-token').value = cfg.t;
  $('con-token').type = 'password';
  $('con-mostrar').textContent = 'Mostrar';
  $('con-aviso').textContent = '';
  travarConexao(true); // abre travado: só um toque em Editar libera, para não mudar sem querer
  showVersions(cfg);
  $('conexao').showModal();
}

// Que versão do painel está rodando neste aparelho e que versão do script a planilha tem: se diferirem
// do esperado, o painel ou o script não foi atualizado.
async function showVersions(cfg) {
  const linha = $('con-versao');
  const painel = `Painel ${VERSAO_PAINEL}`;
  linha.textContent = `${painel} · script da planilha: consultando…`;
  if (!cfg.u || !cfg.t) { linha.textContent = `${painel} · script da planilha: sem endereço`; return; }
  try {
    const resposta = await fetch(`${cfg.u}?token=${encodeURIComponent(cfg.t)}&o=versao`, { cache: 'no-store' });
    const dados = await resposta.json();
    linha.textContent = `${painel} · script da planilha: ${dados.ok && dados.versao ? dados.versao : 'sem versão (desatualizado)'}`;
  } catch (erro) {
    linha.textContent = `${painel} · script da planilha: sem conexão`;
  }
}

// Os campos da conexão ficam só para leitura (dá para copiar) até o toque em Editar.
function travarConexao(travar) {
  $('con-url').readOnly = travar;
  $('con-token').readOnly = travar;
  $('con-editar').hidden = !travar;
  $('con-salvar').hidden = travar;
}

function closeConnection() {
  $('con-token').value = '';  // o token não fica no campo depois de fechar
  $('con-token').type = 'password';
  $('conexao').close();
}

function setupConnection() {
  $('conexao-abrir').addEventListener('click', openConnection);
  $('con-cancelar').addEventListener('click', closeConnection);
  $('conexao').addEventListener('cancel', () => { $('con-token').value = ''; });
  $('con-mostrar').addEventListener('click', () => {
    const mostrar = $('con-token').type === 'password';
    $('con-token').type = mostrar ? 'text' : 'password';
    $('con-mostrar').textContent = mostrar ? 'Ocultar' : 'Mostrar';
  });
  document.querySelectorAll('[data-copiar]').forEach((botao) => {
    botao.addEventListener('click', () => {
      const campo = $(botao.dataset.copiar);
      copyText(campo.value, campo);
    });
  });
  $('con-link').addEventListener('click', () => {
    const u = $('con-url').value.trim();
    const t = $('con-token').value.trim();
    if (!u || !t) { $('con-aviso').textContent = 'Preencha o endereço e o token primeiro.'; return; }
    copyText(`${location.origin}${location.pathname}#u=${encodeURIComponent(u)}&t=${encodeURIComponent(t)}`, null);
  });
  $('con-limpar-cache').addEventListener('click', limparCache);
  $('con-editar').addEventListener('click', () => { travarConexao(false); $('con-url').focus(); });
  $('con-salvar').addEventListener('click', () => {
    const u = $('con-url').value.trim();
    const t = $('con-token').value.trim();
    if (!u || !t) { $('con-aviso').textContent = 'Preencha o endereço e o token.'; return; }
    store.set('cfg', { u, t });
    closeConnection();
    showWarning('');
    load();
  });
}

document.addEventListener('DOMContentLoaded', () => {
  setupConnection();
  $('atualizar').addEventListener('click', load);
  ligarQuadros();
  $('cfg-salvar').addEventListener('click', saveConfig);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  setInterval(load, REFRESH_MS);
  setInterval(renderAges, AGES_MS);
  const seguro = location.protocol === 'https:' || ['localhost', '127.0.0.1'].includes(location.hostname);
  if ('serviceWorker' in navigator && seguro) navigator.serviceWorker.register('sw.js').catch(() => {});
  load();
});
