/**
 * LevainPeak — receptor de dados no Google Sheets (Google Apps Script).
 *
 * O app do PC envia um pacote por ciclo (POST); o celular lê (GET). O token fica
 * nas "Propriedades do script" (TOKEN), nunca neste código.
 *
 * POST (corpo JSON):
 *   { "token": "...", "session": "Bradok", "sent_at": "2026-10-31T00:00:00-03:00",
 *     "live":   { "volume_pct": 42.3, "fase": "Crescendo", ... },          (opcional)
 *     "points": [ { "t": ISO, "v": volume 0-1, "c": temperatura, "p": fase } ],  (opcional)
 *     "chart":  { ... série do gráfico, já na escala do app ... },          (opcional)
 *     "snapshot": { "t": ISO, "jpeg_b64": "..." } }                        (opcional)
 *
 * A planilha guarda até NUM_SLOTS (2) sessões ao mesmo tempo, cada uma pelo seu nome ("session"). Uma sessão nova
 * ocupa o espaço livre; sem espaço, sai a que está há mais tempo sem enviar (e as paradas há mais de
 * HORAS_OCIOSA h saem sozinhas). O espaço 1 usa as abas serie, ao_vivo...; o 2 usa serie_2, ao_vivo_2...;
 * a aba sessoes diz qual nome está em cada espaço.
 *
 * POST de limpeza: { "token": "...", "acao": "limpar", "session": "Bradok" } zera só essa sessão;
 * sem "session" zera todas (mantém os cabeçalhos).
 *
 * A aba quadros guarda as últimas 10 imagens recebidas (uma por envio); o painel pede as mais novas que a sua.
 *
 * GET:  ?token=...&o=painel[&slot=1|2][&snap=<t do snapshot que você já tem>]
 *       | ao_vivo | serie&limite=1500 | snapshot | sessoes      (slot: sem ele, a sessão que enviou por último)
 */

/** Sobe a cada mudança deste arquivo: confira na implantação com ?o=versao (ou rodando versao() no editor). */
const VERSAO = '2026-11-13.1 duas-sessoes';

const ABA_SERIE = 'serie';
const ABA_AO_VIVO = 'ao_vivo';
const ABA_SNAPSHOT = 'snapshot';
const ABA_GRAFICO = 'grafico';
const ABA_QUADROS = 'quadros';
const ABA_SESSOES = 'sessoes';
const CABECALHO_SESSOES = ['slot', 'sessao', 'atualizado_em'];
const NUM_SLOTS = 2;              // sessões guardadas ao mesmo tempo
const HORAS_OCIOSA = 72;          // uma sessão sem enviar por tanto tempo libera o espaço dela
const MAX_QUADROS = 10;           // imagens do pote que a planilha mantém; a mais antiga sai
const CABECALHO_SERIE = ['timestamp', 'volume_fracao', 'temperatura_c', 'fase', 'sessao'];
const MAX_LINHAS_SERIE = 20000;   // as mais antigas saem quando passa disso
const MAX_CHARS_CELULA = 49000;   // o limite do Sheets é 50.000 caracteres por célula

/** Rode uma vez: cria as abas e pede a autorização do Google. */
function configurar() {
  const token = PropertiesService.getScriptProperties().getProperty('TOKEN');
  if (!token || token.length < 16) {
    throw new Error('Defina a propriedade de script TOKEN com pelo menos 16 caracteres ' +
        '(Configurações do projeto > Propriedades do script).');
  }
  aba_(ABA_SERIE, CABECALHO_SERIE);
  aba_(ABA_AO_VIVO, ['campo', 'valor']);
  aba_(ABA_SNAPSHOT, ['campo', 'valor']);
  aba_(ABA_GRAFICO, ['campo', 'valor']);
  aba_(ABA_SESSOES, CABECALHO_SESSOES);
  Logger.log('Pronto. Agora implante como app da web.');
}

/** Para rodar à mão no editor: apaga todos os dados e deixa só os cabeçalhos. */
function limparTudo() {
  Logger.log(JSON.stringify(limparTodas_()));
}

function doPost(e) {
  let corpo;
  try {
    corpo = JSON.parse(e.postData.contents);
  } catch (erro) {
    return resposta_({ ok: false, erro: 'JSON inválido' });
  }
  if (!tokenValido_(corpo && corpo.token)) return resposta_({ ok: false, erro: 'token inválido' });

  const trava = LockService.getScriptLock();
  trava.waitLock(20000);
  try {
    if (corpo.acao === 'limpar') {
      const nome = String(corpo.session || '');
      const dela = nome ? lerSessoes_().filter(function (x) { return x.nome === nome; })[0] : null;
      return resposta_({ ok: true, limpou: dela ? limpar_(dela.slot) : (nome ? {} : limparTodas_()) });
    }
    const recebido = {};
    const sessao = String(corpo.session || '');
    const escolha = escolherSlot_(sessao);
    const slot = escolha.slot;
    if (escolha.nova) recebido.sessao_nova = true;
    recebido.slot = slot;
    gravarSessao_(slot, sessao || escolha.nome, new Date().toISOString());
    if (Array.isArray(corpo.points) && corpo.points.length) {
      recebido.pontos = acrescentarPontos_(corpo.points, sessao, slot);
    }
    if (corpo.live && typeof corpo.live === 'object') {
      gravarAoVivo_(corpo.live, corpo.sent_at, slot);
      recebido.ao_vivo = true;
    }
    if (corpo.chart && typeof corpo.chart === 'object') {
      recebido.grafico = gravarGrafico_(corpo.chart, corpo.sent_at, slot);
    }
    if (corpo.snapshot && corpo.snapshot.jpeg_b64) {
      recebido.snapshot = gravarSnapshot_(corpo.snapshot, slot);
    }
    return resposta_({ ok: true, recebido: recebido });
  } finally {
    trava.releaseLock();
  }
}

function doGet(e) {
  const p = (e && e.parameter) || {};
  if (!tokenValido_(p.token)) return resposta_({ ok: false, erro: 'token inválido' });
  const o = p.o || 'ao_vivo';
  const slot = slotPedido_(p.slot);
  if (o === 'painel') return resposta_(lerPainel_(String(p.snap || ''), slot));
  if (o === 'ao_vivo') return resposta_(lerAoVivo_(slot));
  if (o === 'serie') return resposta_(lerSerie_(Math.min(Number(p.limite) || 1500, 5000), slot));
  if (o === 'snapshot') return resposta_(lerSnapshot_(slot));
  if (o === 'sessoes') return resposta_({ ok: true, sessoes: lerSessoes_() });
  if (o === 'versao') return resposta_({ ok: true, versao: VERSAO });
  return resposta_({ ok: false, erro: 'parâmetro o desconhecido' });
}

/** Rode no editor para ver a versão deste código; a implantação responde o mesmo em ?o=versao. */
function versao() {
  return VERSAO;
}

// ---------------------------------------------------------------- escrita

function acrescentarPontos_(pontos, sessao, slot) {
  const aba = aba_(nomeAba_(ABA_SERIE, slot), CABECALHO_SERIE);
  const ultima = aba.getLastRow();
  const ultimoTs = ultima > 1 ? String(aba.getRange(ultima, 1).getValue()) : '';
  // Só o que for mais novo que a última linha: um reenvio não duplica nada.
  const novas = pontos
      .filter(function (p) { return p && p.t && String(p.t) > ultimoTs; })
      .sort(function (a, b) { return String(a.t) < String(b.t) ? -1 : 1; });
  if (!novas.length) return 0;
  const linhas = novas.map(function (p) {
    return [String(p.t), numero_(p.v), numero_(p.c), String(p.p || ''), sessao];
  });
  aba.getRange(ultima + 1, 1, linhas.length, CABECALHO_SERIE.length).setValues(linhas);
  const excesso = aba.getLastRow() - 1 - MAX_LINHAS_SERIE;
  if (excesso > 0) aba.deleteRows(2, excesso);
  return linhas.length;
}

function gravarAoVivo_(live, enviadoEm, slot) {
  const aba = aba_(nomeAba_(ABA_AO_VIVO, slot), ['campo', 'valor']);
  const quando = String(enviadoEm || new Date().toISOString());
  // Linha 3 guarda o JSON exato; as demais são só para olhar a planilha no celular.
  const linhas = [['atualizado_em', quando], ['json', JSON.stringify(live)]];
  Object.keys(live).forEach(function (chave) {
    const valor = live[chave];
    linhas.push([String(chave), typeof valor === 'object' ? JSON.stringify(valor) : String(valor)]);
  });
  const anteriores = aba.getLastRow() - 1;
  if (anteriores > 0) aba.getRange(2, 1, anteriores, 2).clearContent();
  aba.getRange(2, 1, linhas.length, 2).setValues(linhas);
}

function gravarGrafico_(grafico, enviadoEm, slot) {
  const texto = JSON.stringify(grafico);
  if (texto.length > MAX_CHARS_CELULA) return 'grande demais (' + texto.length + ' caracteres)';
  const aba = aba_(nomeAba_(ABA_GRAFICO, slot), ['campo', 'valor']);
  aba.getRange(2, 1, 2, 2).setValues([
    ['atualizado_em', String(enviadoEm || new Date().toISOString())],
    ['json', texto],
  ]);
  return 'ok';
}

function gravarSnapshot_(snapshot, slot) {
  const imagem = String(snapshot.jpeg_b64);
  if (imagem.length > MAX_CHARS_CELULA) return 'grande demais (' + imagem.length + ' caracteres)';
  const aba = aba_(nomeAba_(ABA_SNAPSHOT, slot), ['campo', 'valor']);
  aba.getRange(2, 1, 2, 2).setValues([
    ['atualizado_em', String(snapshot.t || new Date().toISOString())],
    ['jpeg_base64', imagem],
  ]);
  guardarQuadro_(String(snapshot.t || new Date().toISOString()), imagem, slot);
  return 'ok';
}

/** Acrescenta a imagem à aba quadros e mantém só as MAX_QUADROS mais novas. */
function guardarQuadro_(t, imagem, slot) {
  const aba = aba_(nomeAba_(ABA_QUADROS, slot), ['t', 'jpeg_base64']);
  const ultima = aba.getLastRow();
  if (ultima >= 2 && String(aba.getRange(ultima, 1).getValue()) === t) return;
  aba.appendRow([t, imagem]);
  const dados = aba.getLastRow() - 1;
  if (dados > MAX_QUADROS) aba.deleteRows(2, dados - MAX_QUADROS);
}

/** As imagens guardadas mais novas que "desde" (todas, se vazio), da mais antiga para a mais nova. */
function lerQuadros_(desde, slot) {
  const aba = aba_(nomeAba_(ABA_QUADROS, slot), ['t', 'jpeg_base64']);
  const ultima = aba.getLastRow();
  if (ultima < 2) return [];
  const corte = desde ? Date.parse(desde) : -Infinity;
  return aba.getRange(2, 1, ultima - 1, 2).getValues()
    .map(function (l) { return { t: String(l[0]), jpeg_b64: String(l[1]) }; })
    .filter(function (q) { return q.jpeg_b64 && !(Date.parse(q.t) <= corte); });
}

/** Zera as abas de um espaço e devolve quantas linhas de dados cada uma tinha. */
function limpar_(slot) {
  const abas = [[ABA_SERIE, CABECALHO_SERIE], [ABA_AO_VIVO, ['campo', 'valor']],
    [ABA_GRAFICO, ['campo', 'valor']], [ABA_SNAPSHOT, ['campo', 'valor']], [ABA_QUADROS, ['t', 'jpeg_base64']]];
  const limpou = {};
  abas.forEach(function (par) {
    const aba = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(nomeAba_(par[0], slot));
    if (!aba) { limpou[par[0]] = 0; return; }
    const linhas = Math.max(0, aba.getLastRow() - 1);
    if (linhas > 0) aba.getRange(2, 1, linhas, par[1].length).clearContent();
    limpou[par[0]] = linhas;
  });
  apagarSessao_(slot);
  return limpou;
}

/** Zera todos os espaços; as contagens somam. */
function limparTodas_() {
  const total = {};
  for (let slot = 1; slot <= NUM_SLOTS; slot++) {
    const parcial = limpar_(slot);
    Object.keys(parcial).forEach(function (aba) { total[aba] = (total[aba] || 0) + parcial[aba]; });
  }
  return total;
}

// ---------------------------------------------------------------- sessões (os espaços)

function nomeAba_(base, slot) {
  return slot > 1 ? base + '_' + slot : base;
}

/** As sessões guardadas: [{slot, nome, atualizado_em}], só os espaços ocupados. */
function lerSessoes_() {
  const aba = aba_(ABA_SESSOES, CABECALHO_SESSOES);
  const ultima = aba.getLastRow();
  if (ultima < 2) return [];
  return aba.getRange(2, 1, ultima - 1, 3).getValues()
    .map(function (l) { return { slot: Number(l[0]), nome: String(l[1]), atualizado_em: String(l[2]) }; })
    .filter(function (x) { return x.slot >= 1 && x.slot <= NUM_SLOTS && x.nome !== ''; });
}

function instante_(texto) {
  const t = Date.parse(texto);
  return isNaN(t) ? -Infinity : t;
}

function gravarSessao_(slot, nome, quando) {
  const aba = aba_(ABA_SESSOES, CABECALHO_SESSOES);
  const ultima = aba.getLastRow();
  let linha = ultima + 1;
  if (ultima >= 2) {
    const slots = aba.getRange(2, 1, ultima - 1, 1).getValues();
    for (let i = 0; i < slots.length; i++) if (Number(slots[i][0]) === slot) { linha = i + 2; break; }
  }
  aba.getRange(linha, 1, 1, 3).setValues([[slot, nome, quando]]);
}

function apagarSessao_(slot) {
  const aba = aba_(ABA_SESSOES, CABECALHO_SESSOES);
  const ultima = aba.getLastRow();
  if (ultima < 2) return;
  const slots = aba.getRange(2, 1, ultima - 1, 1).getValues();
  for (let i = 0; i < slots.length; i++) {
    if (Number(slots[i][0]) === slot) aba.getRange(i + 2, 2, 1, 2).clearContent();
  }
}

function maisRecente_(sessoes) {
  return sessoes.slice().sort(function (a, b) { return instante_(b.atualizado_em) - instante_(a.atualizado_em); })[0];
}

/**
 * Em que espaço a sessão "sessao" fica: o dela, se já tem; senão um livre; senão o da que está há mais tempo
 * sem enviar (que é zerado). Sem nome, vai para a sessão que enviou por último. Antes disso, as sessões
 * paradas há mais de HORAS_OCIOSA h saem (menos a que está enviando agora).
 */
function escolherSlot_(sessao) {
  const limite = Date.now() - HORAS_OCIOSA * 3600 * 1000;
  let sessoes = lerSessoes_().filter(function (x) {
    if (x.nome !== sessao && instante_(x.atualizado_em) < limite) { limpar_(x.slot); return false; }
    return true;
  });
  if (!sessao) {
    const recente = sessoes.length ? maisRecente_(sessoes) : null;
    return recente ? { slot: recente.slot, nome: recente.nome, nova: false } : { slot: 1, nome: '', nova: false };
  }
  const dela = sessoes.filter(function (x) { return x.nome === sessao; })[0];
  if (dela) return { slot: dela.slot, nome: sessao, nova: false };
  const ocupados = sessoes.map(function (x) { return x.slot; });
  for (let slot = 1; slot <= NUM_SLOTS; slot++) {
    if (ocupados.indexOf(slot) < 0) return { slot: slot, nome: sessao, nova: true };
  }
  const velha = sessoes.slice().sort(function (a, b) { return instante_(a.atualizado_em) - instante_(b.atualizado_em); })[0];
  limpar_(velha.slot);
  return { slot: velha.slot, nome: sessao, nova: true };
}

/** O espaço pedido no GET (?slot=): se não existe ou está vazio, o da sessão que enviou por último. */
function slotPedido_(valor) {
  const sessoes = lerSessoes_();
  const n = Number(valor);
  if (sessoes.some(function (x) { return x.slot === n; })) return n;
  return sessoes.length ? maisRecente_(sessoes).slot : 1;
}

// ---------------------------------------------------------------- leitura

function lerAoVivo_(slot) {
  const aba = aba_(nomeAba_(ABA_AO_VIVO, slot), ['campo', 'valor']);
  if (aba.getLastRow() < 3) return { ok: true, vazio: true };
  const quando = String(aba.getRange(2, 2).getValue());
  let live = {};
  try {
    live = JSON.parse(String(aba.getRange(3, 2).getValue()));
  } catch (erro) {
    live = {};
  }
  return { ok: true, atualizado_em: quando, live: live };
}

function lerGrafico_(slot) {
  const aba = aba_(nomeAba_(ABA_GRAFICO, slot), ['campo', 'valor']);
  if (aba.getLastRow() < 3) return { ok: true, vazio: true };
  let chart = null;
  try {
    chart = JSON.parse(String(aba.getRange(3, 2).getValue()));
  } catch (erro) {
    chart = null;
  }
  return { ok: true, atualizado_em: String(aba.getRange(2, 2).getValue()), chart: chart };
}

/** Tudo o que o painel mostra, numa chamada só. O snapshot só vai se mudou. */
function lerPainel_(snapDoCliente, slot) {
  const snapshot = lerSnapshot_(slot);
  if (!snapshot.vazio && snapDoCliente && snapDoCliente === snapshot.t) {
    snapshot.jpeg_b64 = null;
    snapshot.igual = true;
  }
  return {
    ok: true, slot: slot, sessoes: lerSessoes_(), ao_vivo: lerAoVivo_(slot), grafico: lerGrafico_(slot),
    snapshot: snapshot, quadros: lerQuadros_(snapDoCliente, slot),
  };
}

function lerSerie_(limite, slot) {
  const aba = aba_(nomeAba_(ABA_SERIE, slot), CABECALHO_SERIE);
  const ultima = aba.getLastRow();
  if (ultima < 2) return { ok: true, pontos: [] };
  const n = Math.min(limite, ultima - 1);
  const dados = aba.getRange(ultima - n + 1, 1, n, CABECALHO_SERIE.length).getValues();
  return {
    ok: true,
    pontos: dados.map(function (l) {
      return { t: String(l[0]), v: vazioParaNulo_(l[1]), c: vazioParaNulo_(l[2]), p: String(l[3]), s: String(l[4]) };
    }),
  };
}

function lerSnapshot_(slot) {
  const aba = aba_(nomeAba_(ABA_SNAPSHOT, slot), ['campo', 'valor']);
  if (aba.getLastRow() < 3) return { ok: true, vazio: true };
  return {
    ok: true,
    t: String(aba.getRange(2, 2).getValue()),
    jpeg_b64: String(aba.getRange(3, 2).getValue()),
  };
}

// ---------------------------------------------------------------- apoio

function aba_(nome, cabecalho) {
  const planilha = SpreadsheetApp.getActiveSpreadsheet();
  let aba = planilha.getSheetByName(nome);
  if (!aba) {
    aba = planilha.insertSheet(nome);
    // Texto puro: o Sheets não converte "2026-10-31T00:00:00-03:00" em data nem trata "=..." como fórmula.
    const base = nome.replace(/_\d+$/, '');
    aba.getRange(base === ABA_SESSOES ? 'A:C' : (base === ABA_SERIE || base === ABA_QUADROS ? 'A:A' : 'B:B')).setNumberFormat('@');
  }
  if (aba.getLastRow() === 0 && cabecalho) aba.appendRow(cabecalho);
  return aba;
}

function tokenValido_(token) {
  const esperado = PropertiesService.getScriptProperties().getProperty('TOKEN');
  return !!esperado && esperado.length >= 16 && typeof token === 'string' && token === esperado;
}

function numero_(x) {
  if (x === null || x === undefined || x === '') return '';
  const n = Number(x);
  return isNaN(n) ? '' : n;
}

function vazioParaNulo_(x) {
  return x === '' ? null : x;
}

function resposta_(objeto) {
  return ContentService.createTextOutput(JSON.stringify(objeto))
      .setMimeType(ContentService.MimeType.JSON);
}
