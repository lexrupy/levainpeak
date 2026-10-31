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
ok('painel vazio responde sem erro', () => { const r = get({ token: T, o: 'painel' }); assert.ok(r.ok && r.ao_vivo.vazio && r.grafico.vazio && r.snapshot.vazio); });
ok('POST completo grava pontos, ao vivo, gráfico e snapshot', () => {
  const r = post({ token: T, session: 'Bradok', sent_at: '2026-10-31T00:05:00-03:00',
    live: { volume_pct: 42.3, fase: 'Crescendo' }, points: [pt(1, 0.31), pt(2, 0.32), pt(3, 0.33)], chart, snapshot: { t: '2026-10-31T00:05:00-03:00', jpeg_b64: 'QUJD' } });
  assert.deepStrictEqual(r, { ok: true, recebido: { pontos: 3, ao_vivo: true, grafico: 'ok', snapshot: 'ok' } });
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
ok('snapshot grande demais é recusado e o antigo fica', () => { assert.match(post({ token: T, snapshot: { jpeg_b64: 'A'.repeat(50000) } }).recebido.snapshot, /grande demais/); assert.strictEqual(get({ token: T, o: 'snapshot' }).jpeg_b64, 'QUJD'); });
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
ok('sessão diferente zera a antiga e grava a nova', () => {
  post({ token: T, session: 'Antiga', points: [pt(1, 0.3), pt(2, 0.4)], live: { a: 1 } });
  const r = post({ token: T, session: 'Nova', points: [pt(1, 0.2), pt(2, 0.25), pt(3, 0.3)], live: { b: 1 } });
  assert.strictEqual(r.recebido.sessao_nova, true); assert.strictEqual(r.recebido.pontos, 3);
  const s = get({ token: T, o: 'serie' }).pontos; assert.ok(s.length === 3 && s.every(p => p.s === 'Nova'));
  assert.deepStrictEqual(Object.keys(get({ token: T, o: 'ao_vivo' }).live), ['b']);
});
ok('a mesma sessão não zera nada', () => { const r = post({ token: T, session: 'Nova', points: [pt(4, 0.35)] }); assert.ok(!r.recebido.sessao_nova); assert.strictEqual(get({ token: T, o: 'serie' }).pontos.length, 4); });
ok('pacote sem nome de sessão nunca zera', () => { post({ token: T, live: { c: 1 } }); assert.strictEqual(get({ token: T, o: 'serie' }).pontos.length, 4); });
ok('limparTudo (editor) também funciona', () => { ctx.__api.limparTudo(); assert.strictEqual(sheets.serie.getLastRow(), 1); });
console.log(`\n${n} verificações passaram`);
