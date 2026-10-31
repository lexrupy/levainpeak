# LevainPeak — estado do projeto

Nota para retomar numa conversa nova. Atualizada em 2026-10-31, com 81 testes passando. Complementa o `AGENTS.md` (regras do repositório) e o `README.md`.

## O que o app faz

Monitora um levain por uma câmera RTSP fixa. Mostra o vídeo, o pote detectado, o volume relativo,
a fase e a temperatura lida do visor de um termômetro. Grava uma linha de CSV a cada 10 s por padrão.

| Módulo (`src/levainpeak/`) | Papel |
|---|---|
| `stream.py` | Captura RTSP. |
| `analysis.py` | Thread de análise (`ContainerAnalyzer`): mede o pote, estabiliza, lê o termômetro, escolhe o perfil. |
| `vision.py` | `measure_container`, `column_scan` (superfície por claridade, com reserva por bordas), perfil de largura e volume. |
| `temperature.py` | OCR do visor de 7 segmentos (dia e infravermelho). |
| `profiles.py`, `imaging.py` | Perfil de captura `dia` / `infravermelho`, escolhido pela cor da imagem. |
| `filters.py` | `OutlierGate`, `despike`, `valid_after_recovery`. |
| `sessions.py` | `SessionLog` (CSV e JSON da sessão), `PhaseTracker`. |
| `forecast.py` | Previsão do dobro, com a temperatura como contexto. |
| `graph_phases.py` | Fases do gráfico: Fase inicial, Crescimento, Pico, Declínio. |
| `debug_view.py` | Janela Diagnóstico (mosaico ao vivo das etapas da detecção). |
| `floating.py` | Mover, redimensionar pela borda e fechar com Esc nos popups. |
| `settings.py` | Configurações (`data/settings.json`). |
| `graph_data.py`, `phone_config.py`, `phone_push.py` | Dados do gráfico, `phone.ini` e envio ao celular. |
| `app.py` | Interface Tk. |

## Como rodar

- Testes: `.venv/bin/python -m pytest -q` (sem janelas; `--gui` abre as dos testes de interface)
- App: `.venv/bin/python -m levainpeak.app --resume` (retoma a sessão com o CSV modificado mais recentemente).
- Reiniciar o app aberto: `kill $(pgrep -f levainpeak.app)` e subir de novo com `--resume`, em segundo plano.
- Câmera: `camera.ini` local (fora do git). Nunca versionar credenciais nem imagens da câmera.
- Sessões: `data/sessions/*.csv` e `.json` (fora do git). Cópias de segurança em
  `data/sessions/backup_antes_da_correcao_2026-10-30/`.

## Comportamentos que não são óbvios

- **Volume relativo:** fração da capacidade do pote, integrando a largura ao quadrado até a superfície.
  0% do gráfico é o nível inicial. 100% é o dobro, até ele ser alcançado, e depois o pico da sessão.
- **Marca de início:** pode ser manual. Fica no JSON da sessão (`marker_y`) e volta ao retomar.
  O nível inicial da retomada vem da última linha do CSV.
- **Perfis:** em infravermelho a imagem é cinza (cor média 0,00 contra ~10 de dia). O topo e o fundo do pote
  são medidos de dia e reaproveitados à noite (`vessel_bounds`, no JSON). O OCR usa realce de nitidez.
- **Limpeza de dados:** salto de volume só vale se durar uns 20 s. Uma leitura abaixo de um nível já alcançado,
  e depois superada por outra, é inválida (`valid_after_recovery`). Temperatura não lida repete a última boa.
- **Fases do gráfico:** recalculadas dos dados limpos. "Pico" só aparece num platô confirmado antes da queda.
- **OCR:** só 2, 1, 9 e 8 foram validados em quadros reais. Em infravermelho só o 8. Um 3 lido como 8 aparece
  com borrão forte em teste sintético.
- **Interface:** o vídeo é reduzido à resolução da tela antes de processar. O gráfico só é recalculado quando o
  CSV muda. Os cartões reservam 3 linhas e a mensagem 2.

## Preferências de trabalho do usuário

- Não edita o código enquanto o assistente trabalha. O nvim reformata ao salvar. Conferir `git status` e
  `git diff` no começo de cada rodada. Commitar só os arquivos próprios, nunca `git add -A`.
- Reiniciar o app com `--resume` sempre que for preciso.

## Celular: o app envia para uma planilha e um painel lê

Implementado (2026-10-31). O fluxo: o app do PC envia **um pacote por ciclo** para o Google Sheets, por um
Apps Script; um painel estático só de leitura mostra os dados.

| Peça | Onde |
|---|---|
| Receptor (Apps Script) | `docs/apps-script/Code.gs`; simulação em `docs/apps-script/simulacao.js` (`node docs/apps-script/simulacao.js`) |
| Envio no app | `src/levainpeak/phone_push.py` (`PhonePusher`, `build_packet`, `make_snapshot`), ligado em `app.py` |
| Configuração secreta | `phone.ini` (`[phone]`, `url` e `token`; fora do git; modelo `phone.ini.example`); leitura em `phone_config.py` |
| Opções | aba Configurações: ligar, intervalo (60 a 900 s, padrão 120), imagem do pote (desligada por padrão); `settings.py` |
| Dados do gráfico | `graph_data.py` (`prepare_graph_data`, `volume_scale`, `chart_payload`), usado pela janela e pelo painel |
| Painel | `painel/` (HTML, JS e CSS sem biblioteca). Rodar: `cd painel && python -m http.server 8000`. Links prontos: `python -m levainpeak.phone_config` |

- **Pacote:** `{token, session, sent_at, live, points, chart, snapshot}`. `points` = `{t, v, c, p}` (só os novos desde
  o último envio); `chart` = série já na escala do app (0% = início, 100% = dobro ou pico), até 400 pontos, com as
  fases; `snapshot` = recorte do pote em base64, abaixo de 25.000 caracteres. `live` = `sessao, volume_pct,
  crescimento, fase, temperatura_c, temperatura_nota, previsao, perfil, status, intervalo_s`.
- **Leitura:** `GET ?token=...&o=painel[&snap=<t>]` devolve estado, gráfico e snapshot numa chamada; o snapshot só
  vai se mudou. O painel guarda o endereço e o token no `localStorage` (entram pelo `#u=...&t=...`, que não vai
  para o servidor) e atualiza a cada 60 s.
- **Implantação do Apps Script:** "Executar como: Eu", "Quem pode acessar: Qualquer pessoa". **Depois de editar o
  `Code.gs`, criar uma nova versão** em "Gerenciar implantações". Se o painel disser que o script está
  desatualizado, é isso.
- **Cuidados:** o `POST` ao Apps Script é respondido com um redirecionamento 302 (o `urllib` já segue; no `curl`,
  `-L -d ...` sem `-X POST`). O token nunca vai para log nem para a mensagem de status. O token colado em
  conversa deve ser trocado (propriedade `TOKEN` do script e `phone.ini`).
- **Testes:** `pytest -q` não abre janela. `pytest --gui` roda os testes que abrem janelas e tomam o foco.
- **Descartados:** Firebase (o "ao vivo" de verdade não compensa com envio a cada 2 a 4 min), Gist, página do
  claude.ai.

Validado contra a planilha real em 2026-10-31: envio aceito (1000 pontos), `chart` gravado, `o=painel` lido e o
painel renderizado no Chrome headless com esses dados. A implantação em uso é a criada em "Nova implantação"
(o `/exec` muda a cada nova implantação; editar a existente e criar **nova versão** mantém a URL). A antiga deve ser
arquivada.

Pendente: o usuário ligar o envio na aba Configurações; apagar a linha de teste ("teste", 01/01) da aba `serie`;
abrir o painel de novo pelo link do `python -m levainpeak.phone_config` nos aparelhos que guardaram a URL antiga.
Ainda não vistos: o painel no celular de verdade e o tema escuro pela preferência do sistema.

## Limites conhecidos

- A leitura do termômetro em infravermelho foi ajustada em um único valor (21.8).
- A janela mínima exige ~903 px. O gerenciador de janelas do usuário (tiling) pode abrir a janela mais estreita.
- O gráfico lê as fases recalculadas, mas o CSV guarda os estados detalhados do rastreador.
