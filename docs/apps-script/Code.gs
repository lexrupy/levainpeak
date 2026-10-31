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
 * Se "session" for diferente da última guardada na aba serie, o script zera tudo e começa a nova.
 *
 * POST de limpeza: { "token": "...", "acao": "limpar" } zera as quatro abas (mantém os cabeçalhos).
 *
 * GET:  ?token=...&o=painel[&snap=<t do snapshot que você já tem>]
 *       | ao_vivo | serie&limite=1500 | snapshot
 */

const ABA_SERIE = 'serie';
const ABA_AO_VIVO = 'ao_vivo';
const ABA_SNAPSHOT = 'snapshot';
const ABA_GRAFICO = 'grafico';
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
  Logger.log('Pronto. Agora implante como app da web.');
}

/** Para rodar à mão no editor: apaga todos os dados e deixa só os cabeçalhos. */
function limparTudo() {
  Logger.log(JSON.stringify(limpar_()));
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
    if (corpo.acao === 'limpar') return resposta_({ ok: true, limpou: limpar_() });
    const recebido = {};
    // Outra sessão: a planilha guarda uma só; zera a antiga e começa a nova.
    if (sessaoMudou_(String(corpo.session || ''))) {
      limpar_();
      recebido.sessao_nova = true;
    }
    if (Array.isArray(corpo.points) && corpo.points.length) {
      recebido.pontos = acrescentarPontos_(corpo.points, String(corpo.session || ''));
    }
    if (corpo.live && typeof corpo.live === 'object') {
      gravarAoVivo_(corpo.live, corpo.sent_at);
      recebido.ao_vivo = true;
    }
    if (corpo.chart && typeof corpo.chart === 'object') {
      recebido.grafico = gravarGrafico_(corpo.chart, corpo.sent_at);
    }
    if (corpo.snapshot && corpo.snapshot.jpeg_b64) {
      recebido.snapshot = gravarSnapshot_(corpo.snapshot);
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
  if (o === 'painel') return resposta_(lerPainel_(String(p.snap || '')));
  if (o === 'ao_vivo') return resposta_(lerAoVivo_());
  if (o === 'serie') return resposta_(lerSerie_(Math.min(Number(p.limite) || 1500, 5000)));
  if (o === 'snapshot') return resposta_(lerSnapshot_());
  return resposta_({ ok: false, erro: 'parâmetro o desconhecido' });
}

// ---------------------------------------------------------------- escrita

function acrescentarPontos_(pontos, sessao) {
  const aba = aba_(ABA_SERIE, CABECALHO_SERIE);
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

function gravarAoVivo_(live, enviadoEm) {
  const aba = aba_(ABA_AO_VIVO, ['campo', 'valor']);
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

function gravarGrafico_(grafico, enviadoEm) {
  const texto = JSON.stringify(grafico);
  if (texto.length > MAX_CHARS_CELULA) return 'grande demais (' + texto.length + ' caracteres)';
  const aba = aba_(ABA_GRAFICO, ['campo', 'valor']);
  aba.getRange(2, 1, 2, 2).setValues([
    ['atualizado_em', String(enviadoEm || new Date().toISOString())],
    ['json', texto],
  ]);
  return 'ok';
}

function gravarSnapshot_(snapshot) {
  const imagem = String(snapshot.jpeg_b64);
  if (imagem.length > MAX_CHARS_CELULA) return 'grande demais (' + imagem.length + ' caracteres)';
  const aba = aba_(ABA_SNAPSHOT, ['campo', 'valor']);
  aba.getRange(2, 1, 2, 2).setValues([
    ['atualizado_em', String(snapshot.t || new Date().toISOString())],
    ['jpeg_base64', imagem],
  ]);
  return 'ok';
}

/** True quando a série guardada é de uma sessão diferente da que chegou. */
function sessaoMudou_(sessao) {
  if (!sessao) return false;
  const aba = aba_(ABA_SERIE, CABECALHO_SERIE);
  const ultima = aba.getLastRow();
  if (ultima < 2) return false;
  const guardada = String(aba.getRange(ultima, 5).getValue());
  return guardada !== '' && guardada !== sessao;
}

/** Zera as abas e devolve quantas linhas de dados cada uma tinha. */
function limpar_() {
  const abas = [[ABA_SERIE, CABECALHO_SERIE], [ABA_AO_VIVO, ['campo', 'valor']],
    [ABA_GRAFICO, ['campo', 'valor']], [ABA_SNAPSHOT, ['campo', 'valor']]];
  const limpou = {};
  abas.forEach(function (par) {
    const aba = aba_(par[0], par[1]);
    const linhas = Math.max(0, aba.getLastRow() - 1);
    if (linhas > 0) aba.getRange(2, 1, linhas, par[1].length).clearContent();
    limpou[par[0]] = linhas;
  });
  return limpou;
}

// ---------------------------------------------------------------- leitura

function lerAoVivo_() {
  const aba = aba_(ABA_AO_VIVO, ['campo', 'valor']);
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

function lerGrafico_() {
  const aba = aba_(ABA_GRAFICO, ['campo', 'valor']);
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
function lerPainel_(snapDoCliente) {
  const snapshot = lerSnapshot_();
  if (!snapshot.vazio && snapDoCliente && snapDoCliente === snapshot.t) {
    snapshot.jpeg_b64 = null;
    snapshot.igual = true;
  }
  return { ok: true, ao_vivo: lerAoVivo_(), grafico: lerGrafico_(), snapshot: snapshot };
}

function lerSerie_(limite) {
  const aba = aba_(ABA_SERIE, CABECALHO_SERIE);
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

function lerSnapshot_() {
  const aba = aba_(ABA_SNAPSHOT, ['campo', 'valor']);
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
    aba.getRange(nome === ABA_SERIE ? 'A:A' : 'B:B').setNumberFormat('@');
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
