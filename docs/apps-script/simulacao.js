// Simula o Google Sheets em memória e exercita o Code.gs.
// Uso: node docs/apps-script/simulacao.js   (rode de novo sempre que o script mudar)
const fs = require('fs'), vm = require('vm'), path = require('path'), assert = require('assert');

class Sheet {
  constructor() { this.rows = []; }
  getLastRow() { let n = this.rows.length; while (n > 0 && this.rows[n - 1].every(v => v === '' || v === undefined)) n--; return n; }
  appendRow(a) { this.rows.push(a.slice()); }
  getRange(r, c, nr = 1, nc = 1) {
    if (typeof r === 'string') return { setNumberFormat() {} };
    const s = this;
    return {
      setNumberFormat() {},
      setValues(v) { v.forEach((row, i) => { const t = (s.rows[r - 1 + i] = s.rows[r - 1 + i] || []); row.forEach((x, j) => t[c - 1 + j] = x); }); },
      getValue() { return ((s.rows[r - 1] || [])[c - 1]) ?? ''; },
      getValues() { const out = []; for (let i = 0; i < nr; i++) { const row = []; for (let j = 0; j < nc; j++) row.push(((s.rows[r - 1 + i] || [])[c - 1 + j]) ?? ''); out.push(row); } return out; },
      clearContent() { for (let i = 0; i < nr; i++) for (let j = 0; j < nc; j++) if (s.rows[r - 1 + i]) s.rows[r - 1 + i][c - 1 + j] = ''; },
    };
  }
  deleteRows(start, count) { this.rows.splice(start - 1, count); }
}

const sheets = {}, props = { TOKEN: 'segredo-bem-longo-123456' };
const ctx = {
  SpreadsheetApp: { getActiveSpreadsheet: () => ({ getSheetByName: n => sheets[n] || null, insertSheet: n => (sheets[n] = new Sheet()) }) },
  PropertiesService: { getScriptProperties: () => ({ getProperty: k => props[k] ?? null }) },
  LockService: { getScriptLock: () => ({ waitLock() {}, releaseLock() {} }) },
  ContentService: { MimeType: { JSON: 'json' }, createTextOutput: t => ({ t, setMimeType() { return this; }, getContent() { return this.t; } }) },
  Logger: { log() {} }, console,
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, 'Code.gs'), 'utf8') + '\nthis.__api = { doPost, doGet, configurar, limparTudo };', ctx);
const { doPost, doGet, configurar } = ctx.__api;
const post = o => JSON.parse(doPost({ postData: { contents: typeof o === 'string' ? o : JSON.stringify(o) } }).getContent());
const get = q => JSON.parse(doGet({ parameter: q }).getContent());
const T = props.TOKEN;
const pt = (i, v) => ({ t: `2026-10-31T00:${String(i).padStart(2, '0')}:00-03:00`, v, c: 21.8, p: 'Crescendo' });
const chart = { nome: 'Bradok', t0: '2026-10-31T00:00:00-03:00', escala: 'dobro', pts: [[0, 0, 22], [600, 40.5, 21.9]], fases: [['Crescimento', 0, 600]] };
let n = 0;
const ok = (nome, f) => { f(); n++; console.log('  ok  ' + nome); };

configurar();
ok('GET sem token é recusado', () => assert.strictEqual(get({}).ok, false));
ok('POST com token errado é recusado', () => assert.strictEqual(post({ token: 'x' }).erro, 'token inválido'));
ok('POST com JSON inválido não quebra', () => assert.strictEqual(post('{oops').erro, 'JSON inválido'));
ok('token curto demais nunca vale', () => { props.TOKEN = 'curto'; assert.strictEqual(post({ token: 'curto' }).ok, false); props.TOKEN = T; });
ok('a versão do código é consultável pela implantação', () => { const r = get({ token: T, o: 'versao' }); assert.ok(r.ok && /^\d{4}-\d{2}-\d{2}\.\d+/.test(r.versao)); assert.strictEqual(get({ token: 'errado', o: 'versao' }).ok, false); });
ok('painel vazio responde sem erro', () => { const r = get({ token: T, o: 'painel' }); assert.ok(r.ok && r.ao_vivo.vazio && r.grafico.vazio && r.snapshot.vazio); });
ok('POST completo grava pontos, ao vivo, gráfico e snapshot', () => {
  const r = post({ token: T, session: 'Bradok', sent_at: '2026-10-31T00:05:00-03:00',
    live: { volume_pct: 42.3, fase: 'Crescendo' }, points: [pt(1, 0.31), pt(2, 0.32), pt(3, 0.33)], chart, snapshot: { t: '2026-10-31T00:05:00-03:00', jpeg_b64: 'QUJD' } });
  assert.deepStrictEqual(r, { ok: true, recebido: { sessao_nova: true, slot: 1, pontos: 3, ao_vivo: true, grafico: 'ok', snapshot: 'ok' } });
});
ok('reenviar os mesmos pontos não duplica', () => assert.strictEqual(post({ token: T, points: [pt(1, 0.31), pt(3, 0.33)] }).recebido.pontos, 0));
ok('pontos fora de ordem entram em ordem e só os novos', () => {
  assert.strictEqual(post({ token: T, session: 'Bradok', points: [pt(6, 0.36), pt(4, 0.34), pt(2, 0.32)] }).recebido.pontos, 2);
  assert.deepStrictEqual(get({ token: T, o: 'serie' }).pontos.map(p => p.t.slice(11, 16)), ['00:01', '00:02', '00:03', '00:04', '00:06']);
});
ok('temperatura ausente vira null', () => { post({ token: T, points: [{ t: '2026-10-31T00:07:00-03:00', v: 0.37, p: 'x' }] }); assert.strictEqual(get({ token: T, o: 'serie', limite: 1 }).pontos[0].c, null); });
ok('painel junta ao vivo, gráfico e snapshot', () => {
  const r = get({ token: T, o: 'painel' });
  assert.strictEqual(r.ao_vivo.live.volume_pct, 42.3);
  assert.deepStrictEqual(r.grafico.chart, chart);
  assert.strictEqual(r.snapshot.jpeg_b64, 'QUJD');
});
ok('painel não repete o snapshot que o cliente já tem', () => {
  const igual = get({ token: T, o: 'painel', snap: '2026-10-31T00:05:00-03:00' }).snapshot;
  assert.ok(igual.igual && igual.jpeg_b64 === null && igual.t === '2026-10-31T00:05:00-03:00');
  assert.strictEqual(get({ token: T, o: 'painel', snap: 'outro' }).snapshot.jpeg_b64, 'QUJD');
});
ok('ao vivo substitui o anterior, sem sobras', () => { post({ token: T, live: { so: 1 } }); assert.deepStrictEqual(Object.keys(get({ token: T, o: 'ao_vivo' }).live), ['so']); });
ok('a planilha mantém as 10 últimas imagens, uma por envio, e o painel pede só as mais novas', () => {
  const hora = (min) => `2026-10-31T01:${String(min).padStart(2, '0')}:00-03:00`;
  const quadro = (min, b64) => post({ token: T, snapshot: { t: hora(min), jpeg_b64: b64 } });
  for (let m = 1; m <= 12; m++) quadro(m, 'Q' + m);
  quadro(12, 'Q12'); // o mesmo envio repetido não duplica
  const todas = get({ token: T, o: 'painel' }).quadros;
  assert.deepStrictEqual(todas.map(q => q.jpeg_b64), ['Q3', 'Q4', 'Q5', 'Q6', 'Q7', 'Q8', 'Q9', 'Q10', 'Q11', 'Q12']); // as duas mais antigas saíram
  const novas = get({ token: T, o: 'painel', snap: hora(10) }).quadros;
  assert.deepStrictEqual(novas.map(q => q.jpeg_b64), ['Q11', 'Q12']);
  assert.deepStrictEqual(get({ token: T, o: 'painel', snap: hora(12) }).quadros, []);
  assert.strictEqual(get({ token: T, o: 'painel' }).snapshot.jpeg_b64, 'Q12'); // o campo antigo segue valendo
});
ok('snapshot grande demais é recusado e o antigo fica', () => { assert.match(post({ token: T, snapshot: { jpeg_b64: 'A'.repeat(50000) } }).recebido.snapshot, /grande demais/); assert.strictEqual(get({ token: T, o: 'snapshot' }).jpeg_b64, 'Q12'); });
ok('gráfico grande demais é recusado e o antigo fica', () => { assert.match(post({ token: T, chart: { lixo: 'A'.repeat(50000) } }).recebido.grafico, /grande demais/); assert.deepStrictEqual(get({ token: T, o: 'painel' }).grafico.chart, chart); });
ok('a série é aparada em 20000 linhas', () => {
  const muitos = Array.from({ length: 20050 }, (_, i) => ({ t: `2026-11-01T${String(Math.floor(i / 3600)).padStart(2, '0')}:${String(Math.floor(i / 60) % 60).padStart(2, '0')}:${String(i % 60).padStart(2, '0')}-03:00`, v: 0.4, c: 21, p: 'p' }));
  post({ token: T, points: muitos }); assert.strictEqual(sheets.serie.getLastRow() - 1, 20000);
});
ok('parâmetro desconhecido é recusado', () => assert.strictEqual(get({ token: T, o: 'xx' }).ok, false));
ok('limpar sem token é recusado e não apaga nada', () => { assert.strictEqual(post({ token: 'x', acao: 'limpar' }).ok, false); assert.ok(sheets.serie.getLastRow() > 1); });
ok('limpar zera as quatro abas e mantém os cabeçalhos', () => {
  const r = post({ token: T, acao: 'limpar' });
  assert.strictEqual(r.ok, true); assert.strictEqual(r.limpou.serie, 20000); assert.strictEqual(r.limpou.snapshot, 2);
  assert.strictEqual(sheets.serie.rows[0].join(','), 'timestamp,volume_fracao,temperatura_c,fase,sessao');
  assert.strictEqual(sheets.serie.getLastRow(), 1);
  const p = get({ token: T, o: 'painel' }); assert.ok(p.ao_vivo.vazio && p.grafico.vazio && p.snapshot.vazio);
  assert.deepStrictEqual(get({ token: T, o: 'serie' }).pontos, []);
});
ok('depois de limpar, dados novos entram normalmente', () => assert.strictEqual(post({ token: T, points: [pt(1, 0.3)] }).recebido.pontos, 1));
ok('uma sessão com nome ocupa o espaço 1; os pontos antigos sem nome seguem para ela', () => {
  post({ token: T, acao: 'limpar' });
  const r = post({ token: T, session: 'A', points: [pt(1, 0.3), pt(2, 0.4)], live: { a: 1 } });
  assert.strictEqual(r.recebido.slot, 1); assert.strictEqual(r.recebido.sessao_nova, true);
  assert.deepStrictEqual(get({ token: T, o: 'sessoes' }).sessoes.map(x => [x.slot, x.nome]), [[1, 'A']]);
});
ok('uma segunda sessão vai para o espaço 2 e a primeira fica intacta', () => {
  const r = post({ token: T, session: 'B', points: [pt(1, 0.2), pt(2, 0.25), pt(3, 0.3)], live: { b: 1 } });
  assert.strictEqual(r.recebido.slot, 2); assert.strictEqual(r.recebido.pontos, 3);
  assert.deepStrictEqual(get({ token: T, o: 'serie', slot: 1 }).pontos.map(p => p.s), ['A', 'A']);
  assert.deepStrictEqual(get({ token: T, o: 'serie', slot: 2 }).pontos.map(p => p.s), ['B', 'B', 'B']);
  assert.deepStrictEqual(Object.keys(get({ token: T, o: 'ao_vivo', slot: 1 }).live), ['a']);
  assert.deepStrictEqual(Object.keys(get({ token: T, o: 'ao_vivo', slot: 2 }).live), ['b']);
});
ok('o painel traz a lista das sessões e o espaço pedido; sem pedir, vem a que enviou por último', () => {
  const um = get({ token: T, o: 'painel', slot: 1 });
  assert.strictEqual(um.slot, 1); assert.deepStrictEqual(um.sessoes.map(x => x.nome), ['A', 'B']); assert.strictEqual(um.ao_vivo.live.a, 1);
  const padrao = get({ token: T, o: 'painel' }); assert.strictEqual(padrao.slot, 2); assert.strictEqual(padrao.ao_vivo.live.b, 1);
  assert.strictEqual(get({ token: T, o: 'painel', slot: 9 }).slot, 2); // espaço que não existe: cai no padrão
});
ok('cada sessão mantém as suas imagens', () => {
  post({ token: T, session: 'A', snapshot: { t: '2026-11-01T00:00:00-03:00', jpeg_b64: 'IA' } });
  post({ token: T, session: 'B', snapshot: { t: '2026-11-01T00:00:01-03:00', jpeg_b64: 'IB' } });
  assert.strictEqual(get({ token: T, o: 'painel', slot: 1 }).snapshot.jpeg_b64, 'IA');
  assert.deepStrictEqual(get({ token: T, o: 'painel', slot: 2 }).quadros.map(q => q.jpeg_b64), ['IB']);
});
ok('a mesma sessão não zera nada e continua no seu espaço', () => {
  const r = post({ token: T, session: 'A', points: [pt(4, 0.35)] });
  assert.ok(!r.recebido.sessao_nova); assert.strictEqual(r.recebido.slot, 1); assert.strictEqual(get({ token: T, o: 'serie', slot: 1 }).pontos.length, 3);
});
ok('pacote sem nome vai para a sessão que enviou por último, sem zerar', () => {
  post({ token: T, live: { c: 1 } });
  assert.strictEqual(get({ token: T, o: 'serie', slot: 1 }).pontos.length, 3); assert.strictEqual(get({ token: T, o: 'serie', slot: 2 }).pontos.length, 3);
  assert.strictEqual(get({ token: T, o: 'ao_vivo', slot: 1 }).live.c, 1); // A foi a última a enviar com nome
});
ok('uma terceira sessão toma o lugar da que está há mais tempo sem enviar', () => {
  post({ token: T, session: 'B', live: { b2: 1 } }); // B enviou depois de A: a mais antiga é A
  const r = post({ token: T, session: 'C', points: [pt(1, 0.1)] });
  assert.strictEqual(r.recebido.slot, 1); assert.strictEqual(r.recebido.sessao_nova, true);
  assert.deepStrictEqual(get({ token: T, o: 'sessoes' }).sessoes.map(x => x.nome), ['C', 'B']);
  assert.deepStrictEqual(get({ token: T, o: 'serie', slot: 1 }).pontos.map(p => p.s), ['C']); // nada da A sobrou
  assert.strictEqual(get({ token: T, o: 'serie', slot: 2 }).pontos.length, 3); // a B não foi tocada
});
ok('limpar com o nome de uma sessão zera só ela', () => {
  const r = post({ token: T, acao: 'limpar', session: 'C' });
  assert.ok(r.ok && r.limpou.serie === 1);
  assert.deepStrictEqual(get({ token: T, o: 'sessoes' }).sessoes.map(x => x.nome), ['B']);
  assert.strictEqual(get({ token: T, o: 'serie', slot: 2 }).pontos.length, 3);
  assert.deepStrictEqual(post({ token: T, acao: 'limpar', session: 'Nunca' }).limpou, {}); // nome desconhecido: nada
  assert.strictEqual(get({ token: T, o: 'serie', slot: 2 }).pontos.length, 3);
});
ok('uma sessão parada há mais de 72 h libera o espaço quando outra chega', () => {
  post({ token: T, session: 'D', points: [pt(1, 0.1)] }); // ocupa o espaço 1, livre desde que a C saiu
  const linhas = sheets.sessoes.rows; linhas.forEach(l => { if (l[1] === 'B') l[2] = new Date(Date.now() - 80 * 3600 * 1000).toISOString(); });
  post({ token: T, session: 'D', live: { d: 1 } });
  assert.deepStrictEqual(get({ token: T, o: 'sessoes' }).sessoes.map(x => x.nome), ['D']); // B saiu sozinha
  assert.strictEqual(sheets.serie_2.getLastRow(), 1); // os dados da B foram apagados
});
ok('limpar sem nome zera todas as sessões', () => {
  post({ token: T, session: 'E', points: [pt(1, 0.1)] });
  const r = post({ token: T, acao: 'limpar' });
  assert.ok(r.ok && r.limpou.serie >= 2); assert.deepStrictEqual(get({ token: T, o: 'sessoes' }).sessoes, []);
});
ok('limparTudo (editor) também funciona', () => { ctx.__api.limparTudo(); assert.strictEqual(sheets.serie.getLastRow(), 1); });
console.log(`\n${n} verificações passaram`);
